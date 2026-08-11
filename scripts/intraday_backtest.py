#!/usr/bin/env python3
"""Backtest the calan intraday persona on historical candles."""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.factory import build_market_data_broker
from src.config import get_persona_config
from src.intraday.data import IntradayDataService
from src.intraday.reporting import save_summary, summarize_result
from src.intraday.simulator import IntradaySimulator
from src.intraday.spread_math import scan_symbols
from src.logging_setup import bind_log_context, make_run_label, setup_logging
from src.universe import load_universe


def _trading_days(start: date, end: date) -> Iterable[date]:
    day = start
    while day <= end:
        if day.weekday() < 5:
            yield day
        day += timedelta(days=1)


def _load_frames(
    data: IntradayDataService,
    symbols: List[str],
    start: date,
    end: date,
    interval: str,
    *,
    cache_only: bool,
) -> Dict[str, pd.DataFrame]:
    """Load candles per session day so the on-disk cache is used when possible."""
    frames: Dict[str, pd.DataFrame] = {}
    interval = interval.upper()
    for symbol in symbols:
        parts: List[pd.DataFrame] = []
        for day in _trading_days(start, end):
            frame = data.candles(
                symbol,
                day,
                interval=interval,
                use_cache=True,
                refresh=not cache_only,
            )
            if not frame.empty:
                parts.append(frame)
        if parts:
            frames[symbol] = pd.concat(parts).sort_index()
    return frames


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest an intraday persona.")
    parser.add_argument("--persona", default="calan")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--max-symbols", type=int, default=12)
    parser.add_argument("--interval", default="FIVE_MINUTE")
    parser.add_argument("--force-simulated", action="store_true")
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Use cached intraday candles only (no broker API calls).",
    )
    args = parser.parse_args()

    config = get_persona_config(args.persona)
    setup_logging(
        level=str(config.get("logging.level", "INFO")),
        log_file=config.path("logging.file", "data/fund.log"),
        persona_log_dir=config.path("logging.persona_dir", "data/logs/personas"),
    )
    bind_log_context(
        persona_id=config.persona_id,
        run_label=make_run_label("intraday_backtest", config.persona_id),
        command="intraday_backtest",
        market_id=config.market_id,
    )

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    broker = None
    if not args.cache_only:
        broker = build_market_data_broker(config, force_simulated=args.force_simulated)
        broker.connect()
    try:
        data = IntradayDataService(config, broker=broker)
        universe = load_universe(config)
        symbols = scan_symbols(config, universe.symbols, args.max_symbols)
        if args.cache_only:
            data._load_cache()
            if data._cache:
                cached = {
                    key.split("|", 1)[0].upper()
                    for key in data._cache
                    if "|" in key
                }
                preferred = [s for s in universe.symbols if s.upper() in cached]
                if preferred:
                    symbols = scan_symbols(config, preferred, args.max_symbols)
        frames = _load_frames(
            data,
            symbols,
            start,
            end,
            args.interval,
            cache_only=args.cache_only,
        )
        if not frames:
            print("No candle data loaded — try --cache-only after a watch run, or widen the date range.")
            return 1
        previous_days = data.previous_day_stats_batch(
            list(frames.keys()), start, interval=args.interval.upper()
        )
        result = IntradaySimulator(config).run(frames, previous_days, start, end)
        summary = summarize_result(result)
        path = save_summary(config, summary)
        print(f"Symbols: {len(frames)}  Trades: {summary['total_trades']}  Net P&L: Rs {summary['total_net_pnl']:,.2f}")
        print(f"Expectancy: {summary['expectancy_r']:+.3f}R  Saved: {path}")
    finally:
        if broker is not None:
            broker.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
