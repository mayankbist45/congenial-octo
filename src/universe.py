"""Investable universe loading.

The universe is the Nifty 500 as published by NSE, including each company's
sector, which the risk engine needs for sector caps.
"""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

from src.config import Config, get_config
from src.logging_setup import get_logger
from src.models import UniverseEntry

logger = get_logger(__name__)

NSE_NIFTY500_URL = (
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
)


def _read_csv(path: Path) -> List[UniverseEntry]:
    entries: List[UniverseEntry] = []
    with open(path, "r", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(
            line for line in handle if line.strip() and not line.lstrip().startswith("#")
        )
        if not reader.fieldnames:
            return entries
        fields = {name.strip().lower(): name for name in reader.fieldnames if name}

        for row in reader:
            symbol = (
                row.get(fields.get("symbol", "Symbol"))
                or row.get("Symbol")
                or ""
            ).strip().upper()
            if not symbol:
                continue
            company = (
                row.get(fields.get("company_name", "company_name"))
                or row.get("Company Name")
                or row.get("name")
                or ""
            ).strip()
            sector = (
                row.get(fields.get("gics_sector", "gics_sector"))
                or row.get("Industry")
                or row.get("sector")
                or "Unknown"
            ).strip() or "Unknown"
            isin = (
                row.get(fields.get("isin", "isin"))
                or row.get("ISIN Code")
                or ""
            ).strip()
            entries.append(
                UniverseEntry(
                    symbol=symbol,
                    company_name=company or symbol,
                    sector=sector,
                    isin=isin,
                )
            )
    return entries


class Universe:
    """Indexed access to the investable universe."""

    def __init__(self, entries: List[UniverseEntry]):
        self._entries = entries
        self._by_symbol: Dict[str, UniverseEntry] = {e.symbol: e for e in entries}

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries)

    @property
    def entries(self) -> List[UniverseEntry]:
        return list(self._entries)

    @property
    def symbols(self) -> List[str]:
        return [e.symbol for e in self._entries]

    def get(self, symbol: str) -> Optional[UniverseEntry]:
        return self._by_symbol.get(symbol.upper())

    def contains(self, symbol: str) -> bool:
        return symbol.upper() in self._by_symbol

    def sector_of(self, symbol: str) -> str:
        entry = self.get(symbol)
        return entry.sector if entry else "Unknown"

    def company_name_of(self, symbol: str) -> str:
        entry = self.get(symbol)
        return entry.company_name if entry else symbol

    def sectors(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for entry in self._entries:
            out.setdefault(entry.sector, []).append(entry.symbol)
        return out


@lru_cache(maxsize=4)
def _load_universe_cached(path_str: str) -> Universe:
    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(
            f"Universe file missing at {path}. Run scripts/refresh_universe.py "
            f"to download it from NSE."
        )
    entries = _read_csv(path)
    logger.info("Loaded universe: %d symbols from %s", len(entries), path.name)
    return Universe(entries)


def load_universe(config: Optional[Config] = None) -> Universe:
    config = config or get_config()
    path = config.path("universe.file", "data/nifty500.csv")
    return _load_universe_cached(str(path))


def refresh_universe_file(config: Optional[Config] = None) -> int:
    """Download the current Nifty 500 constituent list from NSE."""
    import requests

    config = config or get_config()
    path = config.path("universe.file", "data/nifty500.csv")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Accept": "text/csv,*/*",
    }
    response = requests.get(NSE_NIFTY500_URL, headers=headers, timeout=30)
    response.raise_for_status()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(response.text, encoding="utf-8")
    _load_universe_cached.cache_clear()

    count = len(_read_csv(path))
    logger.info("Refreshed universe file with %d symbols", count)
    return count
