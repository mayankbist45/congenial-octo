#!/usr/bin/env python3
"""Force-fetch and store recent quarterly report history.

Typical usage:

    .venv/bin/python scripts/backfill_quarterly_reports.py
    .venv/bin/python scripts/backfill_quarterly_reports.py --persona balanced
    .venv/bin/python scripts/backfill_quarterly_reports.py --limit 50
    .venv/bin/python scripts/backfill_quarterly_reports.py --symbols TCS INFY RELIANCE

By default this warms the active persona's universe with the latest 4 quarters,
which is roughly one year's worth of report history.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_config, get_persona_config
from src.fundamentals.reports import QuarterlyReportsService
from src.logging_setup import setup_logging
from src.universe import load_universe


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill quarterly report history")
    parser.add_argument(
        "--persona",
        default="balanced",
        help="persona whose universe should be used (default: balanced)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="backfill only the first N symbols from the persona universe",
    )
    parser.add_argument(
        "--max-quarters",
        type=int,
        default=4,
        metavar="N",
        help="how many most-recent quarters to store per symbol (default: 4)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        metavar="SYMBOL",
        help="explicit symbol list; overrides persona universe",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log at DEBUG level",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    base = get_config()
    setup_logging(
        level="DEBUG" if args.verbose else str(base.get("logging.level", "INFO")),
        log_file=base.path("logging.file", "data/fund.log"),
    )

    config = get_persona_config(args.persona)
    if args.symbols:
        symbols = [symbol.upper() for symbol in args.symbols]
    else:
        symbols = load_universe(config).symbols
        if args.limit:
            symbols = symbols[: args.limit]

    service = QuarterlyReportsService(config)
    try:
        summary = service.backfill_symbols(
            symbols=symbols,
            as_of=date(2026, 8, 3),
            max_quarters=args.max_quarters,
        )
    finally:
        service.close()

    print(f"Persona: {args.persona}")
    print(f"Symbols attempted: {len(symbols)}")
    print(f"Quarter rows stored/updated: {summary['stored']}")
    print(f"Latest-quarter analyses refreshed: {summary['analysed']}")
    print(f"Errors: {summary['errors']}")
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
