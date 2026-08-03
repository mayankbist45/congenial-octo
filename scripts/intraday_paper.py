#!/usr/bin/env python3
"""Run one paper-trading day for Calan using cached or broker candles."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Dict

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.factory import build_broker
from src.config import get_persona_config
from src.intraday.data import IntradayDataService
from src.intraday.reporting import save_summary, summarize_result
from src.intraday.simulator import IntradaySimulator
from src.logging_setup import setup_logging
from src.universe import load_universe


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one intraday paper day.")
    parser.add_argument("--persona", default="calan")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--max-symbols", type=int, default=25)
    parser.add_argument("--interval", default=None)
    parser.add_argument("--force-simulated", action="store_true")
    args = parser.parse_args()
    setup_logging(level="INFO")

    config = get_persona_config(args.persona)
    interval = (args.interval or config.get("intraday.default_interval", "FIVE_MINUTE")).upper()
    broker = build_broker(config, force_simulated=args.force_simulated)
    broker.connect()
    data = IntradayDataService(config, broker=broker)
    universe = load_universe(config)

    frames: Dict[str, pd.DataFrame] = {}
    for symbol in universe.symbols[: args.max_symbols]:
        frame = data.candles(symbol, args.date, interval=interval)
        if not frame.empty:
            frames[symbol] = frame

    result = IntradaySimulator(config).run(frames, {}, args.date, args.date)
    summary = summarize_result(result)
    path = save_summary(config, summary)
    print(f"\nCalan paper day ({summary['label']})")
    print(f"  Date:       {summary['start_date']}")
    print(f"  Trades:     {summary['total_trades']}")
    print(f"  Net P&L:    Rs {summary['total_net_pnl']:,.2f}")
    print(f"  Expectancy: {summary['expectancy_r']:+.3f}R")
    print(f"  Saved:      {path}")


if __name__ == "__main__":
    main()
