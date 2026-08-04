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

{
    echo ""
    echo "=================================================================="
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z')  run_daily.py watch --persona ${PERSONA}"
    echo "=================================================================="
} >>"$LOG"

if [ ! -x "$PYTHON" ]; then
    echo "FATAL: no virtualenv at $PYTHON" >>"$LOG"
    exit 1
fi

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
    exit 0
fi

"$PYTHON" run_daily.py watch --persona "$PERSONA" >>"$LOG" 2>&1
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo "--- finished cleanly at $(date '+%H:%M:%S') ---" >>"$LOG"
else
    echo "--- FAILED with exit $STATUS at $(date '+%H:%M:%S') ---" >>"$LOG"
fi

exit $STATUS
