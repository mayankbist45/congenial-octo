#!/usr/bin/env python3
"""Verify the installation: config validity, database, universe, credentials.

Run this first after cloning:  python scripts/setup_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_config
from src.journal.db import init_db, is_postgres
from src.universe import load_universe

OK = "[ ok ]"
WARN = "[warn]"
FAIL = "[fail]"


def main() -> int:
    failures = 0
    warnings = 0

    print("AI Mutual Fund Manager - setup check\n")

    config = get_config()
    print(f"{OK} settings.yaml loaded")

    problems = config.validate()
    if problems:
        for problem in problems:
            print(f"{FAIL} config: {problem}")
        failures += len(problems)
    else:
        print(f"{OK} configuration validated")

    try:
        universe = load_universe(config)
        sectors = universe.sectors()
        print(f"{OK} universe: {len(universe)} symbols across {len(sectors)} sectors")
    except Exception as exc:
        print(f"{FAIL} universe: {exc}")
        failures += 1

    try:
        print(f"{OK} journal ready: {init_db(config)}")
        if not is_postgres(config.db_url):
            print(
                f"{WARN} using the SQLite fallback - fine for a trial, but set "
                f"database.url to PostgreSQL before running the fund for real"
            )
            warnings += 1
    except Exception as exc:
        print(f"{FAIL} database: {exc}")
        failures += 1

    secrets = config.secrets
    if secrets.has_broker_credentials:
        print(f"{OK} Angel One credentials present")
    else:
        print(
            f"{WARN} Angel One credentials missing - the fund will run in "
            f"simulated-portfolio mode (no broker holdings, no order placement)"
        )
        warnings += 1

    if secrets.has_llm_credentials:
        print(f"{OK} LLM credentials present")
    else:
        print(
            f"{WARN} LLM credentials missing - decisions will fall back to the "
            f"rules-only engine and news digests will be keyword-based"
        )
        warnings += 1

    if secrets.dry_run:
        print(f"{OK} DRY_RUN is enabled: no real orders can be placed")
    else:
        print(f"{WARN} DRY_RUN is disabled: approved trades will hit the market")
        warnings += 1

    print("\n--- policy summary ---")
    print(f"  benchmark              : {config.get('fund.benchmark_label')}")
    print(f"  target positions       : {config.get('fund.target_positions')}")
    print(f"  max position weight    : {config.get('risk.max_position_pct')}%")
    print(f"  max sector weight      : {config.get('risk.max_sector_pct')}%")
    print(f"  stop loss              : {config.get('risk.stop_loss_pct')}%")
    print(f"  drift band             : +/-{config.get('risk.drift_band_pct')}%")
    print(
        f"  turnover budget        : "
        f"{config.get('risk.annual_turnover_budget_pct')}%/year"
    )
    print(f"  order product          : {config.get('execution.product_type')}")

    print()
    if failures:
        print(f"{FAIL} {failures} blocking problem(s) found")
        return 1
    if warnings:
        print(f"{OK} setup usable, with {warnings} warning(s)")
        return 0
    print(f"{OK} all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
