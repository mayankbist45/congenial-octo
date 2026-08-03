#!/bin/bash
# Market-hours Calan paper watch. Started by cron/systemd on weekdays;
# exits on its own around force_exit_time (default 15:15 IST).

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG="$PROJECT_DIR/data/calan_watch.log"
mkdir -p "$PROJECT_DIR/data"

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/homebrew/bin"

{
    echo ""
    echo "=================================================================="
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z')  run_daily.py watch --persona calan"
    echo "=================================================================="
} >>"$LOG"

if [ ! -x "$PYTHON" ]; then
    echo "FATAL: no virtualenv at $PYTHON" >>"$LOG"
    exit 1
fi

# Skip quietly on weekends / outside a sensible start window if launched by hand.
DOW="$(date '+%u')"  # 1=Mon .. 7=Sun
HOUR="$(date '+%H')"
if [ "$DOW" -gt 5 ]; then
    echo "Skipping Calan watch: weekend" >>"$LOG"
    exit 0
fi
if [ "$HOUR" -lt 9 ] || [ "$HOUR" -ge 16 ]; then
    echo "Skipping Calan watch: outside market hours (hour=$HOUR)" >>"$LOG"
    exit 0
fi

"$PYTHON" run_daily.py watch --persona calan >>"$LOG" 2>&1
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo "--- finished cleanly at $(date '+%H:%M:%S') ---" >>"$LOG"
else
    echo "--- FAILED with exit $STATUS at $(date '+%H:%M:%S') ---" >>"$LOG"
fi

exit $STATUS
