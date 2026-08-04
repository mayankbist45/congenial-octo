#!/bin/bash
# Start all enabled US intraday paper watches (one subprocess per persona).

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

PYTHON="$PROJECT_DIR/.venv/bin/python"
WATCH="$PROJECT_DIR/scripts/run_intraday_watch.sh"
LOG="$PROJECT_DIR/data/us_intraday_watch.log"

US_PERSONAS=(calan_us revert_us flux_us spread_us slice_us)

mkdir -p "$PROJECT_DIR/data"

{
    echo ""
    echo "=================================================================="
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z')  US intraday watch launcher"
    echo "=================================================================="
} >>"$LOG"

if [ ! -x "$PYTHON" ] || [ ! -x "$WATCH" ]; then
    echo "FATAL: missing venv or run_intraday_watch.sh" >>"$LOG"
    exit 1
fi

PIDS=()
for persona in "${US_PERSONAS[@]}"; do
    echo "Starting watch: $persona" >>"$LOG"
    "$WATCH" "$persona" &
    PIDS+=("$!")
done

FAIL=0
for pid in "${PIDS[@]}"; do
    if ! wait "$pid"; then
        FAIL=1
    fi
done

echo "--- US watch launcher done at $(date '+%H:%M:%S') exit=$FAIL ---" >>"$LOG"
exit $FAIL
