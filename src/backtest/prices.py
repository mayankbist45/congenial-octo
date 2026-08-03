"""Deep daily price history for the earnings backtest, via Yahoo Finance.

The fund's live :mod:`src.market_data.prices` service caches ~500 days because
that is all the daily cycle needs; an earnings backtest wants the full listed
history, so this store downloads ``period="max"`` and keeps one pickle per
ticker under ``data/backtest/prices/`` with a JSON manifest recording when
each series was last fetched and how deep it goes.

Yahoo symbol conventions match the live service: Indian tickers get ``.NS``
appended, index symbols (``^CRSLDX``) pass through untouched. US tickers pass
through too, except share-class dots become dashes (``BRK.B`` -> ``BRK-B``),
which is how Yahoo names them. ``auto_adjust=True`` so the engine sees
split- and dividend-adjusted prices — the only sane basis for event returns.
"""

from __future__ import annotations

import json
import pickle
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from src.logging_setup import get_logger

logger = get_logger(__name__)

_BATCH_SIZE = 25
_BATCH_PAUSE_SECONDS = 1.0
BENCHMARKS = {"SPY": "SPY", "^CRSLDX": "^CRSLDX"}

_SANITIZE_PATTERN = re.compile(r"[^A-Za-z0-9.\-]")


def us_yahoo_symbol(ticker: str) -> str:
    """AAPL stays AAPL; BRK.B becomes BRK-B."""
    return ticker.upper().strip().replace(".", "-")


def india_yahoo_symbol(ticker: str) -> str:
    """RELIANCE becomes RELIANCE.NS; indices already carry their ^ prefix."""
    ticker = ticker.upper().strip()
    if ticker.startswith("^") or "." in ticker:
        return ticker
    return f"{ticker}.NS"


def _cache_name(ticker: str) -> str:
    return _SANITIZE_PATTERN.sub("_", ticker.upper().strip()) + ".pkl"


class BacktestPriceStore:
    """Per-ticker pickled frames plus a fetch manifest.

    Store keys are market-aware: US tickers as-is (``AAPL``), Indian tickers
    in Yahoo form (``HAL.NS``), indices as-is (``^CRSLDX``). The two markets
    share tickers (CCL/HAL/IEX mean different companies on the NSE and NYSE),
    so keying India by the Yahoo symbol is what keeps the cache collision-free.
    """

    def __init__(self, cache_dir: Path):
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self._cache_dir / "manifest.json"
        self._manifest = self._read_manifest()

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------
    def _read_manifest(self) -> Dict[str, dict]:
        if not self._manifest_path.exists():
            return {}
        try:
            with open(self._manifest_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                return payload
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read price manifest: %s", exc)
        return {}

    def _write_manifest(self) -> None:
        try:
            with open(self._manifest_path, "w", encoding="utf-8") as handle:
                json.dump(self._manifest, handle, indent=2, sort_keys=True)
        except OSError as exc:
            logger.warning("Could not write price manifest: %s", exc)

    def manifest(self) -> Dict[str, dict]:
        return dict(self._manifest)

    # ------------------------------------------------------------------
    # Cache IO
    # ------------------------------------------------------------------
    def _path_for(self, ticker: str) -> Path:
        return self._cache_dir / _cache_name(ticker)

    def load(self, ticker: str) -> Optional[pd.DataFrame]:
        path = self._path_for(ticker)
        if not path.exists():
            return None
        try:
            with open(path, "rb") as handle:
                frame = pickle.load(handle)
            if isinstance(frame, pd.DataFrame):
                return frame
        except (pickle.UnpicklingError, EOFError, OSError) as exc:
            logger.warning("Could not read cached prices for %s: %s", ticker, exc)
        return None

    def load_all(self) -> Dict[str, pd.DataFrame]:
        out: Dict[str, pd.DataFrame] = {}
        for ticker in self._manifest:
            frame = self.load(ticker)
            if frame is not None:
                out[ticker] = frame
        return out

    def cached_tickers(self) -> List[str]:
        return sorted(self._manifest.keys())

    # ------------------------------------------------------------------
    # Downloading
    # ------------------------------------------------------------------
    def _download_batch(self, mapping: Dict[str, str]) -> Dict[str, pd.DataFrame]:
        """One yfinance batch; ``mapping`` is store key -> Yahoo symbol."""
        import yfinance as yf

        yahoo_symbols = list(mapping.values())
        raw = None
        for attempt in (1, 2):
            try:
                raw = yf.download(
                    yahoo_symbols,
                    period="max",
                    interval="1d",
                    progress=False,
                    auto_adjust=True,
                    group_by="ticker",
                    threads=True,
                )
            except Exception as exc:  # yfinance raises a grab-bag of types
                logger.warning("Price batch failed (attempt %d): %s", attempt, exc)
                raw = None
            if raw is not None and not raw.empty:
                break
            time.sleep(8.0 * attempt)

        if raw is None or raw.empty:
            return {}

        out: Dict[str, pd.DataFrame] = {}
        for key, yahoo_symbol in mapping.items():
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if yahoo_symbol not in raw.columns.get_level_values(0):
                        continue
                    frame = raw[yahoo_symbol].copy()
                else:
                    frame = raw.copy()
            except (KeyError, IndexError):
                continue

            if "Close" not in frame.columns:
                continue
            frame = frame.dropna(subset=["Close"])
            if frame.empty:
                continue
            if frame.index.tz is not None:
                frame.index = frame.index.tz_localize(None)
            out[key] = frame
        return out

    def _fetch(self, mapping: Dict[str, str], resume: bool) -> Dict[str, str]:
        """Fetch every key in ``mapping``; returns key -> status string."""
        pending = dict(mapping)
        if resume:
            pending = {
                key: yahoo
                for key, yahoo in mapping.items()
                if not (key in self._manifest and self._path_for(key).exists())
            }
        statuses: Dict[str, str] = {
            key: "cached" for key in mapping if key not in pending
        }
        if not pending:
            return statuses

        keys = list(pending.keys())
        logger.info(
            "Downloading max history for %d series in batches of %d",
            len(keys),
            _BATCH_SIZE,
        )
        for start in range(0, len(keys), _BATCH_SIZE):
            chunk = keys[start : start + _BATCH_SIZE]
            fetched = self._download_batch({k: pending[k] for k in chunk})
            for key in chunk:
                frame = fetched.get(key)
                if frame is None:
                    statuses[key] = "failed"
                    continue
                try:
                    with open(self._path_for(key), "wb") as handle:
                        pickle.dump(frame, handle, protocol=pickle.HIGHEST_PROTOCOL)
                except OSError as exc:
                    logger.warning("Could not cache prices for %s: %s", key, exc)
                    statuses[key] = "failed"
                    continue
                self._manifest[key] = {
                    "yahoo": pending[key],
                    "fetched_on": datetime.now().isoformat(timespec="seconds"),
                    "rows": int(len(frame)),
                    "first_date": str(frame.index[0].date()),
                    "last_date": str(frame.index[-1].date()),
                }
                statuses[key] = "fetched"
            self._write_manifest()
            logger.info(
                "  batch %d/%d: %d/%d series retrieved",
                start // _BATCH_SIZE + 1,
                (len(keys) + _BATCH_SIZE - 1) // _BATCH_SIZE,
                sum(1 for k in chunk if statuses.get(k) == "fetched"),
                len(chunk),
            )
            time.sleep(_BATCH_PAUSE_SECONDS)
        return statuses

    def fetch_us(self, tickers: List[str], resume: bool = True) -> Dict[str, str]:
        mapping = {t.upper().strip(): us_yahoo_symbol(t) for t in tickers}
        return self._fetch(mapping, resume=resume)

    def fetch_india(self, tickers: List[str], resume: bool = True) -> Dict[str, str]:
        # Keyed by Yahoo symbol (HAL.NS), not the bare ticker: bare tickers
        # collide with US names in a shared cache.
        mapping = {india_yahoo_symbol(t): india_yahoo_symbol(t) for t in tickers}
        return self._fetch(mapping, resume=resume)

    def fetch_benchmarks(self, resume: bool = True) -> Dict[str, str]:
        return self._fetch(dict(BENCHMARKS), resume=resume)

    # ------------------------------------------------------------------
    # Liquidity ranking
    # ------------------------------------------------------------------
    def average_daily_turnover(self, ticker: str, window: int = 60) -> Optional[float]:
        """Mean close x volume over the recent window, in local currency."""
        frame = self.load(ticker)
        if frame is None or "Close" not in frame or "Volume" not in frame:
            return None
        tail = frame.tail(window)
        turnover = (tail["Close"].astype(float) * tail["Volume"].astype(float)).dropna()
        if turnover.empty:
            return None
        return float(turnover.mean())

    def rank_by_turnover(
        self, tickers: List[str], window: int = 60
    ) -> List[str]:
        """Most liquid first; tickers with no cached history go last."""
        scored = []
        for ticker in tickers:
            turnover = self.average_daily_turnover(ticker, window=window)
            scored.append((ticker, turnover if turnover is not None else -1.0))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [ticker for ticker, _ in scored]

    # ------------------------------------------------------------------
    # Coverage helpers for the report
    # ------------------------------------------------------------------
    def history_depths(self) -> Dict[str, Optional[date]]:
        """Store key -> first bar date, from the manifest."""
        out: Dict[str, Optional[date]] = {}
        for key, entry in self._manifest.items():
            raw = entry.get("first_date")
            try:
                out[key] = datetime.strptime(str(raw), "%Y-%m-%d").date() if raw else None
            except ValueError:
                out[key] = None
        return out
