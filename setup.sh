#!/bin/bash
# One-shot installer for AutoFree.
# Idempotent — safe to re-run after a `git pull`.
set -e

cd "$(dirname "$0")"

# ---- 1. system deps (Linux only) ----
if [[ "$(uname)" == "Linux" ]]; then
    if ! command -v Xvfb &> /dev/null; then
        echo "[setup] installing xvfb (needed for headless Linux)..."
        if command -v sudo &> /dev/null; then
            sudo apt-get update && sudo apt-get install -y xvfb fonts-noto-cjk
        else
            apt-get update && apt-get install -y xvfb fonts-noto-cjk
        fi
    fi
fi

# ---- 2. uv ----
if ! command -v uv &> /dev/null; then
    echo "[setup] installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# ---- 3. python deps ----
echo "[setup] uv sync..."
uv sync

# ---- 4. Playwright browsers (REQUIRED — without this `autofree run` will crash) ----
echo "[setup] installing Playwright Chromium + system deps..."
uv run playwright install chromium
if [[ "$(uname)" == "Linux" ]]; then
    uv run playwright install-deps chromium || true   # may need root; non-fatal
fi

# ---- 5. .env ----
mkdir -p data
if [ ! -f data/.env ]; then
    if command -v openssl &> /dev/null; then
        KEY="$(openssl rand -hex 16)"
    else
        KEY="$(head -c 16 /dev/urandom | xxd -p)"
    fi
    cat > data/.env <<EOF
# AutoFree config — only AUTOFREE_API_KEY is mandatory here.
# All other settings live in data/settings.json (web-editable).
AUTOFREE_API_KEY=${KEY}
EOF
    echo "[setup] generated data/.env with random API key"
fi

# ---- 6. frontend (best-effort) ----
if command -v npm &> /dev/null; then
    if [ ! -d src/autofree/web/dist ] || [ -z "$(ls src/autofree/web/dist 2>/dev/null)" ]; then
        echo "[setup] building frontend..."
        (cd web && npm install && npm run build)
    fi
else
    echo "[setup] WARNING: npm not found — frontend will fall back to JSON placeholder."
    echo "[setup]          install Node.js then run: cd web && npm install && npm run build"
fi

cat <<EOF

============================================================
✅  AutoFree 安装完成

API key: $(grep ^AUTOFREE_API_KEY data/.env | cut -d= -f2)

启动:
    uv run autofree api               # http://0.0.0.0:8788

调试:
    uv run autofree status            # 看母号 + 已生产 auth
    uv run autofree run -R 1 -n 1     # 阻塞跑一轮一个号

文档:
    README.md                         # 顶层介绍
    docs/getting-started.md           # 7 步从零跑通
    docs/troubleshooting.md           # 报错排查
============================================================
EOF
