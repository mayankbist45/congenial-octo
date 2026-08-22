from __future__ import annotations

import copy
import os
from pathlib import Path

import pandas as pd

from src.config import Config, get_persona_config
from src.intraday.kronos_eval import build_kronos_eval_rows, summarize_kronos_eval
from src.intraday.kronos_service import KronosForecastResult, maybe_enrich_ranked_with_kronos
from src.intraday.models import RankedStock


def _frame(periods: int = 140, freq: str = "5min") -> pd.DataFrame:
    idx = pd.date_range("2026-08-10 09:30", periods=periods, freq=freq)
    closes = [200.0 + i * 0.1 for i in range(periods)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": [50_000] * periods,
        },
        index=idx,
    )


def test_remote_backend_populates_ranked_fields(monkeypatch):
    config = get_persona_config("calan_us")
    raw = copy.deepcopy(config.raw)
    raw["intraday"]["kronos"]["backend"] = "http"
    raw["intraday"]["kronos"]["endpoint_url"] = "http://kronos-internal:8090"
    cfg = Config(raw=raw, secrets=config.secrets, root=config.root, persona_id=config.persona_id)

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            future_idx = pd.date_range("2026-08-10 15:00", periods=3, freq="5min")
            return {
                "forecasts": [
                    {
                        "forecast": [
                            {
                                "timestamp": ts.isoformat(),
                                "open": 214.0 + i * 0.1,
                                "high": 214.2 + i * 0.1,
                                "low": 213.9 + i * 0.1,
                                "close": 214.1 + i * 0.1,
                                "volume": 50_000,
                                "amount": (214.1 + i * 0.1) * 50_000,
                            }
                            for i, ts in enumerate(future_idx)
                        ]
                    }
                ]
            }

    def fake_post(url, json, timeout):
        assert url == "http://kronos-internal:8090/predict"
        assert json["windows"][0]["symbol"] == "AAPL"
        assert timeout == 15.0
        return FakeResponse()

    monkeypatch.setattr("src.intraday.kronos_service.requests.post", fake_post)
    ranked = [
        RankedStock(
            symbol="AAPL",
            score=80.0,
            turnover=1e8,
            volume_expansion=1.5,
            range_pct=1.1,
            gap_pct=0.8,
            vwap_distance_pct=0.2,
            prior_level_distance_pct=0.3,
            trend_cleanliness=0.7,
            in_play=True,
        )
    ]
    maybe_enrich_ranked_with_kronos(cfg, {"AAPL": _frame()}, ranked)
    assert ranked[0].kronos_enabled is True
    assert ranked[0].kronos_score is not None
    assert ranked[0].kronos_rationale


def test_remote_backend_rejects_partial_forecast_response(monkeypatch):
    config = get_persona_config("calan_us")
    raw = copy.deepcopy(config.raw)
    raw["intraday"]["kronos"]["backend"] = "http"
    raw["intraday"]["kronos"]["endpoint_url"] = "http://kronos-internal:8090"
    cfg = Config(raw=raw, secrets=config.secrets, root=config.root, persona_id=config.persona_id)

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"forecasts": []}

    monkeypatch.setattr(
        "src.intraday.kronos_service.requests.post",
        lambda *_args, **_kwargs: FakeResponse(),
    )
    ranked = [
        RankedStock(
            symbol="AAPL",
            score=80.0,
            turnover=1e8,
            volume_expansion=1.5,
            range_pct=1.1,
            gap_pct=0.8,
            vwap_distance_pct=0.2,
            prior_level_distance_pct=0.3,
            trend_cleanliness=0.7,
            in_play=True,
        )
    ]

    results = maybe_enrich_ranked_with_kronos(cfg, {"AAPL": _frame()}, ranked)

    assert results["AAPL"].available is False
    assert "forecast count mismatch" in results["AAPL"].rationale
    assert ranked[0].kronos_enabled is False


def test_kronos_deployment_scripts_are_executable():
    root = Path(__file__).resolve().parent.parent
    for relative_path in (
        "scripts/bootstrap_kronos_vm.sh",
        "scripts/deploy_kronos_gcp.sh",
    ):
        assert os.access(root / relative_path, os.X_OK)


def test_build_kronos_eval_rows_and_summary(monkeypatch):
    config = get_persona_config("calan_us")

    class StubService:
        def score_frames(self, frames):
            symbol = next(iter(frames))
            return {
                symbol: KronosForecastResult(
                    symbol=symbol,
                    available=True,
                    mode="shadow",
                    model_version="stub",
                    quality_score=0.8,
                    expected_return_pct=0.5,
                    expected_range_pct=1.0,
                    confidence=0.7,
                    trend_bias="bullish",
                    rationale="kronos bullish",
                    source="kronos",
                )
            }

    monkeypatch.setattr("src.intraday.kronos_eval.get_kronos_service", lambda _cfg: StubService())
    rows = build_kronos_eval_rows(config, {"AAPL": _frame(periods=140)}, stride=20)
    assert rows
    assert rows[0].symbol == "AAPL"
    assert rows[0].market_id == "us_equity"
    assert rows[0].confidence == 0.7
    summary = summarize_kronos_eval(rows)
    assert summary["rows"] == len(rows)
    assert summary["symbols"] == 1
    assert summary["directional_hit_rate"] is not None
