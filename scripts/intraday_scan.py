#!/usr/bin/env python3
"""Scan today's intraday candles and list Calan's eligible playbooks."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Dict

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.connectors.registry import build_broker
from src.config import get_persona_config
from src.intraday.data import IntradayDataService
from src.intraday.scanner import IntradayScanner
from src.logging_setup import bind_log_context, make_run_label, setup_logging
from src.universe import load_universe


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan intraday candidates.")
    parser.add_argument("--persona", default="calan")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--max-symbols", type=int, default=25)
    parser.add_argument("--interval", default=None)
    parser.add_argument("--force-simulated", action="store_true")
    args = parser.parse_args()
    config = get_persona_config(args.persona)
    setup_logging(
        level=str(config.get("logging.level", "INFO")),
        log_file=config.path("logging.file", "data/fund.log"),
        persona_log_dir=config.path("logging.persona_dir", "data/logs/personas"),
    )
    bind_log_context(
        persona_id=config.persona_id,
        run_label=make_run_label("intraday_scan", config.persona_id),
        command="intraday_scan",
        market_id=config.market_id,
    )
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

    candidates = IntradayScanner(config).scan(frames)
    print(f"\nCalan scan for {args.date} ({interval})")
    if not candidates:
        print("  No eligible intraday setups found.")
        return
    for candidate in candidates[:20]:
        print(
            f"  {candidate.symbol:<14} {', '.join(candidate.strategies):<55} "
            f"range={candidate.atr_pct:.2f}% turnover=Rs {candidate.turnover:,.0f}"
        )


if __name__ == "__main__":
    main()
