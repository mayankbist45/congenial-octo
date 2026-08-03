#!/usr/bin/env python3
"""Backtest the calan intraday persona on historical candles."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.factory import build_market_data_broker
from src.config import get_persona_config
from src.intraday.data import IntradayDataService
from src.intraday.reporting import save_summary, summarize_result
from src.intraday.simulator import IntradaySimulator
from src.logging_setup import setup_logging
from src.universe import load_universe


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest an intraday persona.")
    parser.add_argument("--persona", default="calan")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--max-symbols", type=int, default=12)
    parser.add_argument("--interval", default="FIVE_MINUTE")
    parser.add_argument("--force-simulated", action="store_true")
    args = parser.parse_args()

    config = get_persona_config(args.persona)
    setup_logging(
        level=str(config.get("logging.level", "INFO")),
        log_file=config.path("logging.file", "data/fund.log"),
    )

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    broker = build_market_data_broker(config, force_simulated=args.force_simulated)
    broker.connect()
    try:
        data = IntradayDataService(config, broker=broker)
        universe = load_universe(config)
        frames = {}
        for symbol in universe.symbols[: args.max_symbols]:
            frame = data.candles_range(symbol, start, end, interval=args.interval.upper())
            if not frame.empty:
                frames[symbol] = frame
        result = IntradaySimulator(config).run(frames, {}, start, end)
        summary = summarize_result(result)
        path = save_summary(config, summary)
        print(f"Trades: {summary['total_trades']}  Net P&L: Rs {summary['total_net_pnl']:,.2f}")
        print(f"Expectancy: {summary['expectancy_r']:+.3f}R  Saved: {path}")
    finally:
        broker.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
