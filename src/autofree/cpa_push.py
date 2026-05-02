"""Push free-account auth.json files to CLIProxyAPI.

Add-only design — never deletes or overwrites a CPA file unless the user
explicitly forces it. CPA filename uses a distinct prefix so our files
cannot collide with whatever the existing AutoTeam pipeline writes there.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

from autofree import storage
from autofree.settings import get_cpa_config

logger = logging.getLogger(__name__)


# Distinct prefix so we never overwrite Team-pipeline files in the same CPA.
CPA_FILE_PREFIX = "codex-free-"


class CPAError(Exception):
    pass


# ------------------------------------------------------------ http


def _config() -> tuple[str, str]:
    cfg = get_cpa_config()
    base = (cfg.get("base_url") or "").rstrip("/")
    key = cfg.get("key") or ""
    if not base or not key:
        raise CPAError("CPA 未配置 base_url / key")
    return base, key


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _cpa_filename(email: str) -> str:
    safe = (email or "").strip().replace("/", "_").replace("\\", "_")
    return f"{CPA_FILE_PREFIX}{safe}.json"


# ------------------------------------------------------------ public ops


def list_remote() -> list[dict[str, Any]]:
    """GET /v0/management/auth-files — returns the full CPA file list (all files,
    not just ours). Caller can filter by `CPA_FILE_PREFIX` if needed."""
    base, key = _config()
    r = requests.get(f"{base}/v0/management/auth-files", headers=_headers(key), timeout=10)
    if r.status_code != 200:
        raise CPAError(f"list_remote HTTP {r.status_code}: {(r.text or '')[:200]}")
    data = r.json() or {}
    return data.get("files") or []


def push_one(email: str, *, overwrite: bool = False) -> dict[str, Any]:
    """Push the local auth.json for `email` to CPA.

    `overwrite=False` (default) refuses to push if a file with the same
    target name already exists in CPA — a deliberate guard against
    clobbering an Team-pipeline file. Set True to delete-then-upload.
    """
    base, key = _config()
    auth = storage.load_auth(email)
    if not auth:
        raise CPAError(f"未找到本地 auth: {email}")
    path = storage.auth_path(email)
    if not path.is_file():
        raise CPAError(f"auth file 缺失: {path}")

    target_name = _cpa_filename(email)

    # Pre-check: collision detection.
    existing_names = {f.get("name") for f in list_remote() if isinstance(f, dict)}
    if target_name in existing_names and not overwrite:
        return {
            "ok": False,
            "skipped": True,
            "reason": "already_exists",
            "name": target_name,
        }
    if target_name in existing_names and overwrite:
        _delete_remote(base, key, target_name)

    # Upload as multipart with our chosen filename.
    with open(path, "rb") as f:
        r = requests.post(
            f"{base}/v0/management/auth-files",
            headers=_headers(key),
            files={"file": (target_name, f, "application/json")},
            timeout=15,
        )
    if r.status_code != 200:
        raise CPAError(f"upload HTTP {r.status_code}: {(r.text or '')[:200]}")

    storage.mark_pushed(email)
    logger.info("[CPA] 已推送: %s", target_name)
    return {"ok": True, "skipped": False, "name": target_name}


def push_many(emails: list[str], *, overwrite: bool = False) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    pushed = skipped = failed = 0
    for email in emails:
        try:
            res = push_one(email, overwrite=overwrite)
        except Exception as exc:
            res = {"ok": False, "skipped": False, "error": str(exc), "email": email}
            failed += 1
            results.append(res)
            continue
        res["email"] = email
        results.append(res)
        if res.get("ok"):
            pushed += 1
        elif res.get("skipped"):
            skipped += 1
        else:
            failed += 1
    return {
        "pushed": pushed,
        "skipped": skipped,
        "failed": failed,
        "total": len(emails),
        "results": results,
    }


def probe() -> dict[str, Any]:
    """Cheap connectivity check — list_remote + count files."""
    files = list_remote()
    ours = [f for f in files if isinstance(f, dict) and (f.get("name") or "").startswith(CPA_FILE_PREFIX)]
    return {"ok": True, "total_files": len(files), "our_files": len(ours)}


def _delete_remote(base: str, key: str, name: str) -> None:
    r = requests.delete(
        f"{base}/v0/management/auth-files",
        headers=_headers(key),
        params={"name": name},
        timeout=10,
    )
    if r.status_code != 200:
        raise CPAError(f"delete HTTP {r.status_code}: {(r.text or '')[:200]}")


# ------------------------------------------------------------ build auth payload


def build_auth_payload(
    email: str,
    tokens: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shape OAuth-token response into the auth.json schema used by Codex CLI / CPA.

    `tokens` must include access_token + refresh_token; id_token is optional.
    Extracts chatgpt_account_id from the JWT, computes expired/last_refresh.
    """
    import base64
    from datetime import datetime, timedelta, timezone

    access_token = tokens.get("access_token") or ""
    refresh_token = tokens.get("refresh_token") or ""
    id_token = tokens.get("id_token") or ""

    if not access_token or not refresh_token:
        raise CPAError("build_auth_payload: tokens 缺 access_token 或 refresh_token")

    # Decode JWT payload (best-effort)
    payload: dict = {}
    try:
        seg = access_token.split(".")[1]
        seg += "=" * ((4 - len(seg) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        pass

    auth_info = payload.get("https://api.openai.com/auth") or {}
    exp = payload.get("exp")
    expired_iso = ""
    if isinstance(exp, int) and exp > 0:
        expired_iso = (
            datetime.fromtimestamp(exp, tz=timezone(timedelta(hours=8)))
            .strftime("%Y-%m-%dT%H:%M:%S+08:00")
        )

    now_iso = (
        datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    )

    out: dict[str, Any] = {
        "type": "codex",
        "email": email,
        "expired": expired_iso,
        "id_token": id_token,
        "account_id": auth_info.get("chatgpt_account_id", ""),
        "access_token": access_token,
        "last_refresh": now_iso,
        "refresh_token": refresh_token,
    }
    if extra:
        out.update(extra)
    return out


def save_and_register(email: str, tokens: dict[str, Any], *, extra: dict[str, Any] | None = None) -> Path:
    """Save the OAuth tokens as data/auths/{email}.json (no CPA push).
    Caller decides when to push via push_one()."""
    payload = build_auth_payload(email, tokens, extra=extra)
    return storage.save_auth(email, payload)
