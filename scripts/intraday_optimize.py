#!/usr/bin/env python3
"""Grid-search Calan intraday config variants on historical candles."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.connectors.registry import build_market_data_broker
from src.config import Config, get_persona_config
from src.intraday.agent import IntradayAgent
from src.intraday.data import IntradayDataService
from src.intraday.reporting import summarize_result
from src.intraday.simulator import IntradaySimulator
from src.intraday.spread_math import scan_symbols
from src.universe import load_universe


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _config_with_overrides(persona_id: str, overrides: dict) -> Config:
    base = get_persona_config(persona_id)
    raw = _deep_merge(base.raw, overrides)
    return Config(
        raw=raw,
        secrets=base.secrets,
        root=base.root,
        persona_id=persona_id,
    )


@dataclass
class Variant:
    name: str
    overrides: dict
    rules_only: bool = True


VARIANTS: List[Variant] = [
    Variant("baseline_llm", {}, rules_only=False),
    Variant("baseline_rules", {}, rules_only=True),
    Variant(
        "orb_vwap_only",
        {"intraday": {"strategies": ["opening_range_breakout", "vwap_pullback"]}},
    ),
    Variant(
        "orb_vwap_loose",
        {
            "intraday": {
                "strategies": ["opening_range_breakout", "vwap_pullback"],
                "stocks_in_play": {
                    "min_trend_at_signal": 0.30,
                    "min_or_range_pct": 0.30,
                    "min_rr_orb": 1.20,
                    "min_rr_vwap": 1.35,
                },
                "agent": {"min_trade_confidence": 0.55},
            }
        },
    ),
    Variant(
        "orb_vwap_core",
        {
            "intraday": {
                "strategies": ["opening_range_breakout", "vwap_pullback"],
                "volume_spike_multiple": 1.15,
                "target_r_multiple": 1.5,
                "stocks_in_play": {
                    "min_gap_pct": 0.6,
                    "min_rvol": 1.8,
                    "min_trend_at_signal": 0.30,
                    "min_or_range_pct": 0.30,
                    "min_rr_orb": 1.15,
                    "min_rr_vwap": 1.30,
                    "min_stop_pct": 0.18,
                },
                "agent": {"min_trade_confidence": 0.52, "min_watch_confidence": 0.50},
            }
        },
    ),
    Variant(
        "all_strategies_tuned",
        {
            "intraday": {
                "strategies": ["opening_range_breakout", "vwap_pullback", "scalping"],
                "scalping_stop_pct": 0.18,
                "scalping_target_pct": 0.55,
                "scalping_min_net_edge_bps": 35.0,
                "stocks_in_play": {
                    "min_trend_at_signal": 0.30,
                    "min_rr_orb": 1.20,
                    "min_rr_vwap": 1.35,
                    "min_rr_scalping": 1.35,
                },
                "agent": {"min_trade_confidence": 0.55, "veto_scalping": False},
            }
        },
    ),
]


def run_variant(
    config: Config,
    frames: dict,
    previous_days: dict,
    start: date,
    end: date,
    rules_only: bool,
) -> dict:
    agent = IntradayAgent(config)
    if rules_only:
        agent._llm = MagicMock()
        agent._llm.available = False
    result = IntradaySimulator(config, agent=agent).run(frames, previous_days, start, end)
    summary = summarize_result(result)
    by_strat: Dict[str, int] = {}
    for t in summary.get("trades", []):
        by_strat[t["strategy"]] = by_strat.get(t["strategy"], 0) + 1
    return {
        "trades": summary["total_trades"],
        "net_pnl": summary["total_net_pnl"],
        "return_pct": summary["total_return_pct"],
        "win_rate": summary["win_rate_pct"],
        "expectancy_r": summary["expectancy_r"],
        "profit_factor": summary.get("profit_factor"),
        "strategy_pnl": summary.get("strategy_pnl", {}),
        "strategy_counts": by_strat,
        "daily_pnl": summary.get("daily_pnl", {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Optimize calan intraday configs.")
    parser.add_argument("--persona", default="calan")
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--end", default="2026-08-04")
    parser.add_argument("--max-symbols", type=int, default=12)
    parser.add_argument("--out", default="data/intraday/calan_optimize.json")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    base = get_persona_config(args.persona)

    broker = build_market_data_broker(base)
    broker.connect()
    try:
        data = IntradayDataService(base, broker=broker)
        universe = load_universe(base)
        symbols = scan_symbols(base, universe.symbols, args.max_symbols)
        frames = {}
        for symbol in symbols:
            frame = data.candles_range(symbol, start, end, interval="FIVE_MINUTE")
            if not frame.empty:
                frames[symbol] = frame
        previous_days = data.previous_day_stats_batch(
            list(frames.keys()), start, interval="FIVE_MINUTE"
        )
        days = sorted({d for f in frames.values() for d in f.index.date if start <= d <= end})
        print(f"Loaded {len(frames)} symbols, {len(days)} trading days ({days[0]} .. {days[-1]})")

        results = []
        for variant in VARIANTS:
            config = _config_with_overrides(args.persona, variant.overrides)
            metrics = run_variant(
                config, frames, previous_days, start, end, variant.rules_only
            )
            row = {"variant": variant.name, **metrics}
            results.append(row)
            print(
                f"{variant.name:22}  trades={metrics['trades']:3d}  "
                f"PnL={metrics['net_pnl']:+10.2f}  "
                f"ret={metrics['return_pct']:+.2f}%  "
                f"E={metrics['expectancy_r']:+.3f}R  "
                f"strats={metrics['strategy_counts']}"
            )

        results.sort(key=lambda r: r["net_pnl"], reverse=True)
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "symbols": len(frames),
            "trading_days": len(days),
            "results": results,
            "best": results[0] if results else None,
        }
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nBest: {results[0]['variant']}  PnL Rs {results[0]['net_pnl']:+,.2f}")
        print(f"Saved: {out_path}")
    finally:
        broker.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
