"""Angel One instrument master.

Every SmartAPI call identifies an instrument by numeric token, not by ticker,
so the fund keeps a cached copy of the daily scrip master and resolves
``RELIANCE`` -> ``2885`` locally.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests

from src.config import Config, get_config
from src.logging_setup import get_logger

logger = get_logger(__name__)

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/"
    "OpenAPIScripMaster.json"
)
CACHE_FILENAME = "angelone_instruments.json"
# The master is regenerated once a day before market open.
CACHE_TTL_SECONDS = 20 * 3600


@dataclass
class Instrument:
    token: str
    symbol: str  # Broker trading symbol, e.g. "RELIANCE-EQ"
    name: str  # Base ticker, e.g. "RELIANCE"
    exchange: str
    lot_size: int = 1
    tick_size: float = 0.05
    instrument_type: str = ""


class InstrumentMaster:
    """Cached symbol-to-token lookup for NSE cash equities."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._by_symbol: Dict[str, Instrument] = {}
        self._by_token: Dict[str, Instrument] = {}
        self._loaded = False

    @property
    def cache_path(self) -> Path:
        return self._config.cache_dir / CACHE_FILENAME

    def _cache_is_fresh(self) -> bool:
        path = self.cache_path
        if not path.exists():
            return False
        return (time.time() - path.stat().st_mtime) < CACHE_TTL_SECONDS

    def _download(self) -> List[dict]:
        logger.info("Downloading Angel One scrip master (~10MB)")
        response = requests.get(SCRIP_MASTER_URL, timeout=120)
        response.raise_for_status()
        payload = response.json()
        self.cache_path.write_text(json.dumps(payload), encoding="utf-8")
        logger.info("Scrip master cached: %d instruments", len(payload))
        return payload

    def _read_cache(self) -> List[dict]:
        with open(self.cache_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def load(self, force_refresh: bool = False) -> "InstrumentMaster":
        if self._loaded and not force_refresh:
            return self

        if force_refresh or not self._cache_is_fresh():
            try:
                rows = self._download()
            except Exception as exc:
                if self.cache_path.exists():
                    logger.warning(
                        "Scrip master download failed (%s); using stale cache", exc
                    )
                    rows = self._read_cache()
                else:
                    raise
        else:
            rows = self._read_cache()

        self._index(rows)
        self._loaded = True
        return self

    def _index(self, rows: List[dict]) -> None:
        self._by_symbol.clear()
        self._by_token.clear()

        for row in rows:
            exchange = (row.get("exch_seg") or "").upper()
            if exchange != "NSE":
                continue
            trading_symbol = (row.get("symbol") or "").upper()
            # NSE cash equity rows are suffixed "-EQ"; everything else on the
            # NSE segment is a derivative or ETF variant we do not trade.
            if not trading_symbol.endswith("-EQ"):
                continue

            base = trading_symbol[:-3]
            try:
                lot_size = int(float(row.get("lotsize") or 1))
            except (TypeError, ValueError):
                lot_size = 1
            try:
                # The master reports tick size in paise (5 == Rs 0.05).
                tick_size = float(row.get("tick_size") or 5) / 100.0
            except (TypeError, ValueError):
                tick_size = 0.05

            instrument = Instrument(
                token=str(row.get("token") or ""),
                symbol=trading_symbol,
                name=base,
                exchange=exchange,
                lot_size=max(lot_size, 1),
                tick_size=tick_size if tick_size > 0 else 0.05,
                instrument_type=(row.get("instrumenttype") or "").upper(),
            )
            self._by_symbol[base] = instrument
            self._by_token[instrument.token] = instrument

        logger.info("Indexed %d NSE equity instruments", len(self._by_symbol))

    def get(self, symbol: str) -> Optional[Instrument]:
        if not self._loaded:
            self.load()
        return self._by_symbol.get(symbol.upper())

    def token_for(self, symbol: str) -> Optional[str]:
        instrument = self.get(symbol)
        return instrument.token if instrument else None

    def trading_symbol_for(self, symbol: str) -> str:
        instrument = self.get(symbol)
        return instrument.symbol if instrument else f"{symbol.upper()}-EQ"

    def by_token(self, token: str) -> Optional[Instrument]:
        if not self._loaded:
            self.load()
        return self._by_token.get(str(token))

    def resolve_many(self, symbols: List[str]) -> Dict[str, Instrument]:
        """Map symbols to instruments, logging any that cannot be resolved."""
        resolved: Dict[str, Instrument] = {}
        missing: List[str] = []
        for symbol in symbols:
            instrument = self.get(symbol)
            if instrument:
                resolved[symbol.upper()] = instrument
            else:
                missing.append(symbol)
        if missing:
            logger.warning(
                "Could not resolve %d symbol(s) to instrument tokens: %s",
                len(missing),
                ", ".join(missing[:10]),
            )
        return resolved

    @property
    def age(self) -> Optional[str]:
        path = self.cache_path
        if not path.exists():
            return None
        modified = datetime.fromtimestamp(path.stat().st_mtime)
        return modified.strftime("%Y-%m-%d %H:%M")


_MASTER: Optional[InstrumentMaster] = None


def get_instrument_master(config: Optional[Config] = None) -> InstrumentMaster:
    """Process-wide singleton so the 10MB master is parsed at most once."""
    global _MASTER
    if _MASTER is None:
        _MASTER = InstrumentMaster(config)
    return _MASTER
