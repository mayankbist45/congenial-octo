#!/bin/bash
# Start all enabled intraday paper watches for one market (one subprocess per persona).

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

MARKET=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --market)
            MARKET="${2:-}"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

if [ -z "$MARKET" ]; then
    echo "Usage: run_market_intraday_watches.sh --market <market_id>" >&2
    exit 1
fi

PYTHON="$PROJECT_DIR/.venv/bin/python"
WATCH="$PROJECT_DIR/scripts/run_intraday_watch.sh"
LOG="$PROJECT_DIR/data/${MARKET//_/-}-intraday-watch.log"

mkdir -p "$PROJECT_DIR/data"

emit_event() {
    local status="$1"
    shift || true
    python3 -m src.operations emit \
        --log "$LOG" \
        --event "intraday_launcher" \
        --status "$status" \
        --field "market_id=$MARKET" \
        --field "source=run_market_intraday_watches.sh" \
        "$@"
}

{
    echo ""
    echo "=================================================================="
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z')  Intraday watch launcher ($MARKET)"
    echo "=================================================================="
} >>"$LOG"

if [ ! -x "$PYTHON" ] || [ ! -x "$WATCH" ]; then
    echo "FATAL: missing venv or run_intraday_watch.sh" >>"$LOG"
    emit_event "failure" --field "error=missing_runtime"
    exit 1
fi

mapfile -t PERSONAS < <(
    "$PYTHON" -c "
import sys
sys.path.insert(0, '$PROJECT_DIR')
from src.market.scheduling import enabled_intraday_personas
for pid in enabled_intraday_personas('$MARKET'):
    print(pid)
"
)

if [ "${#PERSONAS[@]}" -eq 0 ]; then
    echo "No enabled intraday personas for market $MARKET" >>"$LOG"
    emit_event "success" --field "persona_count=0" --field "state=no_personas_enabled"
    exit 0
fi

STAGGER_SECONDS="$(
    "$PYTHON" -c "
import sys
sys.path.insert(0, '$PROJECT_DIR')
from src.market.registry import get_market
m = get_market('$MARKET')
w = m.scheduling.intraday_watch if m else None
print(int(w.persona_start_stagger_sec) if w else 0)
" 2>/dev/null || echo 0
)"
# Allow override without editing yaml (e.g. INTRADAY_PERSONA_STAGGER_SECONDS=90).
if [ -n "${INTRADAY_PERSONA_STAGGER_SECONDS:-}" ]; then
    STAGGER_SECONDS="$INTRADAY_PERSONA_STAGGER_SECONDS"
fi

emit_event "start" \
    --field "persona_count=${#PERSONAS[@]}" \
    --field "persona_start_stagger_sec=${STAGGER_SECONDS}"

PIDS=()
idx=0
for persona in "${PERSONAS[@]}"; do
    if [ "$idx" -gt 0 ] && [ "${STAGGER_SECONDS}" -gt 0 ] 2>/dev/null; then
        echo "Stagger ${STAGGER_SECONDS}s before starting $persona" >>"$LOG"
        sleep "$STAGGER_SECONDS"
    fi
    echo "Starting watch: $persona" >>"$LOG"
    "$WATCH" "$persona" &
    PIDS+=("$!")
    idx=$((idx + 1))
done

FAIL=0
for pid in "${PIDS[@]}"; do
    if ! wait "$pid"; then
        FAIL=1
    fi
done

echo "--- $MARKET watch launcher done at $(date '+%H:%M:%S') exit=$FAIL ---" >>"$LOG"
if [ $FAIL -eq 0 ]; then
    emit_event "success" --field "persona_count=${#PERSONAS[@]}" --field "exit_code=0"
else
    emit_event "failure" --field "persona_count=${#PERSONAS[@]}" --field "exit_code=$FAIL"
fi
exit $FAIL
