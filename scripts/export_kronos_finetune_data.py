#!/usr/bin/env python3
"""Export intraday candles into a Kronos-friendly fine-tuning dataset."""

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
    parser = argparse.ArgumentParser(description="Export fine-tuning candles for Kronos.")
    parser.add_argument("--persona", default="calan_us")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--max-symbols", type=int, default=12)
    parser.add_argument("--interval", default="FIVE_MINUTE")
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--force-simulated", action="store_true")
    parser.add_argument("--output-dir", default="data/kronos/exports")
    args = parser.parse_args()

    config = get_persona_config(args.persona)
    setup_logging(
        level=str(config.get("logging.level", "INFO")),
        log_file=config.path("logging.file", "data/fund.log"),
        persona_log_dir=config.path("logging.persona_dir", "data/logs/personas"),
    )
    bind_log_context(
        persona_id=config.persona_id,
        run_label=make_run_label("export_kronos_finetune_data", config.persona_id),
        command="export_kronos_finetune_data",
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
        frames = _load_frames(data, symbols, start, end, args.interval.upper(), cache_only=args.cache_only)
    finally:
        if broker is not None:
            broker.disconnect()

    if not frames:
        print("No candle data loaded for export.")
        return 1

    rows: List[pd.DataFrame] = []
    for symbol, frame in frames.items():
        part = frame.copy().reset_index()
        ts_col = str(part.columns[0])
        part = part.rename(columns={ts_col: "timestamp"})
        part["timestamp"] = pd.to_datetime(part["timestamp"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        part["symbol"] = symbol
        if "amount" not in part.columns:
            part["amount"] = part["close"].astype(float) * part["volume"].fillna(0.0).astype(float)
        rows.append(part.loc[:, ["symbol", "timestamp", "open", "high", "low", "close", "volume", "amount"]])

    dataset = pd.concat(rows, ignore_index=True)
    out_dir = config.root / args.output_dir / config.persona_id
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{start.isoformat()}_{end.isoformat()}_{args.interval.upper().lower()}"
    csv_path = out_dir / f"{stem}.csv"
    manifest_path = out_dir / f"{stem}_manifest.json"
    dataset.to_csv(csv_path, index=False)
    manifest = {
        "persona_id": config.persona_id,
        "market_id": config.market_id,
        "interval": args.interval.upper(),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "symbol_count": len(frames),
        "row_count": int(len(dataset)),
        "symbols": sorted(frames.keys()),
        "csv_path": str(csv_path),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Exported {manifest['row_count']} rows across {manifest['symbol_count']} symbols")
    print(f"CSV: {csv_path}")
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
