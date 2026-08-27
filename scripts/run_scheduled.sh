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

emit_event() {
    local status="$1"
    shift || true
    local fields=()
    for field in "$@"; do fields+=(--field "$field"); done
    python3 -m src.operations emit \
        --log "$LOG" \
        --event "scheduled_run" \
        --status "$status" \
        --field "job=$COMMAND" "${fields[@]}" \
        --field "source=run_scheduled.sh"
}

control_for_command() {
    case "$1" in
        weekly_research|close) echo "$1" ;;
        *) echo "" ;;
    esac
}

run_persona_report() {
    local report_log="$PROJECT_DIR/data/persona_report.log"
    "$PROJECT_DIR/scripts/run_daily_persona_report.sh" --market india_equity >>"$report_log" 2>&1
    local report_status=$?
    if [ $report_status -eq 0 ]; then
        echo "--- India WhatsApp digest sent at $(date '+%H:%M:%S') ---" >>"$LOG"
    else
        # The close is complete even if a downstream notification provider fails.
        echo "--- India WhatsApp digest failed with exit $report_status at $(date '+%H:%M:%S') ---" >>"$LOG"
    fi
}

{
    echo ""
    echo "=================================================================="
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z')  run_daily.py $COMMAND $*"
    echo "=================================================================="
} >>"$LOG"

if [ ! -x "$PYTHON" ]; then
    echo "FATAL: no virtualenv at $PYTHON - run: python3 -m venv .venv && pip install -r requirements.txt" >>"$LOG"
    emit_event "failure" "error=no_virtualenv"
    exit 1
fi

CONTROL="$(control_for_command "$COMMAND")"
if [ -n "$CONTROL" ] && [ "$("$PYTHON" -m src.scheduler_controls enabled "$CONTROL")" != "1" ]; then
    echo "--- skipped: $CONTROL is disabled in Operations ---" >>"$LOG"
    emit_event "skipped" "control=$CONTROL" "state=disabled"
    exit 0
fi
if [ "$COMMAND" = "long_cycle" ]; then
    INDIA_ENABLED="$("$PYTHON" -m src.scheduler_controls enabled india_long_cycle)"
    US_ENABLED="$("$PYTHON" -m src.scheduler_controls enabled us_long_cycle)"
    [ "$INDIA_ENABLED" = "1" ] || emit_event "skipped" "control=india_long_cycle" "state=disabled"
    [ "$US_ENABLED" = "1" ] || emit_event "skipped" "control=us_long_cycle" "state=disabled"
    if [ "$INDIA_ENABLED" != "1" ] && [ "$US_ENABLED" != "1" ]; then
        echo "--- skipped: all long-cycle markets are disabled in Operations ---" >>"$LOG"
        exit 0
    fi
fi

emit_event "start"
FUND_SCHEDULED_RUN=1 "$PYTHON" run_daily.py "$COMMAND" "$@" >>"$LOG" 2>&1
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo "--- finished cleanly at $(date '+%H:%M:%S') ---" >>"$LOG"
    emit_event "success" "exit_code=0"
    if [ "$COMMAND" = "close" ]; then
        "$PYTHON" run_daily.py publish_dashboard --refresh-portfolio >>"$LOG" 2>&1
        SNAPSHOT_STATUS=$?
        if [ $SNAPSHOT_STATUS -eq 0 ]; then
            emit_event "success" "job=publish_dashboard" "exit_code=0"
            run_persona_report
        else
            echo "--- dashboard snapshot failed with exit $SNAPSHOT_STATUS at $(date '+%H:%M:%S') ---" >>"$LOG"
            emit_event "failure" "job=publish_dashboard" "exit_code=$SNAPSHOT_STATUS"
        fi
    fi
else
    echo "--- FAILED with exit $STATUS at $(date '+%H:%M:%S') ---" >>"$LOG"
    emit_event "failure" "exit_code=$STATUS"
fi

exit $STATUS
