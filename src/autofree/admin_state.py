"""Master account login state — session_token / account_id / workspace_name / email.

Persisted to data/admin_state.json. session_token is sensitive — file is rw owner-only.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from autofree.config import ADMIN_STATE_FILE

_LOCK = threading.RLock()


def _read() -> dict[str, Any]:
    if not ADMIN_STATE_FILE.is_file():
        return {}
    try:
        return json.loads(ADMIN_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(data: dict[str, Any]) -> None:
    tmp = ADMIN_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(ADMIN_STATE_FILE)


def get_state() -> dict[str, Any]:
    with _LOCK:
        return _read()


def update_state(**fields: Any) -> dict[str, Any]:
    with _LOCK:
        data = _read()
        for k, v in fields.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        _write(data)
        return data


def clear_state() -> None:
    with _LOCK:
        if ADMIN_STATE_FILE.exists():
            ADMIN_STATE_FILE.unlink()


def get_session_token() -> str:
    return get_state().get("session_token") or ""


def get_account_id() -> str:
    return get_state().get("account_id") or ""


def get_email() -> str:
    return get_state().get("email") or ""


def get_workspace_name() -> str:
    return get_state().get("workspace_name") or ""


def get_summary() -> dict[str, Any]:
    """Public summary safe to send to the web UI (no token)."""
    s = get_state()
    token = s.get("session_token") or ""
    return {
        "email": s.get("email") or "",
        "account_id": s.get("account_id") or "",
        "workspace_name": s.get("workspace_name") or "",
        "has_session_token": bool(token),
        "session_token_len": len(token),
        "updated_at": s.get("updated_at") or "",
    }
