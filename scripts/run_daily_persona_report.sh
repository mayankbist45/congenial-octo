#!/bin/bash
# Cron wrapper: end-of-day persona digest → WhatsApp.

set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG="$PROJECT_DIR/data/persona_report.log"

{
    echo ""
    echo "=================================================================="
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z')  send_daily_persona_report $*"
    echo "=================================================================="
} >>"$LOG"

emit_event() {
    local status="$1"
    shift || true
    if [ "$#" -gt 0 ]; then
        python3 -m src.operations emit \
            --log "$LOG" \
            --event "whatsapp_digest" \
            --status "$status" \
            --field "market=india_equity" \
            --field "source=run_daily_persona_report.sh" \
            --field "$1"
    else
        python3 -m src.operations emit \
            --log "$LOG" \
            --event "whatsapp_digest" \
            --status "$status" \
            --field "market=india_equity" \
            --field "source=run_daily_persona_report.sh"
    fi
}

if [ ! -x "$PYTHON" ]; then
    echo "FATAL: missing venv at $PYTHON" >>"$LOG"
    emit_event "failure" "error=no_virtualenv"
    exit 1
fi

emit_event "start"
"$PYTHON" "$PROJECT_DIR/scripts/send_daily_persona_report.py" "$@" >>"$LOG" 2>&1
STATUS=$?
if [ $STATUS -eq 0 ]; then
    emit_event "success" "exit_code=0"
else
    emit_event "failure" "exit_code=$STATUS"
fi
exit $STATUS
