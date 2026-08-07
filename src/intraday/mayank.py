"""Mayank — hybrid intraday persona (growth→momentum, value→mean reversion)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from src.config import Config
from src.intraday.fundamental_style import StyleProfile, get_style_profile
from src.intraday.models import IntradaySignal, RankedStock
from src.intraday.strategies import load_strategies

MOMENTUM_STRATEGIES = frozenset(
    {"opening_range_breakout", "vwap_pullback", "scalping"}
)
REVERSION_STRATEGIES = frozenset(
    {"vwap_fade", "opening_gap_fade", "range_revert"}
)


def is_mayank_persona(config: Config) -> bool:
    from src.market.registry import persona_strategy_type

    return persona_strategy_type(config.persona_id) == "mayank"


def style_params_snapshot(config: Config) -> Dict[str, Any]:
    """Thresholds shown in the dashboard."""
    fund = dict(config.get("intraday.fundamentals", {}) or {})
    return {
        "high_pe_threshold": fund.get("high_pe_threshold", 35),
        "value_pe_threshold": fund.get("value_pe_threshold", 18),
        "min_growth_pct": fund.get("min_growth_pct", 8),
        "max_peg_growth": fund.get("max_peg_growth", 2.5),
        "allow_value_trap": fund.get("allow_value_trap", False),
        "momentum_strategies": sorted(MOMENTUM_STRATEGIES),
        "reversion_strategies": sorted(REVERSION_STRATEGIES),
    }


def profile_to_dict(profile: StyleProfile) -> Dict[str, Any]:
    return {
        "fundamental_style": profile.label,
        "intraday_bias": profile.intraday_bias,
        "pe_ratio": profile.pe_ratio,
        "peg_ratio": profile.peg_ratio,
        "earnings_growth_pct": profile.earnings_growth_pct,
        "revenue_growth_pct": profile.revenue_growth_pct,
        "style_rationale": profile.rationale,
        "fundamental_summary": profile.summary(),
        "preferred_strategies": sorted(
            strategies_for_profile(profile, allow_value_trap=False)
        ),
    }


def strategies_for_profile(
    profile: StyleProfile,
    *,
    allow_value_trap: bool = False,
) -> set[str]:
    if profile.label == "value_trap" and not allow_value_trap:
        return set()
    if profile.intraday_bias == "momentum":
        return set(MOMENTUM_STRATEGIES)
    if profile.intraday_bias == "mean_reversion":
        return set(REVERSION_STRATEGIES)
    return set(MOMENTUM_STRATEGIES) | set(REVERSION_STRATEGIES)


def allowed_strategy_names(config: Config, symbol: str) -> Optional[set[str]]:
    profile = get_style_profile(config, symbol)
    if profile is None:
        return None
    allow_trap = bool(
        (config.get("intraday.fundamentals", {}) or {}).get("allow_value_trap", False)
    )
    names = strategies_for_profile(profile, allow_value_trap=allow_trap)
    return names or None


def collect_mayank_signals(
    config: Config,
    frames: Dict[str, pd.DataFrame],
    ranked: List[RankedStock],
    previous_days: Optional[Dict[str, Dict[str, float]]] = None,
    signal_score_fn=None,
) -> List[Tuple[float, IntradaySignal, RankedStock]]:
    from src.intraday.engine_helpers import signal_score

    score_fn = signal_score_fn or signal_score
    previous_days = previous_days or {}
    all_strategies = load_strategies(config)
    strategy_map = {s.name: s for s in all_strategies}
    out: List[Tuple[float, IntradaySignal, RankedStock]] = []

    for item in ranked:
        frame = frames.get(item.symbol)
        if frame is None or frame.empty:
            continue
        allowed = allowed_strategy_names(config, item.symbol)
        if allowed is None:
            strategies = all_strategies
        else:
            strategies = [strategy_map[n] for n in allowed if n in strategy_map]
        if not strategies:
            continue
        best: Optional[Tuple[float, IntradaySignal]] = None
        for strategy in strategies:
            for signal in strategy.signals(
                item.symbol, frame, config, previous_days.get(item.symbol)
            ):
                score = score_fn(signal, item, frame)
                if best is None or score > best[0]:
                    best = (score, signal)
        if best:
            out.append((best[0], best[1], item))
    out.sort(key=lambda row: (-row[0], row[1].entry_time))
    return out


def build_style_context(
    config: Config,
    ranked: List[RankedStock],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    profiles: Dict[str, Dict[str, Any]] = {}
    for item in ranked:
        profile = get_style_profile(config, item.symbol)
        if profile is not None:
            profiles[item.symbol.upper()] = profile_to_dict(profile)
    return profiles, style_params_snapshot(config)


def enrich_candidates_with_style(
    candidates: List[Dict[str, Any]],
    profiles: Dict[str, Dict[str, Any]],
) -> None:
    for row in candidates:
        sym = str(row.get("symbol", "")).upper()
        if sym in profiles:
            row.update(profiles[sym])


def mayank_catalyst_rules(config: Config, ranked: List[RankedStock]) -> dict:
    """Hybrid watchlist: momentum picks for growth, fade picks for value."""
    min_conf = float(config.get("intraday.agent.min_watch_confidence", 0.55) or 0.55)
    min_fade_dist = float(config.get("intraday.min_fade_vwap_pct", 1.0) or 1.0)
    allow_trap = bool(
        (config.get("intraday.fundamentals", {}) or {}).get("allow_value_trap", False)
    )
    watchlist: List[Dict[str, object]] = []

    for item in ranked[:20]:
        profile = get_style_profile(config, item.symbol)
        if profile is None:
            continue
        if profile.label == "value_trap" and not allow_trap:
            continue

        base = profile_to_dict(profile)
        entry: Dict[str, object] = {
            "symbol": item.symbol,
            "in_play": True,
            **base,
        }

        if profile.intraday_bias == "momentum":
            if not item.in_play:
                continue
            conf = min(0.95, 0.55 + item.trend_cleanliness * 0.3)
            if item.volume_expansion >= 2.0:
                conf += 0.05
            if profile.label == "growth":
                conf = min(conf + 0.03, 0.95)
            entry.update(
                {
                    "playbook": "momentum",
                    "catalyst_class": "growth_momentum",
                    "direction_bias": "either",
                    "confidence": round(conf, 2),
                    "rationale": (
                        f"Growth/momentum: {'; '.join(item.reasons[:2])} "
                        f"({profile.summary()})"
                    ),
                }
            )
        elif profile.intraday_bias == "mean_reversion":
            vwap_dist = abs(item.vwap_distance_pct or 0.0)
            if vwap_dist < min_fade_dist:
                continue
            conf = min(0.95, 0.55 + vwap_dist * 0.08 + (1.0 - item.trend_cleanliness) * 0.15)
            entry.update(
                {
                    "playbook": "mean_reversion",
                    "fade_candidate": True,
                    "catalyst_class": "value_fade",
                    "direction_bias": "either",
                    "confidence": round(conf, 2),
                    "vwap_distance_pct": round(vwap_dist, 3),
                    "rationale": (
                        f"Value fade: vwap_dist={vwap_dist:.2f}% "
                        f"({profile.summary()})"
                    ),
                }
            )
        else:
            if not item.in_play:
                continue
            entry.update(
                {
                    "playbook": "neutral",
                    "catalyst_class": "neutral",
                    "direction_bias": "either",
                    "confidence": round(min(0.75, 0.5 + item.trend_cleanliness * 0.2), 2),
                    "rationale": f"Neutral style; quant in_play ({profile.summary()})",
                }
            )

        if float(entry.get("confidence", 0) or 0) >= min_conf:
            watchlist.append(entry)
        if len(watchlist) >= 8:
            break

    quality = "good" if len(watchlist) >= 2 else "fair" if watchlist else "poor"
    growth_n = sum(1 for w in watchlist if w.get("playbook") == "momentum")
    value_n = sum(1 for w in watchlist if w.get("playbook") == "mean_reversion")
    return {
        "session_view": (
            f"Mayank hybrid scan: {growth_n} growth/momentum, {value_n} value/fade candidates."
        ),
        "trade_day_quality": quality,
        "watchlist": watchlist,
        "generated_by_llm": False,
    }
