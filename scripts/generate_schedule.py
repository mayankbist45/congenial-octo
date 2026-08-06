#!/usr/bin/env python3
"""Generate cron and systemd fragments from config/markets/*.yaml.

Times are converted from market-local session bounds to the VM timezone
(default Asia/Kolkata). Re-run on deploy so US watch times track DST.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.config import PROJECT_ROOT
from src.market.registry import list_markets
from src.market.scheduling import enabled_intraday_personas


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def _to_vm_cron(dt: datetime, vm_tz: ZoneInfo) -> tuple[str, str, str]:
    """Return (minute, hour, dow) for cron in VM timezone."""
    local = dt.astimezone(vm_tz)
    minute = str(local.minute)
    hour = str(local.hour)
    # cron dow: 0=Sun; Python weekday Mon=0 -> cron Mon=1
    dow = str(local.weekday() + 1)
    return minute, hour, dow


def _cron_line(minute: str, hour: str, dow: str, command: str) -> str:
    return f"{minute} {hour} * * {dow} {command}"


def _next_weekday(day: date) -> date:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def generate_cron(project_dir: Path, vm_tz_name: str, app_user: str) -> str:
    vm_tz = ZoneInfo(vm_tz_name)
    markets = list_markets()
    lines = [
        f"# AI Mutual Fund Manager - generated schedule (VM TZ: {vm_tz_name})",
        "SHELL=/bin/bash",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "MAILTO=\"\"",
        "",
    ]

    # One swing cycle per market at market-local pre-open time.
    for market in markets:
        hour, minute = _parse_hhmm(market.scheduling.swing_cycle)
        sample_day = _next_weekday(date.today())
        market_tz = ZoneInfo(market.timezone)
        cycle_dt = datetime.combine(sample_day, datetime.min.time()).replace(
            hour=hour, minute=minute, tzinfo=market_tz
        )
        m, h, dow = _to_vm_cron(cycle_dt, vm_tz)
        lines.append(
            _cron_line(
                m,
                h,
                "1-5",
                f"{app_user} cd {project_dir} && ./scripts/run_scheduled.sh cycle --market {market.id}",
            )
        )

    # Intraday watch start/stop per market.
    for market in markets:
        watch = market.scheduling.intraday_watch
        if not watch or not watch.personas:
            continue
        sample_day = _next_weekday(date.today())
        cal_start, cal_stop = _watch_bounds(market, sample_day)
        start_m, start_h, start_dow = _to_vm_cron(cal_start, vm_tz)
        stop_m, stop_h, stop_dow = _to_vm_cron(cal_stop, vm_tz)
        service = f"mutualfund-{market.id.replace('_', '-')}-intraday-watch"
        lines.append(
            _cron_line(
                start_m,
                start_h,
                "1-5",
                f"root systemctl start {service}",
            )
        )
        # Stop may fall on next calendar day (US from IST VM).
        stop_dow_expr = _stop_dow_expr(cal_start, cal_stop, vm_tz)
        lines.append(
            _cron_line(
                stop_m,
                stop_h,
                stop_dow_expr,
                f"root systemctl stop {service}",
            )
        )

    # Shared close + backup (India-oriented close kept for all swing books).
    lines.append(
        _cron_line(
            "0",
            "18",
            "1-5",
            f"{app_user} cd {project_dir} && ./scripts/run_scheduled.sh close",
        )
    )
    lines.append(
        _cron_line(
            "30",
            "19",
            "1-5",
            f"{app_user} cd {project_dir} && ./scripts/backup_journal.sh >> data/backup.log 2>&1",
        )
    )
    return "\n".join(lines) + "\n"


def _watch_bounds(market, day: date) -> tuple[datetime, datetime]:
    from src.market.calendar import SessionCalendar

    cal = SessionCalendar(market)
    bounds = cal.watch_start_stop(day)
    if bounds is None:
        raise ValueError(f"No intraday watch schedule for {market.id}")
    return bounds


def _stop_dow_expr(start: datetime, stop: datetime, vm_tz: ZoneInfo) -> str:
    start_local = start.astimezone(vm_tz).date()
    stop_local = stop.astimezone(vm_tz).date()
    if stop_local > start_local:
        # e.g. US session stop after midnight IST -> Tue-Sat
        return "2-6"
    return "1-5"


def generate_systemd_unit(project_dir: Path, market_id: str, app_user: str) -> str:
    service = market_id.replace("_", "-")
    log_name = f"{service}.log"
    return f"""[Unit]
Description=Intraday paper watches for {market_id}
After=network.target

[Service]
Type=simple
User={app_user}
WorkingDirectory={project_dir}
ExecStart={project_dir}/scripts/run_market_intraday_watches.sh --market {market_id}
Restart=no
TimeoutStopSec=120
KillMode=mixed
StandardOutput=append:{project_dir}/data/{log_name}
StandardError=append:{project_dir}/data/{log_name}

[Install]
WantedBy=multi-user.target
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate market-aware cron/systemd")
    parser.add_argument("--vm-tz", default="Asia/Kolkata")
    parser.add_argument("--app-user", default="fund")
    parser.add_argument("--project-dir", default=str(PROJECT_ROOT))
    parser.add_argument("--cron-only", action="store_true")
    parser.add_argument("--systemd-only", action="store_true")
    parser.add_argument(
        "--market",
        metavar="ID",
        help="emit systemd unit for one market only (with --systemd-only)",
    )
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()

    if not args.systemd_only:
        print(generate_cron(project_dir, args.vm_tz, args.app_user))

    if not args.cron_only:
        markets = list_markets()
        if args.market:
            markets = [m for m in markets if m.id == args.market]
        for market in markets:
            if market.scheduling.intraday_watch and market.scheduling.intraday_watch.personas:
                if not args.market:
                    print(f"# systemd: mutualfund-{market.id.replace('_', '-')}-intraday-watch")
                print(generate_systemd_unit(project_dir, market.id, args.app_user))


if __name__ == "__main__":
    main()
