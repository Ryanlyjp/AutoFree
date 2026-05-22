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

PROXY_MASTER_MODE_FOLLOW = "follow_proxy"
PROXY_MASTER_MODE_DIRECT = "direct"
_PROXY_MASTER_MODES = {PROXY_MASTER_MODE_FOLLOW, PROXY_MASTER_MODE_DIRECT}

EASYPROXY_MASTER_MODE_DIRECT = "direct"
EASYPROXY_MASTER_MODE_POOL = "follow_pool"
_EASYPROXY_MASTER_MODES = {EASYPROXY_MASTER_MODE_DIRECT, EASYPROXY_MASTER_MODE_POOL}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _seed_from_env() -> dict[str, Any]:
    return {
        "proxy": os.environ.get("HTTP_PROXY", ""),
        "proxy_master_mode": os.environ.get("MASTER_PROXY_MODE", PROXY_MASTER_MODE_FOLLOW),
        "easyproxy": {
            "enabled": False,
            "management_url": os.environ.get("EASYPROXY_MANAGEMENT_URL", "http://127.0.0.1:9888"),
            "password": os.environ.get("EASYPROXY_PASSWORD", ""),
            "proxy_host": os.environ.get("EASYPROXY_PROXY_HOST", "127.0.0.1"),
            "pool_port": _env_int("EASYPROXY_POOL_PORT", 2323),
            "port_min": _env_int("EASYPROXY_PORT_MIN", 24000),
            "port_max": _env_int("EASYPROXY_PORT_MAX", 24100),
            "cooldown_minutes": _env_int("EASYPROXY_COOLDOWN_MINUTES", 60),
            "master_mode": os.environ.get("EASYPROXY_MASTER_MODE", EASYPROXY_MASTER_MODE_DIRECT),
            "local_blacklist": {},
        },
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


def get_proxy_master_mode() -> str:
    mode = str(get_all().get("proxy_master_mode") or PROXY_MASTER_MODE_FOLLOW).strip().lower()
    if mode not in _PROXY_MASTER_MODES:
        return PROXY_MASTER_MODE_FOLLOW
    return mode


def get_easyproxy_config() -> dict[str, Any]:
    data = (get_all().get("easyproxy") or {}).copy()
    master_mode = str(data.get("master_mode") or EASYPROXY_MASTER_MODE_DIRECT).strip().lower()
    if master_mode not in _EASYPROXY_MASTER_MODES:
        master_mode = EASYPROXY_MASTER_MODE_DIRECT
    return {
        "enabled": bool(data.get("enabled")),
        "management_url": str(data.get("management_url") or "http://127.0.0.1:9888").strip(),
        "password": str(data.get("password") or ""),
        "proxy_host": str(data.get("proxy_host") or "127.0.0.1").strip() or "127.0.0.1",
        "pool_port": _coerce_int(data.get("pool_port"), 2323),
        "port_min": _coerce_int(data.get("port_min"), 24000),
        "port_max": _coerce_int(data.get("port_max"), 24100),
        "cooldown_minutes": _coerce_int(data.get("cooldown_minutes"), 60),
        "master_mode": master_mode,
        "local_blacklist": dict(data.get("local_blacklist") or {}),
    }


def easyproxy_enabled() -> bool:
    return bool(get_easyproxy_config().get("enabled"))


def get_master_proxy_url() -> str:
    easyproxy = get_easyproxy_config()
    if easyproxy.get("enabled"):
        if easyproxy.get("master_mode") == EASYPROXY_MASTER_MODE_POOL:
            host = easyproxy.get("proxy_host") or "127.0.0.1"
            port = int(easyproxy.get("pool_port") or 2323)
            return f"http://{host}:{port}"
        return ""
    if get_proxy_master_mode() == PROXY_MASTER_MODE_FOLLOW:
        return get_all().get("proxy") or ""
    return ""


def get_mail_provider() -> str:
    return (get_all().get("mail") or {}).get("provider") or "tempmail"


def get_mail_config(provider: str | None = None) -> dict[str, Any]:
    mail = get_all().get("mail") or {}
    name = provider or mail.get("provider") or "tempmail"
    return (mail.get(name) or {}).copy()


def get_cpa_config() -> dict[str, Any]:
    return (get_all().get("cpa") or {}).copy()
