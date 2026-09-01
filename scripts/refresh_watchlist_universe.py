#!/usr/bin/env python3
"""Refresh the wide NSE equity universe used for watchlists and user personas."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.instruments import InstrumentMaster
from src.config import PROJECT_ROOT, get_config
from src.logging_setup import setup_logging
from src.universe import _load_universe_cached, _read_csv, is_investable_equity_symbol

_INDEX_METADATA_FILES = (
    PROJECT_ROOT / "data" / "nifty500.csv",
    PROJECT_ROOT / "data" / "nifty50.csv",
    PROJECT_ROOT / "data" / "nifty_smallcap250.csv",
)


def _load_index_metadata() -> dict[str, tuple[str, str]]:
    """Merge company name + sector from published index constituent lists."""
    overlay: dict[str, tuple[str, str]] = {}
    for path in _INDEX_METADATA_FILES:
        if not path.exists():
            continue
        for entry in _read_csv(path):
            if entry.sector and entry.sector != "Unknown":
                overlay[entry.symbol] = (entry.company_name, entry.sector)
            else:
                overlay.setdefault(entry.symbol, (entry.company_name, entry.sector))
    return overlay


def _write_from_instruments(path: Path) -> int:
    master = InstrumentMaster(get_config())
    master.load()
    metadata = _load_index_metadata()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Symbol", "Company Name", "Industry"])
        count = 0
        for base in sorted(master._by_symbol.keys()):
            if not is_investable_equity_symbol(base):
                continue
            company, sector = metadata.get(base, (base, ""))
            writer.writerow([base, company, sector])
            count += 1
    _load_universe_cached.cache_clear()
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh watchlist/persona NSE equity universe")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "data" / "nse_equity_all.csv"),
        help="Destination CSV path",
    )
    args = parser.parse_args()
    setup_logging(get_config().get("logging.level", "INFO"))
    path = Path(args.output)
    try:
        count = _write_from_instruments(path)
    except Exception as exc:
        print(f"Instrument refresh failed ({exc}); keeping existing file if present.")
        if not path.exists():
            return 1
        count = len(_load_universe_cached(str(path)).symbols)
    print(f"Watchlist universe ready: {count} symbols at {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
