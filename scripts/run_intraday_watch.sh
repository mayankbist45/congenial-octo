#!/bin/bash
# Live intraday paper watch for one persona. Started by cron/systemd on weekdays.

set -uo pipefail

PERSONA="${1:?usage: run_intraday_watch.sh <persona_id>}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG="$PROJECT_DIR/data/${PERSONA}_watch.log"
mkdir -p "$PROJECT_DIR/data"

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/homebrew/bin"

emit_event() {
    local status="$1"
    shift || true
    python3 -m src.operations emit \
        --log "$LOG" \
        --event "intraday_watch" \
        --status "$status" \
        --field "persona_id=$PERSONA" \
        --field "source=run_intraday_watch.sh" \
        "$@"
}

{
    echo ""
    echo "=================================================================="
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z')  run_daily.py watch --persona ${PERSONA}"
    echo "=================================================================="
} >>"$LOG"

if [ ! -x "$PYTHON" ]; then
    echo "FATAL: no virtualenv at $PYTHON" >>"$LOG"
    emit_event "failure" --field "error=no_virtualenv"
    exit 1
fi

MARKET="$("$PYTHON" - <<PY
from src.config import get_persona_config
print(get_persona_config("${PERSONA}").market_id)
PY
)"
emit_event "start" --field "market_id=$MARKET"

# Skip weekends in the persona's exchange timezone (IST for India, ET for US).
if ! "$PYTHON" - <<PY >>"$LOG" 2>&1
from datetime import datetime
from src.config import get_persona_config
from src.market.session import market_timezone

pcfg = get_persona_config("${PERSONA}")
now = datetime.now(market_timezone(pcfg))
if now.weekday() >= 5:
    print(f"Skipping ${PERSONA} watch: exchange weekend ({now.date()})")
    raise SystemExit(0)
PY
then
    emit_event "success" --field "market_id=$MARKET" --field "state=skipped_weekend" --field "exit_code=0"
    exit 0
fi

"$PYTHON" run_daily.py watch --persona "$PERSONA" >>"$LOG" 2>&1
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo "--- finished cleanly at $(date '+%H:%M:%S') ---" >>"$LOG"
    emit_event "success" --field "market_id=$MARKET" --field "state=stopped" --field "exit_code=0"
else
    echo "--- FAILED with exit $STATUS at $(date '+%H:%M:%S') ---" >>"$LOG"
    emit_event "failure" --field "market_id=$MARKET" --field "exit_code=$STATUS"
fi

exit $STATUS
