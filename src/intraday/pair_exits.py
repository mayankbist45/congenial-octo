"""Pair-level z-score exit logic for spread and xstat personas."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional, Tuple

import pandas as pd

from src.config import Config
from src.intraday.cross_stat_math import hedge_beta, residual_z_at
from src.intraday.spread_math import spread_z_at


def is_pair_strategy(strategy: str) -> bool:
    return strategy in ("pair_zscore", "cross_zscore")


def pair_z_config_keys(config: Config) -> Tuple[float, float, int]:
    """Return (exit_z, stop_z, min_bars) for the active two-leg persona."""
    if config.persona_id == "xstat":
        exit_z = float(config.get("intraday.cross_exit_z", 0.25) or 0.25)
        stop_z = float(config.get("intraday.cross_stop_z", 2.5) or 2.5)
        min_bars = int(config.get("intraday.cross_min_bars", 25) or 25)
    else:
        exit_z = float(config.get("intraday.pair_exit_z", 0.20) or 0.20)
        stop_z = float(config.get("intraday.pair_stop_z", 2.5) or 2.5)
        min_bars = int(config.get("intraday.pair_min_bars", 20) or 20)
    return exit_z, stop_z, min_bars


def z_at_bar(
    config: Config,
    strategy: str,
    frame_a: pd.DataFrame,
    frame_b: pd.DataFrame,
    ts: pd.Timestamp,
    *,
    beta: Optional[float] = None,
) -> Optional[float]:
    """Spread or residual z-score at a bar timestamp."""
    _, _, min_bars = pair_z_config_keys(config)
    fa = frame_a.loc[frame_a.index <= ts]
    fb = frame_b.loc[frame_b.index <= ts]
    if fa.empty or fb.empty:
        return None
    if strategy == "cross_zscore":
        lookback = int(config.get("intraday.cross_beta_lookback", 60) or 60)
        return residual_z_at(
            fa,
            fb,
            beta=beta,
            beta_lookback=lookback,
            min_bars=min_bars,
        )
    return spread_z_at(fa, fb, min_bars=min_bars)


def pair_exit_reason(
    z: float,
    exit_z: float,
    stop_z: float,
) -> Optional[str]:
    if abs(z) <= exit_z:
        return "pair_convergence"
    if abs(z) >= stop_z:
        return "pair_stop"
    return None


def aligned_future_bars(
    frame_a: pd.DataFrame,
    frame_b: pd.DataFrame,
    entry_time: datetime,
    max_exit_time: Optional[datetime],
) -> pd.DatetimeIndex:
    """Bar timestamps present in both legs after entry."""
    entry_ts = pd.Timestamp(entry_time)
    fa = frame_a.loc[frame_a.index > entry_ts]
    fb = frame_b.loc[frame_b.index > entry_ts]
    if max_exit_time is not None:
        cap = pd.Timestamp(max_exit_time)
        fa = fa.loc[fa.index <= cap]
        fb = fb.loc[fb.index <= cap]
    common = fa.index.intersection(fb.index)
    return common.sort_values()


def resolve_pair_exit(
    config: Config,
    strategy: str,
    frame_a: pd.DataFrame,
    frame_b: pd.DataFrame,
    entry_time: datetime,
    max_exit_time: Optional[datetime],
    *,
    beta: Optional[float] = None,
) -> Tuple[Optional[datetime], Optional[str], Optional[float]]:
    """
    Walk synchronized bars; return (exit_time, exit_reason, exit_z).
    Falls back to force square-off at last common bar if no z trigger.
    """
    exit_z, stop_z, _ = pair_z_config_keys(config)
    common = aligned_future_bars(frame_a, frame_b, entry_time, max_exit_time)
    if common.empty:
        return None, None, None

    for ts in common:
        z = z_at_bar(config, strategy, frame_a, frame_b, ts, beta=beta)
        if z is None:
            continue
        reason = pair_exit_reason(z, exit_z, stop_z)
        if reason:
            return ts.to_pydatetime(), reason, z

    last_ts = common[-1].to_pydatetime()
    last_z = z_at_bar(config, strategy, frame_a, frame_b, common[-1], beta=beta)
    return last_ts, "time_exit", last_z


def pair_leg_quantities(
    config: Config,
    equity: float,
    entry_a: float,
    stop_a: float,
    entry_b: float,
    beta: float,
) -> Tuple[int, int]:
    """Beta-adjusted leg sizes for approximate dollar neutrality."""
    risk_amount = equity * float(config.get("intraday.risk_per_trade_pct", 0.4) or 0.4) / 100.0
    max_value = equity * float(config.get("intraday.max_position_value_pct", 20.0) or 20.0) / 100.0
    risk_a = abs(entry_a - stop_a)
    if entry_a <= 0 or entry_b <= 0 or risk_a <= 0:
        return 0, 0
    qty_a = int(min(risk_amount / risk_a, max_value / entry_a))
    if qty_a < 1:
        return 0, 0
    notional_a = qty_a * entry_a
    hedge = abs(beta) if beta else 1.0
    qty_b = int(min(notional_a * hedge / entry_b, max_value / entry_b))
    if qty_b < 1:
        return 0, 0
    return qty_a, qty_b
