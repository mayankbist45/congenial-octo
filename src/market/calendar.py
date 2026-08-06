"""Exchange session calendar backed by market definitions."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from src.config import Config
from src.market.registry import Market, get_market, resolve_market_id


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


class SessionCalendar:
    """Timezone-aware session boundaries for one market."""

    def __init__(self, market: Market) -> None:
        self._market = market
        self._tz = ZoneInfo(market.timezone)
        open_s, close_s = market.session.open, market.session.close
        self._open = _parse_hhmm(open_s)
        self._close = _parse_hhmm(close_s)

    @property
    def market(self) -> Market:
        return self._market

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    @property
    def tz_abbrev(self) -> str:
        return self._market.tz_abbrev

    def open_close(self) -> Tuple[time, time]:
        return self._open, self._close

    def close_time(self) -> time:
        return self._close

    def now(self) -> datetime:
        return datetime.now(self._tz)

    def format_local(self, dt: datetime, timespec: str = "minutes") -> str:
        """Format a datetime in market-local time with TZ abbreviation."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self._tz)
        else:
            dt = dt.astimezone(self._tz)
        return f"{dt.strftime(f'%Y-%m-%d %H:%M') if timespec == 'minutes' else dt.isoformat(timespec=timespec)} {self.tz_abbrev}"

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5

    def session_window(
        self,
        day: date,
        now: Optional[datetime] = None,
    ) -> Optional[Tuple[datetime, datetime]]:
        if not self.is_trading_day(day):
            return None

        from_dt = datetime.combine(day, self._open, tzinfo=self._tz)
        to_dt = datetime.combine(day, self._close, tzinfo=self._tz)

        if now is None:
            now = self.now()
        elif now.tzinfo is None:
            now = now.replace(tzinfo=self._tz)
        else:
            now = now.astimezone(self._tz)

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
        self,
        start: date,
        end: date,
        now: Optional[datetime] = None,
    ) -> Optional[Tuple[datetime, datetime]]:
        if now is None:
            now = self.now()
        elif now.tzinfo is None:
            now = now.replace(tzinfo=self._tz)
        else:
            now = now.astimezone(self._tz)

        if end < start or start > now.date():
            return None
        end = min(end, now.date())

        first_day: Optional[date] = None
        last_day: Optional[date] = None
        cursor = start
        while cursor <= end:
            if self.is_trading_day(cursor):
                first_day = first_day or cursor
                last_day = cursor
            cursor += timedelta(days=1)
        if first_day is None or last_day is None:
            return None

        from_dt = datetime.combine(first_day, self._open, tzinfo=self._tz)
        to_dt = datetime.combine(last_day, self._close, tzinfo=self._tz)
        if last_day == now.date():
            to_dt = min(to_dt, now)
        if to_dt <= from_dt:
            return None
        return from_dt, to_dt

    def watch_start_stop(
        self,
        day: date,
    ) -> Optional[Tuple[datetime, datetime]]:
        """Intraday watch cron bounds for a session day (market-local)."""
        watch = self._market.scheduling.intraday_watch
        if watch is None:
            return None
        if not self.is_trading_day(day):
            return None
        open_dt = datetime.combine(day, self._open, tzinfo=self._tz)
        close_dt = datetime.combine(day, self._close, tzinfo=self._tz)
        start = open_dt + timedelta(minutes=watch.start_offset_min)
        stop = close_dt + timedelta(minutes=watch.stop_offset_min)
        return start, stop


def calendar_for_config(config: Config) -> SessionCalendar:
    market_id = config.market_id
    return SessionCalendar(get_market(market_id))


def calendar_for_market(market_id: str) -> SessionCalendar:
    return SessionCalendar(get_market(market_id))
