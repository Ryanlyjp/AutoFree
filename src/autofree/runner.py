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
           if immediate-kick mode:
               master.kick_user_by_email(acc.email)
               cpa_push.push_one(acc.email)
    6. if round-end-kick mode:
           for acc in cohort:
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

from autofree import cpa_push, easyproxy, master, storage
from autofree.config import AP_PROPAGATION_DELAY
from autofree.mail import get_mail_client
from autofree.settings import easyproxy_enabled, get_cpa_config, get_easyproxy_config, get_master_proxy_url
from autofree.settings import get_proxy, get_proxy_master_mode

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
STAGE_CPA_PUSH = "cpa_push"
STAGE_DONE = "done"
STAGE_REGISTER_ONLY_DONE = "register_only_done"

KICK_MODE_ROUND_END = "round_end"
KICK_MODE_AFTER_EACH_AUTH = "after_each_auth"
_KICK_MODES = {KICK_MODE_ROUND_END, KICK_MODE_AFTER_EACH_AUTH}


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


def _ensure_cpa_ready_for_auto_push() -> None:
    cfg = get_cpa_config()
    base_url = (cfg.get("base_url") or "").strip()
    key = (cfg.get("key") or "").strip()
    if not base_url or not key:
        raise ValueError("已勾选自动推送 CPA,但 CPA 未配置 base_url / key")


def _ensure_easyproxy_ready_for_run() -> None:
    if not easyproxy_enabled():
        return
    status = easyproxy.get_status()
    if not status.get("ok"):
        raise ValueError(f"easyproxy 不可用: {status.get('error') or 'unknown error'}")
    if not (status.get("summary") or {}).get("selectable"):
        raise ValueError("easyproxy 当前没有可用端口，请先释放黑名单或检查 hybrid 节点")


def normalize_kick_mode(value: str | None) -> str:
    mode = (value or KICK_MODE_ROUND_END).strip().lower()
    if mode not in _KICK_MODES:
        allowed = ", ".join(sorted(_KICK_MODES))
        raise ValueError(f"不支持的 kick_mode: {value!r} (允许: {allowed})")
    return mode


def _new_cpa_summary(enabled: bool) -> dict[str, Any]:
    return {"enabled": enabled, "attempted": 0, "pushed": 0, "skipped": 0, "failed": 0}


def _merge_cpa_summary(total: dict[str, Any], partial: dict[str, Any] | None) -> None:
    if not partial:
        return
    total["enabled"] = bool(total.get("enabled") or partial.get("enabled"))
    for field in ("attempted", "pushed", "skipped", "failed"):
        total[field] = int(total.get(field) or 0) + int(partial.get(field) or 0)
    if partial.get("skipped_reason") and not total.get("skipped_reason"):
        total["skipped_reason"] = partial["skipped_reason"]


def _append_member_error(member: dict[str, Any], message: str) -> None:
    message = (message or "").strip()
    if not message:
        return
    existing = (member.get("error") or "").strip()
    member["error"] = f"{existing}; {message}" if existing else message


def _current_network_snapshot() -> dict[str, Any]:
    easy_cfg = easyproxy.normalize_easyproxy_settings(existing=get_easyproxy_config())
    return {
        "proxy": get_proxy(),
        "proxy_master_mode": get_proxy_master_mode(),
        "master_proxy": get_master_proxy_url(),
        "easyproxy": {
            "enabled": bool(easy_cfg.get("enabled")),
            "management_url": easy_cfg.get("management_url") or "",
            "proxy_host": easy_cfg.get("proxy_host") or "127.0.0.1",
            "pool_port": int(easy_cfg.get("pool_port") or 2323),
            "port_min": int(easy_cfg.get("port_min") or 24000),
            "port_max": int(easy_cfg.get("port_max") or 24100),
            "cooldown_minutes": int(easy_cfg.get("cooldown_minutes") or 60),
            "master_mode": easy_cfg.get("master_mode") or easyproxy.EASYPROXY_MASTER_MODE_DIRECT,
        },
    }


def _assign_member_proxy(
    *,
    member: dict[str, Any],
    used_easyproxy_ports: set[int],
    log: Callable[[str, str], None],
) -> str | None:
    if easyproxy_enabled():
        assignment = easyproxy.select_proxy_assignment(avoid_ports=used_easyproxy_ports)
        used_easyproxy_ports.add(int(assignment["port"]))
        member["proxy_mode"] = "easyproxy"
        member["proxy_url"] = assignment["proxy_url"]
        member["proxy_port"] = int(assignment["port"])
        member["proxy_tag"] = assignment.get("tag") or ""
        member["proxy_name"] = assignment.get("name") or ""
        log(
            f"[proxy] {member['email']} -> easyproxy {member['proxy_port']}"
            f" ({member['proxy_tag'] or member['proxy_name'] or 'unknown'})"
        )
        return assignment["proxy_url"]

    proxy = get_proxy().strip() or ""
    member["proxy_mode"] = "proxy" if proxy else "direct"
    member["proxy_url"] = proxy
    member["proxy_port"] = 0
    member["proxy_tag"] = ""
    member["proxy_name"] = ""
    return proxy or None


def _member_proxy_url(member: dict[str, Any]) -> str | None:
    proxy = str(member.get("proxy_url") or "").strip()
    return proxy or None


def _maybe_blacklist_member_proxy(
    *,
    member: dict[str, Any],
    exc: Exception,
    log: Callable[[str, str], None],
) -> None:
    if member.get("proxy_mode") != "easyproxy":
        return
    port = int(member.get("proxy_port") or 0)
    if not port or not easyproxy.is_network_error(exc):
        return
    try:
        easyproxy.mark_port_bad(
            port,
            str(exc),
            tag=str(member.get("proxy_tag") or ""),
            name=str(member.get("proxy_name") or ""),
        )
        log(f"[proxy] easyproxy 端口 {port} 已加入本地黑名单", "warning")
    except Exception as mark_exc:
        log(f"[proxy] easyproxy 黑名单写入失败: {mark_exc}", "warning")


def _kick_member(
    *,
    run_id: str,
    round_index: int,
    ordinal: int,
    total: int,
    member: dict[str, Any],
    master_client: Any,
    summary: dict[str, Any],
    log: Callable[[str, str], None],
) -> None:
    email = member["email"]
    log(f"[Round {round_index}] step 6/6 — kick {ordinal}/{total} {email}")
    try:
        ok, reason = master_client.kick_user_by_email(email)
        if ok:
            member["kicked"] = True
            summary["kicked"] += 1
            log(f"  ✓ kicked {email}")
        else:
            member["kicked"] = False
            _append_member_error(member, f"kick: {reason}")
            summary["errors"].append({"email": email, "where": "kick", "msg": reason})
            log(f"  ⚠ kick 失败 {email}: {reason}", "warning")
    except Exception as exc:
        member["kicked"] = False
        _append_member_error(member, f"kick: {exc}")
        summary["errors"].append({"email": email, "where": "kick", "msg": str(exc)})
        log(f"  ✗ kick 异常 {email}: {exc}", "error")
    storage.update_cohort_member(run_id, email, member)


def _push_run_auth_now(run_id: str, email: str) -> dict[str, Any]:
    log = _logger_for(run_id)
    try:
        result = cpa_push.push_one(email, overwrite=False)
    except Exception as exc:
        log(f"[CPA] ✗ 推送失败 {email}: {exc}", "error")
        return {"ok": False, "skipped": False, "error": str(exc), "email": email}

    result = {"email": email, **result}
    if result.get("ok"):
        log(f"[CPA] ✓ 已推送 {email}")
    elif result.get("skipped"):
        reason = result.get("reason") or "skipped"
        log(f"[CPA] - 跳过 {email}: {reason}", "warning")
    else:
        reason = result.get("error") or result.get("reason") or "unknown error"
        log(f"[CPA] ✗ 推送失败 {email}: {reason}", "error")
    return result


def _record_cpa_result(bucket: dict[str, Any], result: dict[str, Any]) -> None:
    bucket["attempted"] = int(bucket.get("attempted") or 0) + 1
    if result.get("ok"):
        bucket["pushed"] = int(bucket.get("pushed") or 0) + 1
    elif result.get("skipped"):
        bucket["skipped"] = int(bucket.get("skipped") or 0) + 1
    else:
        bucket["failed"] = int(bucket.get("failed") or 0) + 1


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
    register_only: bool = False,
    auto_push_cpa: bool = False,
    kick_mode: str = KICK_MODE_ROUND_END,
) -> dict[str, Any]:
    """Execute one round of N accounts. Returns per-round summary."""
    log = _logger_for(run_id)
    Flow = _build_flow()
    kick_mode = normalize_kick_mode(kick_mode)

    used_easyproxy_ports: set[int] = set()
    mail = get_mail_client(mail_provider)
    mail.login()

    master_client = master.get_default_client()

    summary = {
        "round": round_index,
        "n": n,
        "registered": 0,
        "oauthed": 0,
        "kicked": 0,
        "errors": [],
        "oauth_emails": [],
        "cpa": _new_cpa_summary(enabled=auto_push_cpa and kick_mode == KICK_MODE_AFTER_EACH_AUTH),
    }

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
        flow = None
        try:
            flow_proxy = _assign_member_proxy(member=member, used_easyproxy_ports=used_easyproxy_ports, log=log)
            storage.update_cohort_member(run_id, email, member)
            flow = Flow(
                proxy=flow_proxy, tag=email.split("@")[0], mail_client=mail,
                log_emitter=log, master_account_id=master_client.account_id,
            )
            flow.set_mail_context(mailbox_id)
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
            _maybe_blacklist_member_proxy(member=member, exc=exc, log=log)
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

    # ---- register_only mode: skip OAuth + kick ----
    if register_only:
        storage.update_run(run_id, current_stage=STAGE_REGISTER_ONLY_DONE)
        log(f"[Round {round_index}] 仅注册模式 — 跳过 OAuth + kick,共注册 {summary['registered']} 个账号")
        for m in cohort:
            if m.get("ok"):
                m["stage"] = "registered_pending_oauth"
                storage.update_cohort_member(run_id, m["email"], m)
        return summary

    # ---- 5. OAuth (serial) — force personal workspace ----
    storage.update_run(run_id, current_stage=STAGE_OAUTH)
    for i, member in enumerate(successes, 1):
        if cancel.is_set():
            raise RuntimeError(f"cancelled at oauth {i}/{len(successes)}")

        email = member["email"]
        oauth_ok = False
        log(f"[Round {round_index}] step 5/6 — OAuth {i}/{len(successes)} {email}")
        flow = None
        try:
            flow = Flow(
                proxy=_member_proxy_url(member), tag=email.split("@")[0], mail_client=mail,
                log_emitter=log, master_account_id=master_client.account_id,
            )
            flow.set_mail_context(member.get("mailbox_id"))
            flow.start()
            tokens = flow.oauth_personal(email, member["password"])
            if not tokens or not tokens.get("access_token"):
                reason = flow.oauth_fail_reason or "oauth_personal 返回空 token (无具体原因)"
                raise RuntimeError(reason)
            cpa_push.save_and_register(email, tokens, extra={"run_id": run_id, "round": round_index})
            member["stage"] = "oauthed"
            member["error"] = ""
            oauth_ok = True
            summary["oauthed"] += 1
            summary["oauth_emails"].append(email)
            log(f"  ✓ OAuth + 落盘成功 {email}")
        except Exception as exc:
            member["stage"] = "oauth_failed"
            member["error"] = str(exc)
            summary["errors"].append({"email": email, "where": "oauth", "msg": str(exc)})
            _maybe_blacklist_member_proxy(member=member, exc=exc, log=log)
            log(f"  ✗ OAuth 失败 {email}: {exc}", "error")
        finally:
            try:
                flow.close()
            except Exception:
                pass
        storage.update_cohort_member(run_id, email, member)

        if kick_mode == KICK_MODE_AFTER_EACH_AUTH:
            storage.update_run(run_id, current_stage=STAGE_KICK)
            _kick_member(
                run_id=run_id,
                round_index=round_index,
                ordinal=i,
                total=len(successes),
                member=member,
                master_client=master_client,
                summary=summary,
                log=log,
            )
            if oauth_ok and auto_push_cpa:
                storage.update_run(run_id, current_stage=STAGE_CPA_PUSH)
                result = _push_run_auth_now(run_id, email)
                _record_cpa_result(summary["cpa"], result)
                if not result.get("ok") and not result.get("skipped"):
                    reason = result.get("error") or result.get("reason") or "unknown error"
                    _append_member_error(member, f"cpa_push: {reason}")
                    summary["errors"].append({"email": email, "where": "cpa_push", "msg": reason})
                    storage.update_cohort_member(run_id, email, member)

    # ---- 6. kick everyone in this cohort (regardless of OAuth success) ----
    if kick_mode == KICK_MODE_AFTER_EACH_AUTH:
        return summary

    storage.update_run(run_id, current_stage=STAGE_KICK)
    for i, member in enumerate(cohort, 1):
        _kick_member(
            run_id=run_id,
            round_index=round_index,
            ordinal=i,
            total=len(cohort),
            member=member,
            master_client=master_client,
            summary=summary,
            log=log,
        )

    return summary


# ============================================================ multi-round (entry)


def _interruptible_sleep(seconds: float, cancel: CancelSignal) -> None:
    end = time.time() + max(seconds, 0)
    while time.time() < end:
        if cancel.is_set():
            return
        time.sleep(min(0.5, end - time.time()))


def _auto_push_run_auths(run_id: str, emails: list[str]) -> dict[str, Any]:
    log = _logger_for(run_id)
    deduped = list(dict.fromkeys(email for email in emails if email))
    storage.update_run(run_id, current_stage=STAGE_CPA_PUSH)
    if not deduped:
        log("[CPA] 已启用自动推送,但本次 run 没有可推送的 auth", "warning")
        return {"enabled": True, "attempted": 0, "pushed": 0, "skipped": 0, "failed": 0}

    log(f"[CPA] 自动推送启动: 共 {len(deduped)} 个 auth (仅本次 run 产出)")
    result = cpa_push.push_many(deduped, overwrite=False)
    for row in result.get("results") or []:
        email = row.get("email") or "?"
        if row.get("ok"):
            log(f"[CPA] ✓ 已推送 {email}")
        elif row.get("skipped"):
            reason = row.get("reason") or "skipped"
            log(f"[CPA] - 跳过 {email}: {reason}", "warning")
        else:
            reason = row.get("error") or row.get("reason") or "unknown error"
            log(f"[CPA] ✗ 推送失败 {email}: {reason}", "error")
    log(
        f"[CPA] 自动推送结束: pushed={result.get('pushed', 0)} "
        f"skipped={result.get('skipped', 0)} failed={result.get('failed', 0)}"
    )
    return {
        "enabled": True,
        "attempted": len(deduped),
        "pushed": int(result.get("pushed") or 0),
        "skipped": int(result.get("skipped") or 0),
        "failed": int(result.get("failed") or 0),
    }


def _run_multi_inner(
    run_id: str,
    rounds: int,
    per_round: int,
    mail_provider: str | None,
    register_only: bool = False,
    auto_push_cpa: bool = False,
    kick_mode: str = KICK_MODE_ROUND_END,
) -> None:
    log = _logger_for(run_id)
    kick_mode = normalize_kick_mode(kick_mode)
    cancel = CancelSignal()
    with _CANCELS_LOCK:
        _CANCELS[run_id] = cancel

    storage.update_run(run_id, status="running", started_at=_now_iso(), current_stage=STAGE_INIT)
    mode_label = "仅注册" if register_only else "全流程"
    kick_label = "逐账号即时 kick" if kick_mode == KICK_MODE_AFTER_EACH_AUTH else "统一收尾 kick"
    cpa_label = "auto-cpa=on" if auto_push_cpa else "auto-cpa=off"
    log(f"=== 任务启动: rounds={rounds} per_round={per_round} mode={mode_label} kick={kick_label} {cpa_label} ===")

    total = {
        "registered": 0,
        "oauthed": 0,
        "kicked": 0,
        "errors": [],
        "oauth_emails": [],
        "cpa": _new_cpa_summary(enabled=auto_push_cpa),
    }
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
                    mail_provider=mail_provider, register_only=register_only,
                    auto_push_cpa=auto_push_cpa, kick_mode=kick_mode,
                )
                total["registered"] += summary["registered"]
                total["oauthed"] += summary["oauthed"]
                total["kicked"] += summary["kicked"]
                total["errors"].extend(summary["errors"])
                total["oauth_emails"].extend(summary["oauth_emails"])
                _merge_cpa_summary(total["cpa"], summary.get("cpa"))
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

    if auto_push_cpa:
        if register_only:
            log("[CPA] 当前是仅注册模式,跳过自动推送", "warning")
            total["cpa"]["skipped_reason"] = "register_only"
        elif kick_mode == KICK_MODE_AFTER_EACH_AUTH:
            if total["cpa"].get("failed") and final_status == "done":
                final_status = "done_with_errors"
        elif final_status == "cancelled":
            log("[CPA] 任务已取消,跳过自动推送", "warning")
            total["cpa"]["skipped_reason"] = "cancelled"
        elif final_status == "failed":
            log("[CPA] 任务级异常导致失败,跳过自动推送", "warning")
            total["cpa"]["skipped_reason"] = "task_failed"
        else:
            try:
                total["cpa"] = _auto_push_run_auths(run_id, total["oauth_emails"])
            except Exception as exc:
                total["errors"].append({"where": "cpa_push", "msg": str(exc)})
                total["cpa"] = {
                    "enabled": True,
                    "attempted": len(total["oauth_emails"]),
                    "pushed": 0,
                    "skipped": 0,
                    "failed": len(total["oauth_emails"]),
                    "error": str(exc),
                }
                log(f"[CPA] 自动推送异常: {exc}", "error")

            if total["cpa"].get("failed") and final_status == "done":
                final_status = "done_with_errors"

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
            "cpa": total["cpa"],
        },
        error=fatal_error,
    )
    log(f"=== 任务结束: status={final_status} 注册={total['registered']} 拿token={total['oauthed']} kick={total['kicked']} ===")


def start_run(
    rounds: int,
    per_round: int,
    *,
    mail_provider: str | None = None,
    register_only: bool = False,
    auto_push_cpa: bool = False,
    kick_mode: str = KICK_MODE_ROUND_END,
) -> dict[str, Any]:
    """Validate inputs, create the run record, kick off the worker thread.
    Returns the initial run record (with `id`)."""
    rounds = int(rounds)
    per_round = int(per_round)
    if rounds < 1 or per_round < 1:
        raise ValueError("rounds 与 per_round 都必须 >= 1")
    if per_round > TEAM_HARD_CAP:
        raise ValueError(f"per_round 不能超过 {TEAM_HARD_CAP}")
    kick_mode = normalize_kick_mode(kick_mode)
    if auto_push_cpa:
        _ensure_cpa_ready_for_auto_push()
    _ensure_easyproxy_ready_for_run()

    record = storage.create_run(
        rounds=rounds,
        per_round=per_round,
        params={
            "mail_provider": mail_provider or "",
            **_current_network_snapshot(),
            "register_only": register_only,
            "auto_push_cpa": auto_push_cpa,
            "kick_mode": kick_mode,
        },
    )
    threading.Thread(
        target=_run_multi_inner,
        args=(record["id"], rounds, per_round, mail_provider, register_only, auto_push_cpa, kick_mode),
        name=f"autofree-run-{record['id']}",
        daemon=True,
    ).start()
    return record


# ============================================================ blocking variant (for CLI)


def run_blocking(
    rounds: int,
    per_round: int,
    *,
    mail_provider: str | None = None,
    register_only: bool = False,
    auto_push_cpa: bool = False,
    kick_mode: str = KICK_MODE_ROUND_END,
) -> dict[str, Any]:
    """Synchronous variant for CLI use — blocks until the run finishes,
    returns the final run record."""
    kick_mode = normalize_kick_mode(kick_mode)
    if auto_push_cpa:
        _ensure_cpa_ready_for_auto_push()
    _ensure_easyproxy_ready_for_run()
    record = storage.create_run(
        rounds=rounds,
        per_round=per_round,
        params={
            "mail_provider": mail_provider or "",
            **_current_network_snapshot(),
            "register_only": register_only,
            "auto_push_cpa": auto_push_cpa,
            "kick_mode": kick_mode,
        },
    )
    _run_multi_inner(record["id"], rounds, per_round, mail_provider, register_only, auto_push_cpa, kick_mode)
    return storage.get_run(record["id"]) or record
