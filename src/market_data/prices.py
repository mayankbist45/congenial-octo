"""Price history and technical snapshots.

Yahoo Finance is the primary source because it serves the whole Nifty 500 in a
handful of batched requests, which matters when scoring 500 names daily. Angel
One is used for live intraday quotes at execution time, where a 15-minute delay
would be unacceptable.

History is cached on disk as Parquet-like pickled frames and refreshed once per
session, so re-running the daily cycle is cheap.
"""

from __future__ import annotations

import pickle
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from src.config import Config, get_config
from src.logging_setup import get_logger
from src.market_data.indicators import compute_snapshot
from src.models import TechnicalSnapshot

logger = get_logger(__name__)

_BATCH_SIZE = 60


class PriceService:
    """Fetches, caches and analyses daily price history."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._frames: Dict[str, pd.DataFrame] = {}
        self._snapshots: Dict[str, TechnicalSnapshot] = {}

    # ------------------------------------------------------------------
    # Caching
    # ------------------------------------------------------------------
    @property
    def _cache_path(self) -> Path:
        return self._config.cache_dir / "price_history.pkl"

    def _cache_ttl_seconds(self) -> float:
        hours = float(self._config.get("market_data.cache_ttl_hours", 12) or 12)
        return hours * 3600.0

    def _cache_is_fresh(self) -> bool:
        path = self._cache_path
        if not path.exists():
            return False
        return (time.time() - path.stat().st_mtime) < self._cache_ttl_seconds()

    def _load_cache(self) -> Dict[str, pd.DataFrame]:
        try:
            with open(self._cache_path, "rb") as handle:
                payload = pickle.load(handle)
            if isinstance(payload, dict):
                return payload
        except Exception as exc:
            logger.warning("Could not read price cache: %s", exc)
        return {}

    def cached_history(self) -> Dict[str, pd.DataFrame]:
        """Whatever history is already on disk, without triggering a download.

        For offline analysis that wants the whole universe but has no reason to
        hit the network or to care how stale the cache is.
        """
        if self._frames:
            return dict(self._frames)
        return self._load_cache()

    def _save_cache(self) -> None:
        try:
            with open(self._cache_path, "wb") as handle:
                pickle.dump(self._frames, handle, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:
            logger.warning("Could not write price cache: %s", exc)

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------
    def _download_batch(self, symbols: List[str], days: int) -> Dict[str, pd.DataFrame]:
        import yfinance as yf

        yahoo_symbols = [self._to_yahoo(s) for s in symbols]
        period = f"{max(days, 30)}d"

        try:
            raw = yf.download(
                yahoo_symbols,
                period=period,
                interval="1d",
                progress=False,
                auto_adjust=True,
                group_by="ticker",
                threads=True,
            )
        except Exception as exc:
            logger.warning("Batch price download failed for %d symbols: %s", len(symbols), exc)
            return {}

        if raw is None or raw.empty:
            return {}

        out: Dict[str, pd.DataFrame] = {}
        for symbol, yahoo_symbol in zip(symbols, yahoo_symbols):
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if yahoo_symbol not in raw.columns.get_level_values(0):
                        continue
                    frame = raw[yahoo_symbol].copy()
                else:
                    frame = raw.copy()
            except (KeyError, IndexError):
                continue

            frame = frame.dropna(subset=["Close"]) if "Close" in frame.columns else frame
            if frame.empty:
                continue
            out[symbol] = frame

        return out

    def _retry_missing_symbols(
        self, symbols: List[str], days: int
    ) -> Dict[str, pd.DataFrame]:
        """Retry missing symbols one by one after a partial batch failure."""
        recovered: Dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            try:
                fetched = self._download_batch([symbol], days)
            except Exception as exc:
                logger.warning("Retry failed for %s: %s", symbol, exc)
                continue
            frame = fetched.get(symbol)
            if frame is not None:
                recovered[symbol] = frame
        return recovered

    def _to_yahoo(self, symbol: str) -> str:
        symbol = symbol.upper()
        # Index symbols (^NSEI, ^CRSLDX, ^GSPC) are already in Yahoo form.
        if symbol.startswith("^") or "." in symbol:
            return symbol
        if self._config.market_region == "US":
            return symbol
        return f"{symbol}.NS"

    def load_history(
        self,
        symbols: List[str],
        days: Optional[int] = None,
        use_cache: bool = True,
    ) -> Dict[str, pd.DataFrame]:
        """Load daily OHLCV for the given symbols, batching network calls."""
        days = days or int(self._config.get("market_data.history_days", 500) or 500)
        symbols = [s.upper() for s in dict.fromkeys(symbols)]

        if use_cache and not self._frames and self._cache_is_fresh():
            self._frames = self._load_cache()
            if self._frames:
                logger.info("Loaded %d price series from cache", len(self._frames))

        missing = [s for s in symbols if s not in self._frames]

        if missing:
            logger.info(
                "Downloading price history for %d symbol(s) in batches of %d",
                len(missing),
                _BATCH_SIZE,
            )
            for start in range(0, len(missing), _BATCH_SIZE):
                chunk = missing[start : start + _BATCH_SIZE]
                fetched = self._download_batch(chunk, days)
                chunk_missing = [symbol for symbol in chunk if symbol not in fetched]
                if fetched and chunk_missing:
                    logger.info(
                        "  retrying %d symbol(s) individually after partial batch miss",
                        len(chunk_missing),
                    )
                    recovered = self._retry_missing_symbols(chunk_missing, days)
                    if recovered:
                        logger.info(
                            "  recovered %d/%d symbol(s) on individual retry",
                            len(recovered),
                            len(chunk_missing),
                        )
                    fetched.update(recovered)
                self._frames.update(fetched)
                logger.info(
                    "  batch %d/%d: %d/%d series retrieved",
                    start // _BATCH_SIZE + 1,
                    (len(missing) + _BATCH_SIZE - 1) // _BATCH_SIZE,
                    len(fetched),
                    len(chunk),
                )
            self._save_cache()

        still_missing = [s for s in symbols if s not in self._frames]
        if still_missing:
            logger.warning(
                "No price history for %d symbol(s): %s",
                len(still_missing),
                ", ".join(still_missing[:10]),
            )

        return {s: self._frames[s] for s in symbols if s in self._frames}

    def get_frame(self, symbol: str) -> Optional[pd.DataFrame]:
        symbol = symbol.upper()
        if symbol not in self._frames:
            self.load_history([symbol])
        return self._frames.get(symbol)

    def close_series(self, symbol: str) -> Optional[pd.Series]:
        frame = self.get_frame(symbol)
        if frame is None or "Close" not in frame.columns:
            return None
        return frame["Close"].astype(float).dropna()

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------
    def snapshots(
        self, symbols: List[str], days: Optional[int] = None
    ) -> Dict[str, TechnicalSnapshot]:
        """Compute technical snapshots for the given symbols."""
        frames = self.load_history(symbols, days=days)
        out: Dict[str, TechnicalSnapshot] = {}

        for symbol, frame in frames.items():
            if symbol in self._snapshots:
                out[symbol] = self._snapshots[symbol]
                continue
            payload = compute_snapshot(frame, symbol)
            snapshot = TechnicalSnapshot(**payload)
            self._snapshots[symbol] = snapshot
            out[symbol] = snapshot

        logger.info("Computed %d technical snapshots", len(out))
        return out

    def snapshot(self, symbol: str) -> Optional[TechnicalSnapshot]:
        return self.snapshots([symbol]).get(symbol.upper())

    # ------------------------------------------------------------------
    # Quotes
    # ------------------------------------------------------------------
    def get_last_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Last closing price per symbol, from cached history."""
        frames = self.load_history(symbols)
        prices: Dict[str, float] = {}
        for symbol, frame in frames.items():
            if "Close" not in frame.columns:
                continue
            closes = frame["Close"].dropna()
            if closes.empty:
                continue
            prices[symbol] = float(closes.iloc[-1])
        return prices

    def get_last_price(self, symbol: str) -> Optional[float]:
        return self.get_last_prices([symbol]).get(symbol.upper())

    def benchmark_series(self) -> Optional[pd.Series]:
        """Closing price series for the fund's benchmark index."""
        market_fund = getattr(self._config.market, "fund", None) or {}
        default_symbol = str(market_fund.get("benchmark_symbol") or "^CRSLDX")
        symbol = str(self._config.get("fund.benchmark_symbol", default_symbol))
        days = int(
            self._config.get("market_data.benchmark_history_days", 750) or 750
        )
        frames = self.load_history([symbol], days=days)
        frame = frames.get(symbol.upper())
        if frame is None or "Close" not in frame.columns:
            logger.warning("Benchmark history unavailable for %s", symbol)
            return None
        return frame["Close"].astype(float).dropna()

    def latest_data_date(self) -> Optional[date]:
        """Most recent bar date across cached history - the fund's 'as of'."""
        latest: Optional[date] = None
        for frame in self._frames.values():
            if frame.empty:
                continue
            stamp = frame.index[-1]
            candidate = stamp.date() if isinstance(stamp, (pd.Timestamp, datetime)) else None
            if candidate and (latest is None or candidate > latest):
                latest = candidate
        return latest

    def clear_cache(self) -> None:
        self._frames.clear()
        self._snapshots.clear()
        if self._cache_path.exists():
            self._cache_path.unlink()
