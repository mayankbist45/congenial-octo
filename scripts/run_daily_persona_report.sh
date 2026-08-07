#!/bin/bash
# Cron wrapper: end-of-day persona digest → WhatsApp.

set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PYTHON="$PROJECT_DIR/.venv/bin/python"

{
    echo ""
    echo "=================================================================="
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z')  send_daily_persona_report $*"
    echo "=================================================================="
} >>"$PROJECT_DIR/data/persona_report.log"

if [ ! -x "$PYTHON" ]; then
    echo "FATAL: missing venv at $PYTHON" >>"$PROJECT_DIR/data/persona_report.log"
    exit 1
fi

exec "$PYTHON" "$PROJECT_DIR/scripts/send_daily_persona_report.py" "$@"
