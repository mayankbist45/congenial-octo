"""Exchange session boundaries by market region."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from src.config import Config

_IN_TZ = ZoneInfo("Asia/Kolkata")
_US_TZ = ZoneInfo("America/New_York")


def market_timezone(config: Config) -> ZoneInfo:
    if config.market_region == "US":
        return _US_TZ
    return _IN_TZ


def session_open_close(config: Config) -> Tuple[time, time]:
    """Regular cash session open and close (local exchange time)."""
    if config.market_region == "US":
        return time(9, 30), time(16, 0)
    return time(9, 15), time(15, 30)


def session_end_time(config: Config) -> time:
    return session_open_close(config)[1]


def session_window(
    config: Config,
    day: date,
    now: Optional[datetime] = None,
) -> Optional[Tuple[datetime, datetime]]:
    """Return timezone-aware session bounds for ``day``, clipped to ``now``."""
    if day.weekday() >= 5:
        return None

    tz = market_timezone(config)
    open_t, close_t = session_open_close(config)
    from_dt = datetime.combine(day, open_t, tzinfo=tz)
    to_dt = datetime.combine(day, close_t, tzinfo=tz)

    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    if day > now.date():
        return None
    if day == now.date():
        if now < from_dt:
            return None
        to_dt = min(to_dt, now)
    if to_dt <= from_dt:
        return None
    return from_dt, to_dt


def range_session_window(
    config: Config,
    start: date,
    end: date,
    now: Optional[datetime] = None,
) -> Optional[Tuple[datetime, datetime]]:
    """Multi-day session span from first to last trading day in range."""
    tz = market_timezone(config)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    if end < start or start > now.date():
        return None
    end = min(end, now.date())

    first_day: Optional[date] = None
    last_day: Optional[date] = None
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            first_day = first_day or cursor
            last_day = cursor
        cursor += timedelta(days=1)
    if first_day is None or last_day is None:
        return None

    open_t, close_t = session_open_close(config)
    from_dt = datetime.combine(first_day, open_t, tzinfo=tz)
    to_dt = datetime.combine(last_day, close_t, tzinfo=tz)
    if last_day == now.date():
        to_dt = min(to_dt, now)
    if to_dt <= from_dt:
        return None
    return from_dt, to_dt
