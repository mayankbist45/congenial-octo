#!/bin/bash
# Wrapper used by launchd to run the fund on a schedule.
#
# launchd gives a job almost no environment, so everything here is absolute:
# the project directory, the virtualenv interpreter, and a PATH that can still
# find Homebrew's PostgreSQL client libraries. Output is appended to one log so
# a week of unattended runs reads as a single story.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

COMMAND="${1:-daily_monitor}"
shift || true

PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG="$PROJECT_DIR/data/scheduled.log"
mkdir -p "$PROJECT_DIR/data"

export PATH="/opt/homebrew/bin:/opt/homebrew/opt/postgresql@17/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

{
    echo ""
    echo "=================================================================="
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z')  run_daily.py $COMMAND $*"
    echo "=================================================================="
} >>"$LOG"

if [ ! -x "$PYTHON" ]; then
    echo "FATAL: no virtualenv at $PYTHON - run: python3 -m venv .venv && pip install -r requirements.txt" >>"$LOG"
    exit 1
fi

"$PYTHON" run_daily.py "$COMMAND" "$@" >>"$LOG" 2>&1
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo "--- finished cleanly at $(date '+%H:%M:%S') ---" >>"$LOG"
else
    echo "--- FAILED with exit $STATUS at $(date '+%H:%M:%S') ---" >>"$LOG"
fi

exit $STATUS
