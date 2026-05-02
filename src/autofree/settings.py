"""Runtime-editable settings persisted to data/settings.json.

Web UI reads/writes this; .env values are seed defaults the first time.
Anything in settings.json wins over the environment.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from autofree.config import SETTINGS_FILE

_LOCK = threading.RLock()


def _seed_from_env() -> dict[str, Any]:
    return {
        "proxy": os.environ.get("HTTP_PROXY", ""),
        "mail": {
            "provider": os.environ.get("MAIL_PROVIDER", "tempmail"),
            "cf_temp_email": {
                "base_url": os.environ.get("CLOUDMAIL_BASE_URL", ""),
                "password": os.environ.get("CLOUDMAIL_PASSWORD", ""),
                "domain": os.environ.get("CLOUDMAIL_DOMAIN", ""),
            },
            "maillab": {
                "api_url": os.environ.get("MAILLAB_API_URL", ""),
                "username": os.environ.get("MAILLAB_USERNAME", ""),
                "password": os.environ.get("MAILLAB_PASSWORD", ""),
                "domain": os.environ.get("MAILLAB_DOMAIN", ""),
            },
            "tempmail": {
                "base_url": os.environ.get("TEMPMAIL_BASE_URL", ""),
                "api_key": os.environ.get("TEMPMAIL_API_KEY", ""),
                "domain": os.environ.get("TEMPMAIL_DOMAIN", ""),
            },
        },
        "cpa": {
            "base_url": os.environ.get("CPA_BASE_URL", ""),
            "key": os.environ.get("CPA_KEY", ""),
        },
    }


def _read() -> dict[str, Any]:
    if not SETTINGS_FILE.is_file():
        seeded = _seed_from_env()
        _write(seeded)
        return seeded
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        # corrupt file — back up and reseed
        backup = SETTINGS_FILE.with_suffix(".json.bak")
        SETTINGS_FILE.rename(backup)
        seeded = _seed_from_env()
        _write(seeded)
        return seeded


def _write(data: dict[str, Any]) -> None:
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def get_all() -> dict[str, Any]:
    with _LOCK:
        return _read()


def update(patch: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge `patch` into stored settings and persist."""
    with _LOCK:
        data = _read()
        _deep_merge(data, patch)
        _write(data)
        return data


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


# ------------------------------------------------------------ typed accessors


def get_proxy() -> str:
    return get_all().get("proxy") or ""


def set_proxy(value: str) -> None:
    update({"proxy": (value or "").strip()})


def get_mail_provider() -> str:
    return (get_all().get("mail") or {}).get("provider") or "tempmail"


def get_mail_config(provider: str | None = None) -> dict[str, Any]:
    mail = get_all().get("mail") or {}
    name = provider or mail.get("provider") or "tempmail"
    return (mail.get(name) or {}).copy()


def get_cpa_config() -> dict[str, Any]:
    return (get_all().get("cpa") or {}).copy()
