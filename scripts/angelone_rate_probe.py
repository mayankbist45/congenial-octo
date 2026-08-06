#!/usr/bin/env python3
"""Pre-market probe for Angel One candle fetch reliability."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.factory import build_market_data_broker
from src.config import get_persona_config
from src.logging_setup import setup_logging
from src.universe import load_universe


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Angel One candle rate limits.")
    parser.add_argument("--persona", default="calan")
    parser.add_argument("--symbols", type=int, default=10)
    parser.add_argument(
        "--spacing",
        type=float,
        default=None,
        help="Override angelone.candle_budget_spacing_seconds for this run",
    )
    args = parser.parse_args()

    config = get_persona_config(args.persona)
    setup_logging(
        level=str(config.get("logging.level", "INFO")),
        log_file=config.path("logging.file", "data/fund.log"),
    )
    if args.spacing is not None:
        config.raw.setdefault("angelone", {})["candle_budget_spacing_seconds"] = args.spacing

    universe = load_universe(config)
    symbols = universe.symbols[: max(args.symbols, 1)]
    day = date.today()
    from_dt = datetime.combine(day, datetime.strptime("09:15", "%H:%M").time())
    to_dt = datetime.now()

    broker = build_market_data_broker(config)
    broker.connect()
    ok = 0
    throttled = 0
    latencies: list[float] = []
    try:
        for symbol in symbols:
            start = time.time()
            try:
                rows = broker.get_candles(symbol, from_dt, to_dt, "FIVE_MINUTE")
            except Exception as exc:
                message = str(exc).lower()
                if "rate" in message or "too many" in message:
                    throttled += 1
                latencies.append(time.time() - start)
                continue
            latencies.append(time.time() - start)
            if rows:
                ok += 1
            else:
                throttled += 1
    finally:
        broker.disconnect()

    total = len(symbols)
    success_pct = (ok / total * 100.0) if total else 0.0
    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
    spacing = config.get("angelone.candle_budget_spacing_seconds", 4.0)

    print(f"probe: {ok}/{total} OK ({success_pct:.0f}%), {throttled} failed/empty")
    print(f"probe: avg latency {avg_latency:.2f}s, spacing config {spacing}s")
    if success_pct >= 80.0:
        print("probe: PASS — safe to start intraday watch")
        return 0
    print("probe: FAIL — do not start watch until spacing increased or data2 disabled")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
