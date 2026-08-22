#!/usr/bin/env python3
"""Load Kronos and run one small real forecast for deployment validation."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_persona_config
from src.intraday.kronos_service import maybe_enrich_ranked_with_kronos
from src.intraday.models import RankedStock
from src.logging_setup import bind_log_context, make_run_label, setup_logging


def _frame(periods: int = 140, freq: str = "5min") -> pd.DataFrame:
    idx = pd.date_range("2026-08-10 09:30", periods=periods, freq=freq)
    closes = [200.0 + i * 0.15 for i in range(periods)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.25 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": [50_000] * periods,
        },
        index=idx,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a small Kronos runtime check.")
    parser.add_argument("--persona", default="calan_us")
    args = parser.parse_args()

    config = get_persona_config(args.persona)
    setup_logging(
        level=str(config.get("logging.level", "INFO")),
        log_file=config.path("logging.file", "data/fund.log"),
        persona_log_dir=config.path("logging.persona_dir", "data/logs/personas"),
    )
    bind_log_context(
        persona_id=config.persona_id,
        run_label=make_run_label("kronos_runtime_check", config.persona_id),
        command="kronos_runtime_check",
        market_id=config.market_id,
    )

    ranked = [
        RankedStock(
            symbol="AAPL",
            score=75.0,
            turnover=1e8,
            volume_expansion=1.8,
            range_pct=1.1,
            gap_pct=0.9,
            vwap_distance_pct=0.2,
            prior_level_distance_pct=0.3,
            trend_cleanliness=0.6,
            in_play=True,
        )
    ]
    frames = {"AAPL": _frame()}
    started = time.perf_counter()
    maybe_enrich_ranked_with_kronos(config, frames, ranked)
    elapsed = time.perf_counter() - started
    item = ranked[0]
    print(f"persona={config.persona_id} elapsed_s={elapsed:.2f}")
    print(f"kronos_enabled={item.kronos_enabled}")
    print(f"kronos_score={item.kronos_score}")
    print(f"kronos_expected_return_pct={item.kronos_expected_return_pct}")
    print(f"kronos_confidence={item.kronos_confidence}")
    print(f"kronos_rationale={item.kronos_rationale}")
    return 0 if item.kronos_enabled else 1


if __name__ == "__main__":
    raise SystemExit(main())
