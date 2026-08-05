"""Shared intraday engine helpers for watch + simulator."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.config import Config
from src.intraday.models import IntradaySignal, RankedStock
from src.intraday.strategies import load_strategies, scan_cross_signals, scan_pair_signals


STRATEGY_PRIORITY = {
    "opening_range_breakout": 4.0,
    "vwap_pullback": 3.0,
    "vwap_fade": 3.5,
    "opening_gap_fade": 3.2,
    "range_revert": 1.0,
    "ofi_momentum": 3.5,
    "ofi_exhaustion": 2.5,
    "pair_zscore": 4.0,
    "cross_zscore": 4.0,
    "twap_slice": 1.0,
    "vwap_schedule": 1.0,
    "scalping": 2.8,
}


def signal_score(
    signal: IntradaySignal,
    ranked: RankedStock,
    frame: pd.DataFrame,
) -> float:
    risk = signal.risk_per_share
    reward = abs(signal.target_price - signal.entry_price)
    expected_r = reward / risk if risk > 0 else 0.0
    latest_volume = float(frame["volume"].iloc[-1])
    avg_volume = float(frame["volume"].tail(12).mean())
    volume_confirm = latest_volume / avg_volume if avg_volume > 0 else 1.0
    priority = STRATEGY_PRIORITY.get(signal.strategy, 0.0)
    return ranked.score + expected_r * 18.0 + min(volume_confirm, 4.0) * 5.0 + priority


def collect_signals(
    config: Config,
    frames: Dict[str, pd.DataFrame],
    ranked: List[RankedStock],
    previous_days: Optional[Dict[str, Dict[str, float]]] = None,
) -> List[Tuple[float, IntradaySignal, RankedStock]]:
    previous_days = previous_days or {}
    strategies = load_strategies(config)
    out: List[Tuple[float, IntradaySignal, RankedStock]] = []
    ranked_map = {r.symbol: r for r in ranked}

    if config.persona_id == "spread":
        pair_map = scan_pair_signals(config, frames)
        for symbol, signals in pair_map.items():
            item = ranked_map.get(symbol)
            frame = frames.get(symbol)
            if item is None or frame is None:
                continue
            for signal in signals:
                out.append((signal_score(signal, item, frame), signal, item))
        out.sort(key=lambda row: (-row[0], row[1].entry_time))
        return out

    if config.persona_id == "xstat":
        cross_map = scan_cross_signals(config, frames)
        for symbol, signals in cross_map.items():
            item = ranked_map.get(symbol)
            frame = frames.get(symbol)
            if item is None or frame is None:
                continue
            for signal in signals:
                out.append((signal_score(signal, item, frame), signal, item))
        out.sort(key=lambda row: (-row[0], row[1].entry_time))
        return out

    for item in ranked:
        frame = frames.get(item.symbol)
        if frame is None or frame.empty:
            continue
        best: Optional[Tuple[float, IntradaySignal]] = None
        for strategy in strategies:
            for signal in strategy.signals(
                item.symbol, frame, config, previous_days.get(item.symbol)
            ):
                score = signal_score(signal, item, frame)
                if best is None or score > best[0]:
                    best = (score, signal)
        if best:
            out.append((best[0], best[1], item))
    out.sort(key=lambda row: (-row[0], row[1].entry_time))
    return out
