"""Tests for the live earnings integration."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.earnings.analyzer import EarningsAnalyzer
from src.earnings.brief import build_brief, latest_period
from src.earnings.prompts import validate_payload
from src.earnings.service import EarningsService
from src.earnings.triggers import EarningsTriggerEngine
from src.models import (
    EarningsAnalysis,
    EarningsDigest,
    PortfolioState,
    Position,
    TriggerType,
)


QUARTERS = pd.DataFrame(
    [
        {
            "quarter_label": "Jun 2024",
            "period_end": "2024-06-30",
            "sales_cr": 100.0,
            "operating_profit_cr": 20.0,
            "opm_pct": 0.20,
            "net_profit_cr": 12.0,
            "eps_rs": 10.0,
        },
        {
            "quarter_label": "Jun 2025",
            "period_end": "2025-06-30",
            "sales_cr": 90.0,
            "operating_profit_cr": 14.0,
            "opm_pct": 0.15,
            "net_profit_cr": 8.0,
            "eps_rs": 6.0,
        },
    ]
)


def test_build_brief_includes_yoy_rows() -> None:
    brief = build_brief("TEST", "Test Co", "IT", QUARTERS, date(2025, 6, 30))
    assert "Jun 2025" in brief
    assert "Jun 2024" in brief
    assert latest_period(QUARTERS) == date(2025, 6, 30)


def test_rules_analyzer_flags_miss(temp_config) -> None:
    analyzer = EarningsAnalyzer(temp_config)
    analysis = analyzer._analyze_rules(
        "TEST", date(2025, 6, 30), QUARTERS, "brief"
    )
    assert analysis.verdict == "miss"
    assert analysis.thesis_impact in ("weakens", "broken")
    assert not analysis.generated_by_llm


def test_validate_payload_requires_exact_schema() -> None:
    payload = validate_payload(
        {
            "verdict": "beat",
            "quality_of_earnings": 8,
            "red_flags": [],
            "thesis_impact": "strengthens",
            "rationale": "Revenue and margins expanded together.",
            "confidence": 0.7,
        }
    )
    assert payload is not None
    assert payload["verdict"] == "beat"


def test_earnings_triggers_review_on_broken_thesis(temp_config) -> None:
    portfolio = PortfolioState(
        as_of=date(2025, 7, 1),
        cash=100_000.0,
        positions=[
            Position(
                symbol="RELIANCE",
                quantity=10,
                avg_cost=1000.0,
                last_price=1100.0,
                sector="Energy",
            )
        ],
    )
    digest = EarningsDigest(
        as_of=date(2025, 7, 1),
        analyses={
            "RELIANCE": EarningsAnalysis(
                symbol="RELIANCE",
                period_end=date(2025, 6, 30),
                verdict="miss",
                thesis_impact="broken",
                rationale="Profit collapsed while sales stalled.",
            )
        },
    )
    engine = EarningsTriggerEngine()
    triggers = engine.evaluate(
        digest, portfolio, candidate_symbols=set(), is_deep_review=False
    )
    assert len(triggers) == 1
    assert triggers[0].trigger_type == TriggerType.EARNINGS_EVENT
    assert triggers[0].symbol == "RELIANCE"


def test_earnings_service_respects_config_flag(temp_config) -> None:
    temp_config.raw.setdefault("earnings", {})["enabled"] = False
    service = EarningsService(temp_config)
    assert service.enabled is False


def test_earnings_service_enabled(temp_config) -> None:
    temp_config.raw.setdefault("earnings", {})["enabled"] = True
    service = EarningsService(temp_config)
    assert service.enabled is True


def test_results_season_covers_post_quarter_window() -> None:
    assert EarningsService.is_results_season(date(2025, 7, 15))
    assert not EarningsService.is_results_season(date(2025, 5, 20))
