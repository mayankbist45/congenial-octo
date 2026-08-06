"""Exchange session boundaries by market region.

Legacy shim — prefer :class:`src.market.calendar.SessionCalendar`.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from src.config import Config
from src.market.calendar import calendar_for_config


def market_timezone(config: Config) -> ZoneInfo:
    return calendar_for_config(config).tz


def session_open_close(config: Config) -> Tuple[time, time]:
    """Regular cash session open and close (local exchange time)."""
    return calendar_for_config(config).open_close()


def session_end_time(config: Config) -> time:
    return calendar_for_config(config).close_time()


def session_window(
    config: Config,
    day: date,
    now: Optional[datetime] = None,
) -> Optional[Tuple[datetime, datetime]]:
    """Return timezone-aware session bounds for ``day``, clipped to ``now``."""
    return calendar_for_config(config).session_window(day, now=now)


def range_session_window(
    config: Config,
    start: date,
    end: date,
    now: Optional[datetime] = None,
) -> Optional[Tuple[datetime, datetime]]:
    """Multi-day session span from first to last trading day in range."""
    return calendar_for_config(config).range_session_window(start, end, now=now)
