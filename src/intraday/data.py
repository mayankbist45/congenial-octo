"""Intraday candle loading and cache management."""

from __future__ import annotations

import pickle
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from src.broker.base import BrokerClient
from src.config import Config, get_config
from src.logging_setup import get_logger
from src.market.session import market_timezone, range_session_window, session_window

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
        self._prior_day_cache: Dict[str, Dict[str, float]] = {}

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
        refresh: bool = False,
    ) -> pd.DataFrame:
        interval = interval.upper()
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"Unsupported intraday interval {interval!r}")

        key = self._key(symbol, day, interval)
        cached: Optional[pd.DataFrame] = None
        if use_cache:
            self._load_cache()
            if key in self._cache:
                cached = _strip_index_timezone(self._cache[key]).copy()
                if not refresh:
                    return cached
                if self._cache_is_fresh(cached, interval):
                    return cached

        broker = self._resolve_broker()
        if broker is None:
            if cached is not None and not cached.empty:
                return cached
            logger.warning("No broker candle source available for %s", symbol)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        window = self._request_window(day)
        if window is None:
            if cached is not None and not cached.empty:
                return cached
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        from_dt, to_dt = window

        if refresh and cached is not None and not cached.empty:
            overlap = timedelta(minutes=self._interval_minutes(interval))
            tz = market_timezone(self._config)
            from_naive = (
                from_dt.astimezone(tz).replace(tzinfo=None)
                if from_dt.tzinfo
                else from_dt
            )
            from_naive = max(
                from_naive, cached.index.max().to_pydatetime() - overlap
            )
            from_dt = from_naive.replace(tzinfo=tz)

        if to_dt <= from_dt:
            return cached if cached is not None else pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"]
            )

        raw = broker.get_candles(symbol, from_dt, to_dt, interval)
        frame = _to_market_local_naive(normalize_candles(raw), self._config)
        if cached is not None and not cached.empty and not frame.empty:
            frame = _merge_candles(cached, frame)
        elif cached is not None and not cached.empty and frame.empty:
            logger.debug(
                "Using cached candles for %s after empty broker response", symbol
            )
            return cached

        if use_cache and not frame.empty:
            self._cache[key] = frame
            self._save_cache()
        if frame.empty and cached is not None and not cached.empty:
            if self._cache_is_acceptably_stale(cached, interval):
                logger.debug(
                    "Using stale cache for %s (broker returned no new bars)", symbol
                )
                return cached
        return _strip_index_timezone(frame).copy()

    def _resolve_broker(self) -> Optional[BrokerClient]:
        broker = self._broker
        if broker is not None and not hasattr(broker, "get_candles") and hasattr(broker, "inner"):
            broker = getattr(broker, "inner")
        if broker is not None and hasattr(broker, "get_candles"):
            return broker
        return None

    def _request_window(self, day: date) -> Optional[tuple[datetime, datetime]]:
        return session_window(self._config, day, now=self._now())

    def _interval_minutes(self, interval: str) -> int:
        return {"ONE_MINUTE": 1, "FIVE_MINUTE": 5, "FIFTEEN_MINUTE": 15}.get(
            interval.upper(), 5
        )

    def _cache_is_fresh(self, frame: pd.DataFrame, interval: str) -> bool:
        """Skip API calls when the cache already has the latest completed bar."""
        if frame.empty:
            return False
        stale_minutes = float(
            self._config.get("angelone.candle_cache_stale_minutes", 0) or 0
        )
        if stale_minutes <= 0:
            stale_minutes = float(self._interval_minutes(interval)) + 1.0
        age = self._now() - frame.index.max().to_pydatetime()
        return age.total_seconds() <= stale_minutes * 60.0

    def _cache_is_acceptably_stale(self, frame: pd.DataFrame, interval: str) -> bool:
        """Return cached bars on broker failure when not too old for ranking."""
        if frame.empty:
            return False
        ok_minutes = float(
            self._config.get("angelone.candle_stale_ok_minutes", 15.0) or 15.0
        )
        age = self._now() - frame.index.max().to_pydatetime()
        return age.total_seconds() <= ok_minutes * 60.0

    def candles_range(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "FIVE_MINUTE",
    ) -> pd.DataFrame:
        """Load intraday candles for a date range (one broker request when possible)."""
        interval = interval.upper()
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"Unsupported intraday interval {interval!r}")

        broker = self._resolve_broker()
        if broker is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        window = self._range_request_window(start, end)
        if window is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        from_dt, to_dt = window
        raw = broker.get_candles(symbol, from_dt, to_dt, interval)
        return _strip_index_timezone(
            _to_market_local_naive(normalize_candles(raw), self._config)
        )

    def _range_request_window(
        self, start: date, end: date
    ) -> Optional[tuple[datetime, datetime]]:
        return range_session_window(self._config, start, end, now=self._now())

    def previous_day_stats(
        self,
        symbol: str,
        day: date,
        interval: str = "FIVE_MINUTE",
    ) -> Optional[Dict[str, float]]:
        """Prior session OHLC for gap and prior-level distance."""
        prior = day - timedelta(days=1)
        for _ in range(7):
            if prior.weekday() < 5:
                break
            prior -= timedelta(days=1)
        else:
            return None

        cache_key = f"{symbol.upper()}|{prior.isoformat()}"
        if cache_key in self._prior_day_cache:
            return self._prior_day_cache[cache_key]

        stats = self._prior_day_from_daily(symbol, prior)
        if stats is None:
            frame = self.candles(symbol, prior, interval=interval, use_cache=True)
            if frame.empty:
                frame = self.candles(symbol, prior, interval=interval, use_cache=False)
            if frame.empty:
                return None
            stats = {
                "close": float(frame["close"].iloc[-1]),
                "high": float(frame["high"].max()),
                "low": float(frame["low"].min()),
            }

        self._prior_day_cache[cache_key] = stats
        return stats

    def _prior_day_from_daily(
        self, symbol: str, prior: date
    ) -> Optional[Dict[str, float]]:
        """One ONE_DAY bar for prior session — cheaper than full intraday history."""
        broker = self._resolve_broker()
        if broker is None:
            return None
        from_dt = datetime.combine(prior, time(9, 15))
        to_dt = datetime.combine(prior, time(15, 30))
        try:
            raw = broker.get_candles(symbol, from_dt, to_dt, "ONE_DAY")
        except Exception:
            return None
        frame = normalize_candles(raw)
        if frame.empty:
            return None
        row = frame.iloc[-1]
        return {
            "close": float(row["close"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
        }

    def previous_day_stats_batch(
        self,
        symbols: List[str],
        day: date,
        interval: str = "FIVE_MINUTE",
    ) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {}
        for symbol in symbols:
            stats = self.previous_day_stats(symbol, day, interval=interval)
            if stats:
                out[symbol.upper()] = stats
        return out


def _to_market_local_naive(frame: pd.DataFrame, config: Config) -> pd.DataFrame:
    """Convert broker timestamps to exchange-local naive times for rule logic."""
    if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        return frame
    if config.market_region != "US":
        return _strip_index_timezone(frame)
    tz = market_timezone(config)
    out = frame.copy()
    idx = out.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    out.index = idx.tz_convert(tz).tz_localize(None)
    return out


def normalize_candles(rows: Any) -> pd.DataFrame:
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
        pass

    for col in ("open", "high", "low", "close", "volume"):
        if col not in frame.columns:
            raise ValueError(f"Intraday candles missing {col!r}")
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
    frame = frame.sort_values("timestamp").set_index("timestamp")
    return _strip_index_timezone(frame[["open", "high", "low", "close", "volume"]])


def _merge_candles(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    combined = pd.concat([existing, new])
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()


def _strip_index_timezone(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        return frame
    out = frame.copy()
    if out.index.tz is not None:
        out.index = out.index.tz_localize(None)
    return out
