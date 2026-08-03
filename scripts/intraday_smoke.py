#!/usr/bin/env python3
"""Smoke-test calan intraday logic with synthetic 5-min candles (no broker)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_persona_config
from src.intraday.agent import IntradayAgent
from src.intraday.ranking import IntradayRanker
from src.intraday.reporting import save_summary, summarize_result
from src.intraday.scanner import IntradayScanner
from src.intraday.simulator import IntradaySimulator
from src.intraday.strategies import load_strategies


def _synthetic_day(gap_pct: float = 1.5, breakout: bool = True) -> pd.DataFrame:
    """Build a full session that passes ranker + ORB (09:45 window, 15-min range)."""
    base = 100.0
    open_price = base * (1 + gap_pct / 100.0)
    idx = pd.date_range("2026-08-04 09:15", periods=48, freq="5min")  # through ~13:10
    closes: list[float] = []
    volumes: list[int] = []
    for i, _ in enumerate(idx):
        t = idx[i].time()
        if t < pd.Timestamp("09:45").time():
            # pre-window: modest drift, elevated volume for "in play"
            closes.append(open_price + i * 0.05)
            volumes.append(180_000)
        elif t <= pd.Timestamp("09:55").time():
            # opening range (3 x 5m bars inside 09:45 session)
            closes.append(open_price + 0.15)
            volumes.append(160_000)
        elif breakout and t == pd.Timestamp("10:00").time():
            # ORB long: close above range high, volume spike, above VWAP
            closes.append(open_price + 1.25)
            volumes.append(420_000)
        elif breakout:
            closes.append(closes[-1] + 0.2)
            volumes.append(200_000)
        else:
            closes.append(closes[-1] + 0.05)
            volumes.append(120_000)
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.25 for c in closes]
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )


def main() -> int:
    config = get_persona_config("calan")
    symbols = ["RELIANCE", "INFY", "WIPRO", "HDFCBANK", "TCS"]
    prev = {s: {"close": 100.0, "high": 101.0, "low": 99.0} for s in symbols}
    frames = {
        symbols[0]: _synthetic_day(gap_pct=1.8, breakout=True),
        **{s: _synthetic_day(gap_pct=1.0 + i * 0.15, breakout=False) for i, s in enumerate(symbols[1:], 1)},
    }

    ranked = IntradayRanker(config).rank(frames, prev)
    in_play = [r for r in ranked if r.in_play]
    agent = IntradayAgent(config)
    catalyst = agent.catalyst_scan(in_play or ranked, frames, frames["RELIANCE"].index[8].to_pydatetime())
    candidates = IntradayScanner(config).scan(frames, prev)
    result = IntradaySimulator(config, agent=agent).run(frames, prev, date(2026, 8, 4), date(2026, 8, 4))
    summary = summarize_result(result)
    path = save_summary(config, summary)

    print("=== Calan intraday smoke test (synthetic data) ===")
    print(f"  strategies     {[s.name for s in load_strategies(config)]}")
    print(f"  ranked         {len(ranked)} symbols, {len(in_play)} in_play")
    print(f"  watchlist      {len(catalyst.get('watchlist', []))} names")
    print(f"  setups         {len(candidates)} candidate(s)")
    print(f"  trades         {summary['total_trades']}")
    print(f"  net P&L        Rs {summary['total_net_pnl']:,.2f}")
    print(f"  expectancy     {summary['expectancy_r']:+.3f}R")
    print(f"  strategy P&L   {summary.get('strategy_pnl', {})}")
    print(f"  saved          {path}")
    if summary["total_trades"]:
        for t in summary["trades"]:
            print(
                f"    {t['symbol']} {t['strategy']} {t['side']} "
                f"net={t['net_pnl']:+.2f} ({t['exit_reason']})"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
