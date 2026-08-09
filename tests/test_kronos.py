from __future__ import annotations

import copy
from datetime import datetime

import pandas as pd

from src.config import get_persona_config
from src.intraday.engine_helpers import signal_score
from src.intraday.kronos_data import build_kronos_window
from src.intraday.kronos_features import build_kronos_market_features
from src.intraday.kronos_service import KronosForecastResult, maybe_enrich_ranked_with_kronos
from src.intraday.models import IntradaySignal, RankedStock


def _frame(periods: int = 140, freq: str = "5min") -> pd.DataFrame:
    idx = pd.date_range("2026-08-03 09:15", periods=periods, freq=freq)
    closes = [100.0 + i * 0.1 for i in range(periods)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": [100_000] * periods,
        },
        index=idx,
    )


def _ranked() -> RankedStock:
    return RankedStock(
        symbol="RELIANCE",
        score=80.0,
        turnover=2e8,
        volume_expansion=2.0,
        range_pct=1.0,
        gap_pct=1.0,
        vwap_distance_pct=0.3,
        prior_level_distance_pct=0.4,
        trend_cleanliness=0.7,
        in_play=True,
    )


def _signal(side: str = "BUY") -> IntradaySignal:
    return IntradaySignal(
        symbol="RELIANCE",
        strategy="opening_range_breakout",
        side=side,
        entry_time=datetime(2026, 8, 3, 10, 0),
        entry_price=100.0,
        stop_price=99.5,
        target_price=101.0,
        reason="test",
    )


def test_build_kronos_window_truncates_to_configured_lookback():
    config = get_persona_config("calan")
    window = build_kronos_window(config, "RELIANCE", _frame())
    assert window is not None
    assert len(window.history) == 128
    assert window.pred_len == 3
    assert window.truncated is True
    assert len(window.y_timestamp) == 3


def test_build_kronos_market_features_reports_bullish_bias():
    history = _frame(periods=20).tail(10)
    pred_idx = pd.date_range(history.index[-1] + pd.Timedelta(minutes=5), periods=3, freq="5min")
    forecast = pd.DataFrame(
        {
            "open": [102.0, 102.2, 102.3],
            "high": [102.3, 102.5, 102.7],
            "low": [101.9, 102.0, 102.2],
            "close": [102.2, 102.4, 102.6],
            "volume": [100_000, 100_000, 100_000],
            "amount": [10_220_000, 10_240_000, 10_260_000],
        },
        index=pred_idx,
    )
    features = build_kronos_market_features(history, forecast)
    assert features["trend_bias"] == "bullish"
    assert features["expected_return_pct"] > 0
    assert features["confidence"] >= 0


def test_maybe_enrich_ranked_with_kronos_populates_ranked_fields(monkeypatch):
    config = get_persona_config("calan")
    ranked = [_ranked()]
    frames = {"RELIANCE": _frame()}

    class StubService:
        def score_frames(self, _frames):
            return {
                "RELIANCE": KronosForecastResult(
                    symbol="RELIANCE",
                    available=True,
                    mode="shadow",
                    model_version="stub",
                    quality_score=0.9,
                    expected_return_pct=0.8,
                    expected_range_pct=1.2,
                    confidence=0.7,
                    trend_bias="bullish",
                    rationale="kronos bullish",
                    source="kronos",
                )
            }

    monkeypatch.setattr("src.intraday.kronos_service.get_kronos_service", lambda _cfg: StubService())
    maybe_enrich_ranked_with_kronos(config, frames, ranked)
    assert ranked[0].kronos_enabled is True
    assert ranked[0].kronos_score == 0.9
    assert ranked[0].kronos_rationale == "kronos bullish"


def test_signal_score_uses_kronos_bonus_in_advisory_mode():
    config = get_persona_config("calan")
    raw = copy.deepcopy(config.raw)
    raw["intraday"]["kronos"]["mode"] = "advisory"
    from src.config import Config

    cfg = Config(raw=raw, secrets=config.secrets, root=config.root, persona_id=config.persona_id)
    ranked = _ranked()
    ranked.kronos_enabled = True
    ranked.kronos_mode = "advisory"
    ranked.kronos_score = 1.0
    ranked.kronos_expected_return_pct = 0.8
    ranked.kronos_confidence = 0.8
    ranked.kronos_trend_bias = "bullish"
    frame = _frame(periods=20).tail(12)
    buy_score = signal_score(_signal("BUY"), ranked, frame, cfg)
    sell_score = signal_score(_signal("SELL"), ranked, frame, cfg)
    assert buy_score > sell_score
