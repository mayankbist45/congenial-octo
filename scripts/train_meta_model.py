#!/usr/bin/env python3
"""Train (or bootstrap) the lightweight meta-label model for a persona."""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.connectors.registry import build_market_data_broker
from src.config import get_persona_config
from src.intraday.data import IntradayDataService
from src.intraday.meta_model import MetaLabelModel, default_model_path
from src.intraday.meta_train import merge_training_rows, rows_from_frames, rows_from_journal
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
    parser = argparse.ArgumentParser(description="Train meta-label take/skip model.")
    parser.add_argument("--persona", default="calan")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--max-symbols", type=int, default=50)
    parser.add_argument("--interval", default="FIVE_MINUTE")
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument(
        "--journal-only",
        action="store_true",
        help="Train only from labeled meta_decisions in the journal DB.",
    )
    parser.add_argument(
        "--backtest-only",
        action="store_true",
        help="Train only from historical candle backtest labels.",
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
        run_label=make_run_label("train_meta_model", config.persona_id),
        command="train_meta_model",
        market_id=config.market_id,
    )

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = (
        date.fromisoformat(args.start)
        if args.start
        else end - timedelta(days=21)
    )

    journal_rows: List[dict] = []
    backtest_rows: List[dict] = []

    if not args.backtest_only:
        journal_rows = rows_from_journal(config)
        print(f"Journal labeled rows: {len(journal_rows)}")

    if not args.journal_only:
        broker = None
        if not args.cache_only:
            broker = build_market_data_broker(config)
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
                args.interval.upper(),
                cache_only=args.cache_only,
            )
            prev = data.previous_day_stats_batch(
                list(frames.keys()), start, interval=args.interval.upper()
            )
            backtest_rows = rows_from_frames(config, frames, prev, start, end)
            print(f"Backtest labeled rows: {len(backtest_rows)}")
        finally:
            if broker is not None:
                broker.disconnect()

    rows = merge_training_rows(journal_rows, backtest_rows)
    if len(rows) < 10:
        print(
            f"Not enough labeled rows ({len(rows)}). "
            "Need >= 10 from journal and/or --cache-only backtest."
        )
        return 1

    model, metrics = MetaLabelModel.train(rows)
    path = default_model_path(config)
    model.save(path)
    print(f"Saved {metrics['version']} → {path}")
    print(
        f"n={metrics['n_samples']}  pos_rate={metrics['pos_rate']}  "
        f"train_acc={metrics['train_accuracy']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
