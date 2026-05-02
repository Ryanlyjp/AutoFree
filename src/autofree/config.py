"""Static paths + .env loader. Runtime-editable settings live in settings.py."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- paths

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
AUTHS_DIR = DATA_DIR / "auths"
RUNS_DIR = DATA_DIR / "runs"
LOGS_DIR = DATA_DIR / "logs"
SETTINGS_FILE = DATA_DIR / "settings.json"
ADMIN_STATE_FILE = DATA_DIR / "admin_state.json"
ENV_FILE = DATA_DIR / ".env"

for d in (DATA_DIR, AUTHS_DIR, RUNS_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- .env loader


def _load_env_file(path: Path) -> None:
    """Minimal .env loader. Lines like KEY=value, # comments, no expansion."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


# Load data/.env first (highest), then project-root .env (fallback).
_load_env_file(ENV_FILE)
_load_env_file(PROJECT_ROOT / ".env")


# ---------------------------------------------------------------- env-only

# Bearer key for the FastAPI backend. Required.
def get_api_key() -> str:
    return os.environ.get("AUTOFREE_API_KEY") or ""


# ---------------------------------------------------------------- timing

# OTP polling defaults — can be overridden per call.
EMAIL_POLL_INTERVAL = float(os.environ.get("EMAIL_POLL_INTERVAL", "3"))
EMAIL_POLL_TIMEOUT = int(os.environ.get("EMAIL_POLL_TIMEOUT", "120"))

# Wait between auto_provision toggle and OAuth in run_round (seconds).
AP_PROPAGATION_DELAY = float(os.environ.get("AP_PROPAGATION_DELAY", "5"))
