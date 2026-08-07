"""Persist intraday gate decisions for meta-label training."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from src.config import Config
from src.intraday.fundamental_style import get_style_profile
from src.intraday.models import IntradaySignal, RankedStock
from src.journal.repository import JournalRepository
from src.logging_setup import get_logger

logger = get_logger(__name__)


def meta_logging_enabled(config: Config) -> bool:
    return bool(config.get("intraday.meta.log_decisions", False))


def build_gate_features(
    config: Config,
    signal: IntradaySignal,
    ranked: RankedStock,
    *,
    estimated_costs: float = 0.0,
    watchlist_entry: Optional[Dict[str, object]] = None,
) -> Dict[str, Any]:
    """Structured feature vector for meta-label training."""
    risk = abs(signal.entry_price - signal.stop_price)
    reward = abs(signal.target_price - signal.entry_price)
    gross_rr = (reward / risk) if risk > 0 else None
    features: Dict[str, Any] = {
        "strategy": signal.strategy,
        "side": signal.side,
        "hour_of_day": signal.entry_time.hour + signal.entry_time.minute / 60.0,
        "gap_pct": ranked.gap_pct,
        "trend_cleanliness": ranked.trend_cleanliness,
        "volume_expansion": ranked.volume_expansion,
        "vwap_distance_pct": ranked.vwap_distance_pct,
        "prior_level_distance_pct": ranked.prior_level_distance_pct,
        "range_pct": ranked.range_pct,
        "rank_score": ranked.score,
        "in_play": ranked.in_play,
        "gross_rr": round(gross_rr, 4) if gross_rr is not None else None,
        "estimated_costs": round(float(estimated_costs), 2),
        "ofi_z": ranked.ofi_z,
        "spread_z": ranked.spread_z,
    }
    if watchlist_entry:
        for key in (
            "confidence",
            "catalyst_class",
            "playbook",
            "fundamental_style",
            "pe_ratio",
            "peg_ratio",
        ):
            if key in watchlist_entry and watchlist_entry[key] is not None:
                features[key] = watchlist_entry[key]
    if config.get("intraday.fundamentals.enabled", False):
        try:
            profile = get_style_profile(config, signal.symbol)
        except Exception:
            profile = None
        if profile is not None:
            features.update(
                {
                    "fundamental_style": profile.label,
                    "intraday_bias": profile.intraday_bias,
                    "pe_ratio": profile.pe_ratio,
                    "peg_ratio": profile.peg_ratio,
                    "earnings_growth_pct": profile.earnings_growth_pct,
                    "revenue_growth_pct": profile.revenue_growth_pct,
                }
            )
    return features


def log_gate_decision(
    config: Config,
    signal: IntradaySignal,
    ranked: RankedStock,
    gate: dict,
    *,
    watchlist_entry: Optional[Dict[str, object]] = None,
    estimated_costs: float = 0.0,
    model_version: Optional[str] = None,
    model_score: Optional[float] = None,
    news_refs: Optional[list] = None,
    repo: Optional[JournalRepository] = None,
) -> Optional[int]:
    """Write one gate row to meta_decisions. Failures are logged, never raised."""
    if not meta_logging_enabled(config):
        return None
    try:
        decision = "take" if gate.get("decision") == "approve" else "skip"
        model_version = model_version or gate.get("model_version")
        model_score = (
            model_score if model_score is not None else gate.get("model_score")
        )
        method = gate.get("gate_method")
        if not method:
            if model_version:
                method = "model"
            elif gate.get("generated_by_llm"):
                method = "llm"
            else:
                method = "rules"
        row = {
            "decision_id": str(uuid.uuid4()),
            "symbol": signal.symbol,
            "decision_ts": signal.entry_time.isoformat(timespec="seconds"),
            "strategy": signal.strategy,
            "side": signal.side,
            "gate_decision": decision,
            "gate_method": method,
            "model_version": model_version,
            "model_score": model_score,
            "features": build_gate_features(
                config,
                signal,
                ranked,
                estimated_costs=estimated_costs,
                watchlist_entry=watchlist_entry,
            ),
            "rationale": gate.get("rationale"),
            "news_refs": news_refs or [],
            "gate_reason": None if decision == "take" else gate.get("rationale"),
        }
        journal = repo or JournalRepository(config)
        return journal.insert_meta_decision(row)
    except Exception as exc:
        logger.warning("Could not log meta decision for %s: %s", signal.symbol, exc)
        return None
