"""Persistence for free-account auth files and run logs."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autofree.config import AUTHS_DIR, RUNS_DIR

_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds")


# ============================================================ auths/


def auth_path(email: str) -> Path:
    return AUTHS_DIR / f"{email}.json"


def save_auth(email: str, payload: dict[str, Any]) -> Path:
    path = auth_path(email)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    return path


def load_auth(email: str) -> dict[str, Any] | None:
    path = auth_path(email)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_auths() -> list[dict[str, Any]]:
    """List all stored free-account auth files (metadata only)."""
    out: list[dict[str, Any]] = []
    for path in sorted(AUTHS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append(
            {
                "email": data.get("email") or path.stem,
                "account_id": data.get("account_id") or "",
                "expired": data.get("expired") or "",
                "last_refresh": data.get("last_refresh") or "",
                "type": data.get("type") or "",
                "pushed_to_cpa_at": data.get("pushed_to_cpa_at") or "",
                "file": path.name,
            }
        )
    return out


def delete_auth(email: str) -> bool:
    path = auth_path(email)
    if path.is_file():
        path.unlink()
        return True
    return False


def mark_pushed(email: str, ts: str | None = None) -> None:
    """Record CPA push timestamp on the auth file (does not touch token fields)."""
    data = load_auth(email)
    if not data:
        return
    data["pushed_to_cpa_at"] = ts or _now_iso()
    save_auth(email, data)


# ============================================================ runs/


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def run_path(run_id: str) -> Path:
    return RUNS_DIR / f"{run_id}.json"


def create_run(rounds: int, per_round: int, params: dict[str, Any] | None = None) -> dict[str, Any]:
    run_id = new_run_id()
    record = {
        "id": run_id,
        "status": "pending",
        "created_at": _now_iso(),
        "started_at": "",
        "finished_at": "",
        "rounds": rounds,
        "per_round": per_round,
        "params": params or {},
        "current_round": 0,
        "current_stage": "",
        "logs": [],
        "cohort": [],
        "summary": {"ok": 0, "failed": 0},
        "error": "",
    }
    _write_run(record)
    return record


def _read_run(run_id: str) -> dict[str, Any] | None:
    path = run_path(run_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_run(record: dict[str, Any]) -> None:
    path = run_path(record["id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def update_run(run_id: str, **fields: Any) -> dict[str, Any] | None:
    with _LOCK:
        rec = _read_run(run_id)
        if not rec:
            return None
        for k, v in fields.items():
            rec[k] = v
        _write_run(rec)
        return rec


def append_log(run_id: str, line: str, level: str = "info") -> None:
    with _LOCK:
        rec = _read_run(run_id)
        if not rec:
            return
        rec["logs"].append({"ts": _now_iso(), "level": level, "msg": line})
        # Cap log size in memory file — keep last 5000 lines.
        if len(rec["logs"]) > 5000:
            rec["logs"] = rec["logs"][-5000:]
        _write_run(rec)


def update_cohort_member(run_id: str, email: str, fields: dict | None = None, /, **extra: Any) -> None:
    """Insert-or-update one cohort entry, keyed by email.

    Pass updates as the positional `fields` dict, or as keyword args, or both.
    Keyword args override the dict on conflict. Any "email" key inside
    `fields` / `extra` is silently dropped — the positional `email` always
    wins, so the caller is free to splat a member dict that itself contains
    its own "email" entry.
    """
    merged: dict[str, Any] = {}
    if fields:
        merged.update(fields)
    merged.update(extra)
    merged.pop("email", None)
    with _LOCK:
        rec = _read_run(run_id)
        if not rec:
            return
        for member in rec["cohort"]:
            if member.get("email") == email:
                member.update(merged)
                _write_run(rec)
                return
        rec["cohort"].append({"email": email, **merged})
        _write_run(rec)


def delete_run(run_id: str) -> bool:
    path = run_path(run_id)
    with _LOCK:
        if path.is_file():
            path.unlink()
            return True
    return False


def get_run(run_id: str) -> dict[str, Any] | None:
    with _LOCK:
        return _read_run(run_id)


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    """Return runs sorted newest-first, summary view (no full logs)."""
    rows: list[dict[str, Any]] = []
    for path in RUNS_DIR.glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows.append(
            {
                "id": rec.get("id"),
                "status": rec.get("status"),
                "created_at": rec.get("created_at"),
                "finished_at": rec.get("finished_at"),
                "rounds": rec.get("rounds"),
                "per_round": rec.get("per_round"),
                "params": rec.get("params") or {},
                "current_round": rec.get("current_round"),
                "current_stage": rec.get("current_stage"),
                "summary": rec.get("summary"),
                "error": rec.get("error"),
            }
        )
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows[:limit]
