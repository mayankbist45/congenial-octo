#!/usr/bin/env python3
"""Run Calan's live market-hours paper watch loop."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.factory import build_broker
from src.config import get_persona_config
from src.intraday.data import IntradayDataService
from src.intraday.watch import IntradayWatchEngine
from src.logging_setup import setup_logging
from src.universe import load_universe


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Calan's live paper watch loop.")
    parser.add_argument("--persona", default="calan")
    parser.add_argument("--max-symbols", type=int, default=None)
    parser.add_argument("--force-simulated", action="store_true")
    args = parser.parse_args()
    setup_logging(level="INFO")

    config = get_persona_config(args.persona)
    broker = build_broker(config, force_simulated=args.force_simulated)
    broker.connect()
    try:
        universe = load_universe(config)
        symbols = universe.symbols[: args.max_symbols] if args.max_symbols else universe.symbols
        data = IntradayDataService(config, broker=broker)
        state = IntradayWatchEngine(config, data, symbols).run_until_close()
        print(f"\nCalan watch finished for {state.trading_date}")
        print(f"  completed trades: {len(state.completed_trades)}")
        print(f"  open positions:   {len(state.open_positions)}")
        print(f"  net P&L:           Rs {state.daily_pnl:,.2f}")
        if state.lockout_reason:
            print(f"  lockout:           {state.lockout_reason}")
    finally:
        broker.disconnect()


if __name__ == "__main__":
    main()
