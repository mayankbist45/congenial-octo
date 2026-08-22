from __future__ import annotations

from pathlib import Path

from scripts.generate_schedule import generate_cron


def test_generated_schedule_runs_long_cycle_without_replacing_intraday_watches():
    project_dir = Path("/opt/fund")
    cron = generate_cron(project_dir, "Asia/Kolkata", "fund")

    assert "./scripts/run_scheduled.sh long_cycle" in cron
    assert "./scripts/run_scheduled.sh daily_monitor" not in cron
    assert "mutualfund-india-equity-intraday-watch" in cron
