"""Fundamentals service.

Fundamentals change when companies report, not intraday, so they are cached on
disk for a week by default. Screener.in is the primary source and Yahoo fills
whatever it leaves blank.

Fetching 500 companies takes a few minutes on a cold cache. Holdings are always
refreshed first so that a stale cache never delays a sell decision.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from src.config import Config, get_config
from src.fundamentals.screener import ScreenerClient
from src.fundamentals.sector_models import apply_sector_context
from src.logging_setup import get_logger
from src.models import FundamentalSnapshot
from src.universe import load_universe

logger = get_logger(__name__)

_SNAPSHOT_FIELDS = {f.name for f in fields(FundamentalSnapshot)}


def _merge(
    primary: Optional[FundamentalSnapshot],
    secondary: Optional[FundamentalSnapshot],
) -> Optional[FundamentalSnapshot]:
    """Fill gaps in ``primary`` from ``secondary`` without overwriting data."""
    if primary is None:
        return secondary
    if secondary is None:
        return primary

    filled: List[str] = []
    for name in _SNAPSHOT_FIELDS:
        if name in ("symbol", "as_of", "source"):
            continue
        if getattr(primary, name) is None:
            value = getattr(secondary, name, None)
            if value is not None:
                setattr(primary, name, value)
                filled.append(name)

    if filled:
        primary.source = f"{primary.source}+{secondary.source}"
    return primary


class FundamentalsService:
    """Cached access to company fundamentals."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._universe = load_universe(self._config)
        self._screener = ScreenerClient(
            request_delay=float(
                self._config.get("fundamentals.request_delay_seconds", 0.5) or 0.5
            )
        )
        self._memory: Dict[str, FundamentalSnapshot] = {}
        self._cache: Dict[str, dict] = {}
        self._cache_loaded = False

    @property
    def _cache_path(self) -> Path:
        return self._config.cache_dir / "fundamentals.json"

    def _ttl(self) -> timedelta:
        hours = float(self._config.get("fundamentals.cache_ttl_hours", 168) or 168)
        return timedelta(hours=hours)

    # ------------------------------------------------------------------
    # Disk cache
    # ------------------------------------------------------------------
    def _load_cache(self) -> None:
        if self._cache_loaded:
            return
        self._cache_loaded = True
        path = self._cache_path
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                self._cache = payload
                logger.info("Loaded %d cached fundamental records", len(payload))
        except Exception as exc:
            logger.warning("Could not read fundamentals cache: %s", exc)

    def _save_cache(self) -> None:
        try:
            with open(self._cache_path, "w", encoding="utf-8") as handle:
                json.dump(self._cache, handle, indent=1, default=str)
        except Exception as exc:
            logger.warning("Could not write fundamentals cache: %s", exc)

    def _from_cache(self, symbol: str) -> Optional[FundamentalSnapshot]:
        self._load_cache()
        record = self._cache.get(symbol)
        if not record:
            return None

        stamp = record.get("as_of")
        if stamp:
            try:
                fetched_at = datetime.fromisoformat(str(stamp))
            except ValueError:
                fetched_at = None
            if fetched_at and datetime.utcnow() - fetched_at > self._ttl():
                return None

        payload = {k: v for k, v in record.items() if k in _SNAPSHOT_FIELDS}
        payload["symbol"] = symbol
        if payload.get("as_of"):
            try:
                payload["as_of"] = datetime.fromisoformat(str(payload["as_of"]))
            except ValueError:
                payload["as_of"] = None
        return FundamentalSnapshot(**payload)

    def _to_cache(self, snapshot: FundamentalSnapshot) -> None:
        self._load_cache()
        self._cache[snapshot.symbol] = asdict(snapshot)

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------
    def _fetch_fresh(self, symbol: str) -> Optional[FundamentalSnapshot]:
        from src.fundamentals import yahoo

        region = self._config.market_region
        snapshot: Optional[FundamentalSnapshot] = None

        if region != "US":
            try:
                snapshot = self._screener.fetch(symbol)
            except Exception as exc:
                logger.debug("Screener fetch error for %s: %s", symbol, exc)

        needs_supplement = region == "US" or snapshot is None or any(
            getattr(snapshot, name) is None
            for name in ("pe_ratio", "roe_pct", "debt_to_equity", "revenue_growth_pct")
        )
        if needs_supplement:
            try:
                snapshot = _merge(snapshot, yahoo.fetch(symbol, market_region=region))
            except Exception as exc:
                logger.debug("Yahoo fetch error for %s: %s", symbol, exc)

        if snapshot is not None:
            snapshot = apply_sector_context(
                snapshot, self._universe.sector_of(symbol)
            )

        return snapshot

    def get(
        self, symbol: str, force_refresh: bool = False
    ) -> Optional[FundamentalSnapshot]:
        symbol = symbol.upper()

        if not force_refresh:
            if symbol in self._memory:
                return self._memory[symbol]
            cached = self._from_cache(symbol)
            if cached is not None:
                self._memory[symbol] = cached
                return cached

        snapshot = self._fetch_fresh(symbol)
        if snapshot is not None:
            self._memory[symbol] = snapshot
            self._to_cache(snapshot)
        return snapshot

    def get_many(
        self,
        symbols: Iterable[str],
        force_refresh: bool = False,
        progress_every: int = 50,
    ) -> Dict[str, FundamentalSnapshot]:
        """Fetch many symbols, reporting progress on a cold cache."""
        symbols = [s.upper() for s in dict.fromkeys(symbols)]
        out: Dict[str, FundamentalSnapshot] = {}

        to_fetch: List[str] = []
        for symbol in symbols:
            if force_refresh:
                to_fetch.append(symbol)
                continue
            if symbol in self._memory:
                out[symbol] = self._memory[symbol]
                continue
            cached = self._from_cache(symbol)
            if cached is not None:
                self._memory[symbol] = cached
                out[symbol] = cached
            else:
                to_fetch.append(symbol)

        if to_fetch:
            logger.info(
                "Fetching fundamentals for %d symbol(s) (%d served from cache)",
                len(to_fetch),
                len(out),
            )
            started = time.time()
            for index, symbol in enumerate(to_fetch, start=1):
                snapshot = self.get(symbol, force_refresh=force_refresh)
                if snapshot is not None:
                    out[symbol] = snapshot
                if index % progress_every == 0 or index == len(to_fetch):
                    rate = index / max(time.time() - started, 0.001)
                    logger.info(
                        "  fundamentals %d/%d (%.1f/s)", index, len(to_fetch), rate
                    )
            self._save_cache()

        usable = sum(1 for s in out.values() if s.is_usable)
        logger.info(
            "Fundamentals ready: %d/%d symbols (%d usable for scoring)",
            len(out),
            len(symbols),
            usable,
        )
        return out

    def refresh_symbols(self, symbols: Iterable[str]) -> Dict[str, FundamentalSnapshot]:
        """Force-refresh specific names, e.g. current holdings before a run."""
        return self.get_many(symbols, force_refresh=True)

    def close(self) -> None:
        self._save_cache()
        self._screener.close()
