"""Translate Kronos forecasts into trading-friendly features."""

from __future__ import annotations

from typing import Any, Dict

import pandas as pd

from src.config import Config
from src.intraday.models import IntradaySignal, RankedStock


def build_kronos_market_features(
    history: pd.DataFrame,
    forecast: pd.DataFrame,
) -> Dict[str, Any]:
    if history.empty or forecast.empty:
        return {
            "quality_score": 0.0,
            "confidence": 0.0,
            "expected_return_pct": 0.0,
            "expected_range_pct": 0.0,
            "trend_bias": "neutral",
            "rationale": "empty history or forecast",
        }

    last_close = float(history["close"].iloc[-1])
    end_close = float(forecast["close"].iloc[-1])
    high_max = float(forecast["high"].max())
    low_min = float(forecast["low"].min())
    expected_return_pct = ((end_close / last_close) - 1.0) * 100.0 if last_close > 0 else 0.0
    expected_range_pct = ((high_max / low_min) - 1.0) * 100.0 if low_min > 0 else 0.0

    diffs = forecast["close"].astype(float).diff().dropna()
    if diffs.empty:
        directional_consistency = 0.5
    else:
        up = int((diffs > 0).sum())
        down = int((diffs < 0).sum())
        directional_consistency = abs(up - down) / max(len(diffs), 1)
    move_strength = min(abs(expected_return_pct) / max(expected_range_pct, 0.05), 1.5) / 1.5
    confidence = max(0.0, min(1.0, move_strength * 0.7 + directional_consistency * 0.3))

    trend_threshold = 0.10
    if expected_return_pct > trend_threshold:
        trend_bias = "bullish"
    elif expected_return_pct < -trend_threshold:
        trend_bias = "bearish"
    else:
        trend_bias = "neutral"

    quality_score = abs(expected_return_pct) * confidence
    return {
        "quality_score": round(quality_score, 4),
        "confidence": round(confidence, 4),
        "expected_return_pct": round(expected_return_pct, 4),
        "expected_range_pct": round(expected_range_pct, 4),
        "trend_bias": trend_bias,
        "rationale": (
            f"kronos {trend_bias} {expected_return_pct:+.2f}% "
            f"conf={confidence:.2f} range={expected_range_pct:.2f}%"
        ),
    }


def kronos_score_adjustment(
    config: Config,
    signal: IntradaySignal,
    ranked: RankedStock,
) -> float:
    if not ranked.kronos_enabled or ranked.kronos_score is None or ranked.kronos_confidence is None:
        return 0.0
    if str(ranked.kronos_mode or "shadow").lower() == "shadow":
        return 0.0

    min_conf = float(config.get("intraday.kronos.min_confidence", 0.55) or 0.55)
    weight = float(config.get("intraday.kronos.score_weight", 12.0) or 12.0)
    if ranked.kronos_confidence < min_conf:
        return -weight * 0.25

    expected_return_pct = float(ranked.kronos_expected_return_pct or 0.0)
    direction = 1.0 if signal.side.upper() == "BUY" else -1.0
    aligned_return = expected_return_pct * direction
    alignment = 1.0 if aligned_return > 0 else -1.0 if aligned_return < 0 else 0.0
    magnitude = min(abs(aligned_return), 2.5) / 2.5
    confidence = float(ranked.kronos_confidence)
    return round(weight * magnitude * confidence * alignment, 4)
