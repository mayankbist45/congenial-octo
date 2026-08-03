"""Investable universe loading.

The universe is typically the Nifty 500 (NSE) or S&P 500 (US), including each
company's sector for sector-cap checks in the risk engine.
"""

from __future__ import annotations

import csv
import io
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
NSE_SMALLCAP250_URL = (
    "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap250list.csv"
)

UNIVERSE_URLS: Dict[str, str] = {
    "data/nifty500.csv": NSE_NIFTY500_URL,
    "data/nifty_smallcap250.csv": NSE_SMALLCAP250_URL,
}


def _read_csv(path: Path) -> List[UniverseEntry]:
    entries: List[UniverseEntry] = []
    with open(path, "r", encoding="utf-8-sig") as handle:
        lines = [line for line in handle if not line.lstrip().startswith("#")]
    for row in csv.DictReader(io.StringIO("".join(lines))):
        symbol = (row.get("Symbol") or row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        entries.append(
            UniverseEntry(
                symbol=symbol,
                company_name=(
                    row.get("Company Name") or row.get("name") or ""
                ).strip(),
                sector=(
                    row.get("Industry")
                    or row.get("gics_sector")
                    or "Unknown"
                ).strip()
                or "Unknown",
                isin=(row.get("ISIN Code") or row.get("isin") or "").strip(),
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
    universe = _load_universe_cached(str(path))

    gold_symbol = config.get("gold.symbol")
    if gold_symbol and not universe.contains(gold_symbol):
        extra = UniverseEntry(
            symbol=gold_symbol.upper(),
            company_name="Gold ETF",
            sector="Commodities",
        )
        universe = Universe(universe.entries + [extra])
        logger.info("Added gold ETF %s to universe", gold_symbol)

    return universe


def refresh_universe_file(config: Optional[Config] = None) -> int:
    """Download the current index constituent list from NSE.

    Resolves the correct URL from UNIVERSE_URLS based on the configured
    universe.file path. Falls back to Nifty 500 if the file is not mapped.
    """
    import requests

    config = config or get_config()
    path = config.path("universe.file", "data/nifty500.csv")
    rel = str(Path(config.get("universe.file", "data/nifty500.csv")))
    url = UNIVERSE_URLS.get(rel, NSE_NIFTY500_URL)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Accept": "text/csv,*/*",
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(response.text, encoding="utf-8")
    _load_universe_cached.cache_clear()

    count = len(_read_csv(path))
    logger.info("Refreshed universe file with %d symbols from %s", count, url)
    return count
