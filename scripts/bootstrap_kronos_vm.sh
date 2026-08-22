#!/bin/bash
# Bootstrap a GPU-capable Debian VM for Kronos inference validation.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
APP_USER=""
[ "${1:-}" = "--as-user" ] && APP_USER="${2:-}"
[ -n "$APP_USER" ] || APP_USER="${SUDO_USER:-$(id -un)}"

KRONOS_REPO_URL="${KRONOS_REPO_URL:-https://github.com/shiyu-coder/Kronos.git}"
KRONOS_REPO_DIR="${KRONOS_REPO_DIR:-$PROJECT_DIR/Kronos}"
TZ_NAME="${FUND_TIMEZONE:-America/New_York}"

[ "$(id -u)" = "0" ] || {
    echo "error: run me with sudo" >&2
    exit 1
}
id "$APP_USER" >/dev/null 2>&1 || {
    echo "error: no such user: $APP_USER" >&2
    exit 1
}

as_app() { runuser -u "$APP_USER" -- "$@"; }

echo "Bootstrapping Kronos VM at $PROJECT_DIR for $APP_USER"

timedatectl set-timezone "$TZ_NAME"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-dev build-essential git curl wget \
    ca-certificates pciutils >/dev/null

if lspci | grep -qi nvidia; then
    apt-get install -y -qq nvidia-driver firmware-misc-nonfree >/dev/null || true
fi

if [ ! -d "$KRONOS_REPO_DIR/.git" ]; then
    as_app git clone "$KRONOS_REPO_URL" "$KRONOS_REPO_DIR"
else
    as_app git -C "$KRONOS_REPO_DIR" pull --ff-only || true
fi

if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
    as_app python3 -m venv "$PROJECT_DIR/.venv"
fi

as_app "$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip
as_app "$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"
as_app "$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$KRONOS_REPO_DIR/requirements.txt"

mkdir -p "$PROJECT_DIR/data/cache/huggingface" "$PROJECT_DIR/data/logs/personas" "$PROJECT_DIR/data/kronos"
chown -R "$APP_USER" "$PROJECT_DIR/data"

if [ -f "$ENV_FILE" ]; then
    chown "$APP_USER" "$ENV_FILE" || true
fi

if command -v nvidia-smi >/dev/null 2>&1; then
    echo "GPU status:"
    nvidia-smi || true
else
    echo "note: nvidia-smi not available yet; reboot may be required after driver install"
fi

echo "Kronos VM bootstrap complete"
