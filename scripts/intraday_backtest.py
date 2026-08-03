#!/usr/bin/env python3
"""Run an intraday paper backtest for Calan or another intraday persona."""

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
from src.intraday.ranking import IntradayRanker
from src.intraday.reporting import save_summary, summarize_result
from src.intraday.simulator import IntradaySimulator
from src.logging_setup import setup_logging
from src.universe import load_universe


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _previous_day(frame: pd.DataFrame) -> Dict[str, float]:
    if frame.empty:
        return {}
    return {
        "high": float(frame["high"].max()),
        "low": float(frame["low"].min()),
        "close": float(frame["close"].iloc[-1]),
    }


def _rank_backtest_symbols(
    config, frames: Dict[str, pd.DataFrame], previous: Dict[str, Dict[str, float]], limit: int
) -> list[str]:
    ranker = IntradayRanker(config)
    totals: Dict[str, float] = {}
    counts: Dict[str, int] = {}
    for symbol, frame in frames.items():
        for day in sorted({ts.date() for ts in frame.index}):
            day_frame = frame.loc[frame.index.date == day]
            ranked = ranker.rank({symbol: day_frame}, previous)
            if not ranked:
                continue
            totals[symbol] = totals.get(symbol, 0.0) + ranked[0].score
            counts[symbol] = counts.get(symbol, 0) + 1
    ordered = sorted(
        totals,
        key=lambda symbol: totals[symbol] / max(counts.get(symbol, 1), 1),
        reverse=True,
    )
    return ordered[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest an intraday persona.")
    parser.add_argument("--persona", default="calan")
    parser.add_argument("--start", type=_parse_date, default=date.today())
    parser.add_argument("--end", type=_parse_date, default=date.today())
    parser.add_argument("--max-symbols", type=int, default=25)
    parser.add_argument(
        "--candidate-pool",
        type=int,
        default=120,
        help="how many universe symbols to fetch before ranking down to --max-symbols",
    )
    parser.add_argument("--interval", default=None)
    parser.add_argument("--force-simulated", action="store_true")
    args = parser.parse_args()
    setup_logging(level="INFO")

    config = get_persona_config(args.persona)
    interval = (args.interval or config.get("intraday.default_interval", "FIVE_MINUTE")).upper()
    broker = build_broker(config, force_simulated=args.force_simulated)
    try:
        broker.connect()
        data = IntradayDataService(config, broker=broker)
        universe = load_universe(config)

        frames: Dict[str, pd.DataFrame] = {}
        previous: Dict[str, Dict[str, float]] = {}
        pool_size = max(args.candidate_pool, args.max_symbols)
        symbols = universe.symbols[:pool_size]
        print(
            f"Fetching {interval} candles for {len(symbols)} symbol(s) "
            f"from {args.start} to {args.end}; Calan will rank the top "
            f"{args.max_symbols} trader-friendly names."
        )
        for idx, symbol in enumerate(symbols, start=1):
            print(f"  [{idx:>2}/{len(symbols)}] {symbol}", flush=True)
            frame = data.candles_range(symbol, args.start, args.end, interval=interval)
            if not frame.empty:
                frames[symbol] = frame.sort_index()
                first_day = frame.index[0].date()
                previous[symbol] = _previous_day(frame.loc[frame.index.date == first_day])

        ranked_symbols = _rank_backtest_symbols(config, frames, previous, args.max_symbols)
        print("Selected symbols:", ", ".join(ranked_symbols) if ranked_symbols else "none")
        selected_frames = {symbol: frames[symbol] for symbol in ranked_symbols if symbol in frames}
        selected_previous = {symbol: previous.get(symbol, {}) for symbol in ranked_symbols}

        result = IntradaySimulator(config).run(selected_frames, selected_previous, args.start, args.end)
        summary = summarize_result(result)
        path = save_summary(config, summary)
    finally:
        broker.disconnect()

    print(f"\nCalan intraday backtest ({summary['label']})")
    print(f"  Period:       {summary['start_date']} to {summary['end_date']}")
    print(f"  Trades:       {summary['total_trades']}")
    print(f"  Net P&L:      Rs {summary['total_net_pnl']:,.2f}")
    print(f"  Return:       {summary['total_return_pct']:+.2f}%")
    print(f"  Win rate:     {summary['win_rate_pct']:.1f}%")
    print(f"  Expectancy:   {summary['expectancy_r']:+.3f}R")
    print(f"  Max drawdown: {summary['max_drawdown_pct']:.2f}%")
    print(f"  Saved:        {path}")


if __name__ == "__main__":
    main()
