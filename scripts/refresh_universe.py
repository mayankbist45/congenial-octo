#!/usr/bin/env python3
"""Download the latest Nifty 500 constituent list from NSE.

Index composition changes twice a year, so this is worth running quarterly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_config
from src.logging_setup import setup_logging
from src.universe import load_universe, refresh_universe_file


def main() -> int:
    config = get_config()
    setup_logging(config.get("logging.level", "INFO"))

    try:
        before = set(load_universe(config).symbols)
    except FileNotFoundError:
        before = set()

    count = refresh_universe_file(config)
    after = set(load_universe(config).symbols)

    added = sorted(after - before)
    removed = sorted(before - after)

    print(f"Universe refreshed: {count} symbols")
    if added:
        print(f"  added ({len(added)}): {', '.join(added)}")
    if removed:
        print(f"  removed ({len(removed)}): {', '.join(removed)}")
        print(
            "  NOTE: review any holdings in the removed list - they have left "
            "the Nifty 500 and may no longer meet the fund's liquidity mandate."
        )
    if not added and not removed:
        print("  no composition change")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
