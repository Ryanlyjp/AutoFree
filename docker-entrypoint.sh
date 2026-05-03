#!/bin/bash
# Docker entrypoint — start virtual X server, ensure data dirs, then run autofree.
set -e

# 1. Virtual X (Playwright Chromium needs $DISPLAY even in headless mode for some shaders)
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x800x24 &
export DISPLAY=:99

# 2. Make sure host-mounted /app/data has all subdirs the app expects.
mkdir -p /app/data/auths /app/data/runs /app/data/logs
chmod -R 700 /app/data || true

# 3. Quick fail-fast: generate API key if .env is missing.
if [ ! -f /app/data/.env ]; then
    echo "[entrypoint] /app/data/.env missing — generating with random key" >&2
    KEY="$(head -c 16 /dev/urandom | xxd -p)"
    echo "AUTOFREE_API_KEY=${KEY}" > /app/data/.env
    echo "[entrypoint] AUTOFREE_API_KEY=${KEY}" >&2
fi

# 4. Self-check critical imports — crash-loop early if image is broken.
echo "[self-check] verifying critical imports..."
SELFCHECK='
import sys
from autofree.api import app
from autofree.runner import start_run, run_blocking, cancel_run
from autofree.master import import_session_token, set_access_token, diagnose
from autofree.mail import get_mail_client
from autofree.flow import Flow
print("[self-check] ok")
'
if ! uv run python -c "$SELFCHECK"; then
    echo "[self-check] FATAL: critical import failed — image is stale or broken." >&2
    echo "[self-check] Rebuild: docker compose build --no-cache && docker compose up -d" >&2
    exit 1
fi

# 5. Hand off to autofree
exec uv run autofree "$@"
