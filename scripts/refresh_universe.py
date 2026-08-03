#!/usr/bin/env python3
"""Download the latest NSE index constituent lists.

Supports multiple indices (Nifty 500, Nifty Smallcap 250, etc.) as defined in
UNIVERSE_URLS. Run with --all to refresh every mapped universe file, or with no
arguments to refresh only the default (Nifty 500).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_config, get_persona_config, enabled_personas
from src.logging_setup import setup_logging
from src.universe import UNIVERSE_URLS, load_universe, refresh_universe_file


def _refresh_one(config) -> None:
    universe_file = config.get("universe.file", "data/nifty500.csv")
    path = config.path("universe.file", "data/nifty500.csv")

    try:
        before = set(load_universe(config).symbols)
    except FileNotFoundError:
        before = set()

    count = refresh_universe_file(config)
    after = set(load_universe(config).symbols)

    added = sorted(after - before)
    removed = sorted(before - after)

    print(f"\n[{universe_file}] Refreshed: {count} symbols")
    if added:
        print(f"  added ({len(added)}): {', '.join(added)}")
    if removed:
        print(f"  removed ({len(removed)}): {', '.join(removed)}")
        print(
            "  NOTE: review any holdings in the removed list - they may no "
            "longer meet the fund's liquidity mandate."
        )
    if not added and not removed:
        print("  no composition change")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh NSE universe files")
    parser.add_argument(
        "--all", action="store_true",
        help="Refresh universe files for all enabled personas",
    )
    args = parser.parse_args()

    base_config = get_config()
    setup_logging(base_config.get("logging.level", "INFO"))

    if args.all:
        seen: set = set()
        for persona_id in enabled_personas():
            config = get_persona_config(persona_id)
            universe_file = config.get("universe.file", "data/nifty500.csv")
            if universe_file in seen:
                continue
            seen.add(universe_file)
            _refresh_one(config)
    else:
        _refresh_one(base_config)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
