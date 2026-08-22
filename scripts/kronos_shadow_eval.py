#!/usr/bin/env python3
"""Offline shadow evaluation for Kronos on intraday candles."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.factory import build_market_data_broker
from src.config import get_persona_config
from src.intraday.data import IntradayDataService
from src.intraday.kronos_eval import build_kronos_eval_rows, summarize_kronos_eval
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
    frames: Dict[str, pd.DataFrame] = {}
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
    parser = argparse.ArgumentParser(description="Evaluate Kronos shadow forecasts offline.")
    parser.add_argument("--persona", default="calan_us")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--max-symbols", type=int, default=8)
    parser.add_argument("--interval", default="FIVE_MINUTE")
    parser.add_argument("--stride", type=int, default=3)
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--force-simulated", action="store_true")
    parser.add_argument("--output-dir", default="data/kronos")
    args = parser.parse_args()

    config = get_persona_config(args.persona)
    setup_logging(
        level=str(config.get("logging.level", "INFO")),
        log_file=config.path("logging.file", "data/fund.log"),
        persona_log_dir=config.path("logging.persona_dir", "data/logs/personas"),
    )
    bind_log_context(
        persona_id=config.persona_id,
        run_label=make_run_label("kronos_shadow_eval", config.persona_id),
        command="kronos_shadow_eval",
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
                cached = {key.split("|", 1)[0].upper() for key in data._cache if "|" in key}
                preferred = [s for s in universe.symbols if s.upper() in cached]
                if preferred:
                    symbols = scan_symbols(config, preferred, args.max_symbols)
        frames = _load_frames(data, symbols, start, end, args.interval.upper(), cache_only=args.cache_only)
        if not frames:
            print("No candle data loaded for Kronos evaluation.")
            return 1
        rows = build_kronos_eval_rows(config, frames, stride=args.stride)
    finally:
        if broker is not None:
            broker.disconnect()

    summary = summarize_kronos_eval(rows)
    out_dir = config.root / args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{config.persona_id}_{start.isoformat()}_{end.isoformat()}"
    csv_path = out_dir / f"{stem}_shadow_eval.csv"
    json_path = out_dir / f"{stem}_shadow_eval_summary.json"
    pd.DataFrame([row.to_dict() for row in rows]).to_csv(csv_path, index=False)
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"Rows: {summary['rows']}  Symbols: {summary.get('symbols', 0)}")
    print(f"Directional hit rate: {summary['directional_hit_rate']}")
    print(f"Expected/realized corr: {summary['expected_realized_corr']}")
    print(f"CSV: {csv_path}")
    print(f"Summary: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
