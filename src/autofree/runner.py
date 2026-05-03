"""Orchestration for free-account production.

Single-round flow (N accounts):

    1. master.set_auto_provision(False)   # 防 verified-domain 自动入 Team
    2. baseline = master.list_members()
       require: N + len(baseline) <= 10
    3. for i in 1..N:
           email = mail.create_temp_email()
           flow.run_register(email, password, ...)
           cohort.append({email, password, mailbox_id})
    4. master.set_auto_provision(True)
       sleep AP_PROPAGATION_DELAY
    5. for acc in cohort:
           tokens = flow.oauth_personal(acc.email, acc.password)
           cpa_push.save_and_register(acc.email, tokens)
    6. for acc in cohort:
           master.kick_user_by_email(acc.email)

Multi-round = R × single-round, all serial.

Background-thread runner with cancel signal — frontend polls
storage.get_run(run_id) for status / logs / cohort progress.
"""

from __future__ import annotations

import logging
import random
import string
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from autofree import cpa_push, master, storage
from autofree.config import AP_PROPAGATION_DELAY
from autofree.mail import get_mail_client
from autofree.settings import get_proxy

logger = logging.getLogger(__name__)


# ============================================================ constants

TEAM_HARD_CAP = 10  # N + 已有成员数 <= 10

# Stage labels used in storage.update_run(current_stage=...) — match what
# the web UI displays so the frontend can render a stepper without coupling.
STAGE_INIT = "init"
STAGE_AP_OFF = "auto_provision_off"
STAGE_REGISTER = "register"
STAGE_AP_ON = "auto_provision_on"
STAGE_OAUTH = "oauth"
STAGE_KICK = "kick"
STAGE_DONE = "done"


# ============================================================ cancel signal


class CancelSignal:
    """Cooperative cancel — runner checks .is_set() at every safe boundary."""

    def __init__(self):
        self._evt = threading.Event()

    def cancel(self) -> None:
        self._evt.set()

    def is_set(self) -> bool:
        return self._evt.is_set()


# Map run_id -> CancelSignal for in-flight tasks.
_CANCELS: dict[str, CancelSignal] = {}
_CANCELS_LOCK = threading.Lock()


def cancel_run(run_id: str) -> bool:
    with _CANCELS_LOCK:
        sig = _CANCELS.get(run_id)
    if sig:
        sig.cancel()
        return True
    return False


def is_running(run_id: str) -> bool:
    with _CANCELS_LOCK:
        return run_id in _CANCELS


# ============================================================ helpers


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _generate_password(length: int = 14) -> str:
    """Match daily-playwright.py policy: ≥ 1 lower + 1 upper + 1 digit + 1 symbol."""
    chars = string.ascii_lowercase + string.ascii_uppercase + string.digits + "!@#$%&*"
    seed = [
        random.choice(string.ascii_lowercase),
        random.choice(string.ascii_uppercase),
        random.choice(string.digits),
        random.choice("!@#$%&*"),
    ]
    seed += [random.choice(chars) for _ in range(length - 4)]
    random.shuffle(seed)
    return "".join(seed)


def _random_name() -> str:
    first = random.choice(
        ["James", "Emma", "Liam", "Olivia", "Noah", "Ava", "Lucas", "Mia", "Logan", "Charlotte"]
    )
    last = random.choice(["Smith", "Johnson", "Brown", "Davis", "Wilson", "Moore", "Taylor", "Clark"])
    return f"{first} {last}"


def _random_birthdate() -> str:
    y = random.randint(1985, 2002)
    m = random.randint(1, 12)
    d = random.randint(1, 28)
    return f"{y}-{m:02d}-{d:02d}"


def _logger_for(run_id: str) -> Callable[[str, str], None]:
    """Return a small adapter that writes to both python logging and the run's
    on-disk log file simultaneously."""

    def emit(line: str, level: str = "info") -> None:
        getattr(logger, level if level in ("debug", "info", "warning", "error") else "info")(
            "[run %s] %s", run_id, line
        )
        try:
            storage.append_log(run_id, line, level=level)
        except Exception:
            pass

    return emit


# ============================================================ flow.py adapter


def _build_flow():
    """Construct a Flow instance — imported lazily because flow.py is
    written in a separate batch. ImportError surfaces here with a clear hint."""
    try:
        from autofree.flow import Flow  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "autofree.flow 模块尚未实现 — 写完 flow.py 后此处自动接通。"
            f" (ImportError: {exc})"
        ) from exc
    return Flow


# ============================================================ single round


def _run_round(
    *,
    n: int,
    run_id: str,
    round_index: int,
    cancel: CancelSignal,
    mail_provider: str | None = None,
) -> dict[str, Any]:
    """Execute one round of N accounts. Returns per-round summary."""
    log = _logger_for(run_id)
    Flow = _build_flow()

    proxy = get_proxy() or None
    mail = get_mail_client(mail_provider)
    mail.login()

    master_client = master.get_default_client()

    summary = {"round": round_index, "n": n, "registered": 0, "oauthed": 0, "kicked": 0, "errors": []}

    # ---- 1. AP off ----
    storage.update_run(run_id, current_stage=STAGE_AP_OFF, current_round=round_index)
    log(f"[Round {round_index}] step 1/6 — 关闭 auto_provision")
    if cancel.is_set():
        raise RuntimeError("cancelled before AP off")
    master_client.set_auto_provision(False)

    # ---- 2. capacity check ----
    log(f"[Round {round_index}] step 2/6 — 校验 Team 容量")
    members = master_client.list_members()
    if n + len(members) > TEAM_HARD_CAP:
        raise RuntimeError(
            f"N={n} + 已有成员={len(members)} > {TEAM_HARD_CAP},超过 Team 上限"
        )
    log(f"[Round {round_index}] 当前成员 {len(members)},本轮新增 {n},合计 {len(members) + n}")

    # ---- 3. register N accounts (serial) ----
    storage.update_run(run_id, current_stage=STAGE_REGISTER)
    cohort: list[dict[str, Any]] = []
    for i in range(n):
        if cancel.is_set():
            raise RuntimeError(f"cancelled at register {i + 1}/{n}")

        log(f"[Round {round_index}] step 3/6 — 注册 {i + 1}/{n}")
        mailbox_id, email = mail.create_temp_email()
        password = _generate_password()
        name = _random_name()
        birthdate = _random_birthdate()
        member: dict[str, Any] = {
            "round": round_index,
            "email": email,
            "password": password,
            "mailbox_id": str(mailbox_id),
            "name": name,
            "birthdate": birthdate,
            "stage": "registering",
            "ok": False,
            "error": "",
        }
        storage.update_cohort_member(run_id, email, member)

        flow = Flow(proxy=proxy, tag=email.split("@")[0], mail_client=mail)
        flow.set_mail_context(mailbox_id)
        try:
            flow.start()
            flow.run_register(email, password, name, birthdate)
            member["stage"] = "registered"
            member["ok"] = True
            summary["registered"] += 1
            log(f"  ✓ 注册成功 {email}")
        except Exception as exc:
            member["stage"] = "register_failed"
            member["error"] = str(exc)
            summary["errors"].append({"email": email, "where": "register", "msg": str(exc)})
            log(f"  ✗ 注册失败 {email}: {exc}", "error")
        finally:
            try:
                flow.close()
            except Exception:
                pass
            storage.update_cohort_member(run_id, email, member)
        cohort.append(member)

    # Filter cohort to those that registered ok — they go through OAuth.
    successes = [m for m in cohort if m.get("ok")]
    if not successes:
        log(f"[Round {round_index}] 本轮注册全部失败,跳过 AP-on / OAuth / kick", "warning")
        return summary

    # ---- 4. AP on + propagation wait ----
    storage.update_run(run_id, current_stage=STAGE_AP_ON)
    log(f"[Round {round_index}] step 4/6 — 开启 auto_provision (等待 {AP_PROPAGATION_DELAY}s 生效)")
    if cancel.is_set():
        raise RuntimeError("cancelled before AP on")
    master_client.set_auto_provision(True)
    _interruptible_sleep(AP_PROPAGATION_DELAY, cancel)

    # ---- 5. OAuth (serial) — force personal workspace ----
    storage.update_run(run_id, current_stage=STAGE_OAUTH)
    for i, member in enumerate(successes, 1):
        if cancel.is_set():
            raise RuntimeError(f"cancelled at oauth {i}/{len(successes)}")

        email = member["email"]
        log(f"[Round {round_index}] step 5/6 — OAuth {i}/{len(successes)} {email}")
        flow = Flow(proxy=proxy, tag=email.split("@")[0], mail_client=mail)
        flow.set_mail_context(member.get("mailbox_id"))
        try:
            flow.start()
            tokens = flow.oauth_personal(email, member["password"])
            if not tokens or not tokens.get("access_token"):
                raise RuntimeError("oauth_personal 返回空 token")
            cpa_push.save_and_register(email, tokens, extra={"run_id": run_id, "round": round_index})
            member["stage"] = "oauthed"
            summary["oauthed"] += 1
            log(f"  ✓ OAuth + 落盘成功 {email}")
        except Exception as exc:
            member["stage"] = "oauth_failed"
            member["error"] = str(exc)
            summary["errors"].append({"email": email, "where": "oauth", "msg": str(exc)})
            log(f"  ✗ OAuth 失败 {email}: {exc}", "error")
        finally:
            try:
                flow.close()
            except Exception:
                pass
            storage.update_cohort_member(run_id, email, member)

    # ---- 6. kick everyone in this cohort (regardless of OAuth success) ----
    storage.update_run(run_id, current_stage=STAGE_KICK)
    for i, member in enumerate(cohort, 1):
        email = member["email"]
        log(f"[Round {round_index}] step 6/6 — kick {i}/{len(cohort)} {email}")
        try:
            ok = master_client.kick_user_by_email(email)
            if ok:
                member["kicked"] = True
                summary["kicked"] += 1
                log(f"  ✓ kicked {email}")
            else:
                member["kicked"] = False
                log(f"  ⚠ kick 未找到 {email}", "warning")
        except Exception as exc:
            member["kicked"] = False
            member["error"] = (member.get("error") or "") + f"; kick: {exc}"
            summary["errors"].append({"email": email, "where": "kick", "msg": str(exc)})
            log(f"  ✗ kick 失败 {email}: {exc}", "error")
        storage.update_cohort_member(run_id, email, member)

    return summary


# ============================================================ multi-round (entry)


def _interruptible_sleep(seconds: float, cancel: CancelSignal) -> None:
    end = time.time() + max(seconds, 0)
    while time.time() < end:
        if cancel.is_set():
            return
        time.sleep(min(0.5, end - time.time()))


def _run_multi_inner(run_id: str, rounds: int, per_round: int, mail_provider: str | None) -> None:
    log = _logger_for(run_id)
    cancel = CancelSignal()
    with _CANCELS_LOCK:
        _CANCELS[run_id] = cancel

    storage.update_run(run_id, status="running", started_at=_now_iso(), current_stage=STAGE_INIT)
    log(f"=== 任务启动: rounds={rounds} per_round={per_round} ===")

    total = {"registered": 0, "oauthed": 0, "kicked": 0, "errors": []}
    final_status = "done"
    fatal_error = ""

    try:
        for r in range(1, rounds + 1):
            if cancel.is_set():
                final_status = "cancelled"
                log(f"[Round {r}] 接到取消信号,停止后续轮次", "warning")
                break
            log(f"=== 第 {r}/{rounds} 轮 (本轮 {per_round} 个) ===")
            try:
                summary = _run_round(
                    n=per_round, run_id=run_id, round_index=r, cancel=cancel,
                    mail_provider=mail_provider,
                )
                total["registered"] += summary["registered"]
                total["oauthed"] += summary["oauthed"]
                total["kicked"] += summary["kicked"]
                total["errors"].extend(summary["errors"])
            except Exception as exc:
                # round-level fatal — log and continue to next round unless cancelled.
                log(f"[Round {r}] 轮级失败: {exc}", "error")
                total["errors"].append({"round": r, "where": "round", "msg": str(exc)})
                if "cancelled" in str(exc):
                    final_status = "cancelled"
                    break
        else:
            final_status = "done" if not total["errors"] else "done_with_errors"
    except Exception as exc:
        final_status = "failed"
        fatal_error = str(exc)
        log(f"任务级异常: {exc}", "error")
    finally:
        with _CANCELS_LOCK:
            _CANCELS.pop(run_id, None)

    storage.update_run(
        run_id,
        status=final_status,
        finished_at=_now_iso(),
        current_stage=STAGE_DONE,
        summary={
            "registered": total["registered"],
            "oauthed": total["oauthed"],
            "kicked": total["kicked"],
            "errors": total["errors"],
            "ok": total["oauthed"],
            "failed": len(total["errors"]),
        },
        error=fatal_error,
    )
    log(f"=== 任务结束: status={final_status} 注册={total['registered']} 拿token={total['oauthed']} kick={total['kicked']} ===")


def start_run(rounds: int, per_round: int, *, mail_provider: str | None = None) -> dict[str, Any]:
    """Validate inputs, create the run record, kick off the worker thread.
    Returns the initial run record (with `id`)."""
    rounds = int(rounds)
    per_round = int(per_round)
    if rounds < 1 or per_round < 1:
        raise ValueError("rounds 与 per_round 都必须 >= 1")
    if per_round > TEAM_HARD_CAP:
        raise ValueError(f"per_round 不能超过 {TEAM_HARD_CAP}")

    record = storage.create_run(
        rounds=rounds,
        per_round=per_round,
        params={"mail_provider": mail_provider or "", "proxy": get_proxy()},
    )
    threading.Thread(
        target=_run_multi_inner,
        args=(record["id"], rounds, per_round, mail_provider),
        name=f"autofree-run-{record['id']}",
        daemon=True,
    ).start()
    return record


# ============================================================ blocking variant (for CLI)


def run_blocking(rounds: int, per_round: int, *, mail_provider: str | None = None) -> dict[str, Any]:
    """Synchronous variant for CLI use — blocks until the run finishes,
    returns the final run record."""
    record = storage.create_run(
        rounds=rounds,
        per_round=per_round,
        params={"mail_provider": mail_provider or "", "proxy": get_proxy()},
    )
    _run_multi_inner(record["id"], rounds, per_round, mail_provider)
    return storage.get_run(record["id"]) or record
