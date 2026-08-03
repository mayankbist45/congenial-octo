"""Intraday candle loading and cache management."""

from __future__ import annotations

import pickle
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import pandas as pd

from src.broker.base import BrokerClient
from src.config import Config, get_config
from src.logging_setup import get_logger

logger = get_logger(__name__)

SUPPORTED_INTERVALS = {"ONE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE"}


class IntradayDataService:
    """Load broker intraday candles and cache them independently from daily data."""

    def __init__(
        self,
        config: Optional[Config] = None,
        broker: Optional[BrokerClient] = None,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self._config = config or get_config()
        self._broker = broker
        self._now = now or datetime.now
        self._cache: Dict[str, pd.DataFrame] = {}

    @property
    def _cache_path(self) -> Path:
        return self._config.cache_dir / "intraday_candles.pkl"

    def _key(self, symbol: str, day: date, interval: str) -> str:
        return f"{symbol.upper()}|{day.isoformat()}|{interval.upper()}"

    def _load_cache(self) -> None:
        if self._cache:
            return
        if not self._cache_path.exists():
            return
        try:
            with open(self._cache_path, "rb") as handle:
                payload = pickle.load(handle)
            if isinstance(payload, dict):
                self._cache = payload
        except Exception as exc:
            logger.warning("Could not read intraday candle cache: %s", exc)

    def _save_cache(self) -> None:
        try:
            with open(self._cache_path, "wb") as handle:
                pickle.dump(self._cache, handle, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:
            logger.warning("Could not write intraday candle cache: %s", exc)

    def candles(
        self,
        symbol: str,
        day: date,
        interval: str = "FIVE_MINUTE",
        use_cache: bool = True,
    ) -> pd.DataFrame:
        interval = interval.upper()
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"Unsupported intraday interval {interval!r}")

        key = self._key(symbol, day, interval)
        if use_cache:
            self._load_cache()
            if key in self._cache:
                return _strip_index_timezone(self._cache[key]).copy()

        broker = self._broker
        if broker is not None and not hasattr(broker, "get_candles") and hasattr(broker, "inner"):
            broker = getattr(broker, "inner")
        if broker is None or not hasattr(broker, "get_candles"):
            logger.warning("No broker candle source available for %s", symbol)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        window = self._request_window(day)
        if window is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        from_dt, to_dt = window
        raw = getattr(broker, "get_candles")(symbol, from_dt, to_dt, interval)
        frame = normalize_candles(raw)
        if use_cache and not frame.empty:
            self._cache[key] = frame
            self._save_cache()
        return _strip_index_timezone(frame).copy()

    def candles_range(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "FIVE_MINUTE",
    ) -> pd.DataFrame:
        """Load intraday candles for a date range with one broker request.

        Angel One can return multiple trading days in a single historical
        request. Using this for backtests avoids the slow symbol/day nested loop
        that quickly feels hung under historical-data throttles.
        """
        interval = interval.upper()
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"Unsupported intraday interval {interval!r}")

        broker = self._broker
        if broker is not None and not hasattr(broker, "get_candles") and hasattr(broker, "inner"):
            broker = getattr(broker, "inner")
        if broker is None or not hasattr(broker, "get_candles"):
            logger.warning("No broker candle source available for %s", symbol)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        window = self._range_request_window(start, end)
        if window is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        from_dt, to_dt = window
        raw = getattr(broker, "get_candles")(symbol, from_dt, to_dt, interval)
        return _strip_index_timezone(normalize_candles(raw))

    def _request_window(self, day: date) -> Optional[tuple[datetime, datetime]]:
        """Return the legal broker request window, or None when no bars can exist.

        Angel One rejects requests whose ``fromdate`` is greater than the
        current timestamp. That matters before the market opens and whenever a
        backtest range includes today or future dates.
        """
        if day.weekday() >= 5:
            return None
        now = self._now()
        if day > now.date():
            return None

        from_dt = datetime.combine(day, time(9, 15))
        to_dt = datetime.combine(day, time(15, 30))
        if day == now.date():
            if now < from_dt:
                return None
            to_dt = min(to_dt, now)
        if to_dt <= from_dt:
            return None
        return from_dt, to_dt

    def _range_request_window(
        self, start: date, end: date
    ) -> Optional[tuple[datetime, datetime]]:
        now = self._now()
        if end < start:
            return None
        if start > now.date():
            return None

        end = min(end, now.date())
        cursor = start
        first_day: Optional[date] = None
        last_day: Optional[date] = None
        while cursor <= end:
            if cursor.weekday() < 5:
                first_day = first_day or cursor
                last_day = cursor
            cursor += timedelta(days=1)

        if first_day is None or last_day is None:
            return None

        from_dt = datetime.combine(first_day, time(9, 15))
        to_dt = datetime.combine(last_day, time(15, 30))
        if last_day == now.date():
            if now < datetime.combine(last_day, time(9, 15)):
                if first_day == last_day:
                    return None
                previous = last_day - timedelta(days=1)
                while previous >= first_day and previous.weekday() >= 5:
                    previous -= timedelta(days=1)
                if previous < first_day:
                    return None
                to_dt = datetime.combine(previous, time(15, 30))
            else:
                to_dt = min(to_dt, now)
        if to_dt <= from_dt:
            return None
        return from_dt, to_dt


def normalize_candles(rows: Any) -> pd.DataFrame:
    """Normalize broker candle payloads to an indexed OHLCV frame."""
    if rows is None:
        rows = []
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    rename = {c: str(c).lower() for c in frame.columns}
    frame = frame.rename(columns=rename)
    if "timestamp" not in frame.columns:
        raise ValueError("Intraday candles require a timestamp column")

    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    try:
        if frame["timestamp"].dt.tz is not None:
            frame["timestamp"] = frame["timestamp"].dt.tz_localize(None)
    except AttributeError:
        frame["timestamp"] = frame["timestamp"].apply(
            lambda value: value.tz_localize(None)
            if isinstance(value, pd.Timestamp) and value.tzinfo is not None
            else value
        )
    for col in ("open", "high", "low", "close", "volume"):
        if col not in frame.columns:
            raise ValueError(f"Intraday candles missing {col!r}")
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
    frame = frame.sort_values("timestamp").set_index("timestamp")
    return _strip_index_timezone(frame[["open", "high", "low", "close", "volume"]])


def _strip_index_timezone(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        return frame
    out = frame.copy()
    if out.index.tz is not None:
        out.index = out.index.tz_localize(None)
    return out
