"""Tests for meta_decisions journal table and gate logging."""

from __future__ import annotations

from datetime import datetime

from src.intraday.meta_log import build_gate_features, log_gate_decision, meta_logging_enabled
from src.intraday.models import IntradaySignal, RankedStock
from src.journal.db import connect
from src.journal.repository import JournalRepository


def _signal() -> IntradaySignal:
    return IntradaySignal(
        symbol="INFY",
        strategy="scalping",
        side="BUY",
        entry_time=datetime(2026, 8, 8, 10, 15),
        entry_price=1500.0,
        stop_price=1495.0,
        target_price=1510.0,
        reason="test",
    )


def _ranked() -> RankedStock:
    return RankedStock(
        symbol="INFY",
        score=82.0,
        turnover=120_000_000.0,
        volume_expansion=2.1,
        range_pct=1.2,
        gap_pct=0.9,
        vwap_distance_pct=0.4,
        prior_level_distance_pct=0.2,
        trend_cleanliness=0.65,
        in_play=True,
        reasons=["gap", "rvol"],
    )


def test_meta_decisions_schema_exists(temp_config) -> None:
    connect(temp_config)
    repo = JournalRepository(temp_config)
    row_id = repo.insert_meta_decision(
        {
            "decision_id": "test-decision-1",
            "symbol": "INFY",
            "decision_ts": "2026-08-08T10:15:00",
            "strategy": "scalping",
            "side": "BUY",
            "gate_decision": "skip",
            "gate_method": "rules",
            "features": {"gap_pct": 0.9},
            "rationale": "low confidence",
            "news_refs": [],
            "gate_reason": "low confidence",
        }
    )
    assert row_id > 0
    [stored] = repo.meta_decisions(limit=1)
    assert stored["decision_id"] == "test-decision-1"
    assert stored["features"]["gap_pct"] == 0.9


def test_update_meta_decision_outcome(temp_config) -> None:
    repo = JournalRepository(temp_config)
    repo.insert_meta_decision(
        {
            "decision_id": "test-decision-2",
            "symbol": "TCS",
            "decision_ts": "2026-08-08T11:00:00",
            "gate_decision": "take",
            "gate_method": "rules",
            "features": {},
            "news_refs": [],
        }
    )
    assert repo.update_meta_decision_outcome(
        "test-decision-2",
        outcome_label=1,
        meta_label=1,
        net_pnl=120.5,
        exit_reason="upper_barrier",
    )
    [stored] = repo.meta_decisions(limit=1)
    assert stored["meta_label"] == 1
    assert stored["net_pnl"] == 120.5
    assert stored["labeled_at"] is not None


def test_log_gate_decision_respects_config_flag(temp_config) -> None:
    import copy

    from src.config import Config, get_persona_config

    base = get_persona_config("calan", base=temp_config)
    raw = copy.deepcopy(base.raw)
    raw.setdefault("intraday", {})["meta"] = {"log_decisions": True}
    raw.setdefault("intraday", {})["fundamentals"] = {"enabled": False}
    cfg = Config(
        raw=raw,
        secrets=base.secrets,
        root=base.root,
        persona_id="calan",
    )
    assert meta_logging_enabled(cfg)
    row_id = log_gate_decision(
        cfg,
        _signal(),
        _ranked(),
        {"decision": "veto", "rationale": "spread too wide"},
    )
    assert row_id is not None
    repo = JournalRepository(cfg)
    rows = repo.meta_decisions(limit=5)
    assert any(r["symbol"] == "INFY" and r["gate_decision"] == "skip" for r in rows)


def test_build_gate_features_includes_ranker_fields(temp_config) -> None:
    from src.config import Config, get_persona_config
    import copy

    base = get_persona_config("calan", base=temp_config)
    cfg = Config(
        raw=copy.deepcopy(base.raw),
        secrets=base.secrets,
        root=base.root,
        persona_id="calan",
    )
    features = build_gate_features(cfg, _signal(), _ranked())
    assert features["gap_pct"] == 0.9
    assert features["gross_rr"] is not None
    assert features["strategy"] == "scalping"
