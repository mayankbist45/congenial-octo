"""Sell-discipline tests.

These are the fund's most consequential rules, so each of the six professional
sell triggers is tested for both firing and not firing, and the "quiet day"
case - no trigger, no trade - is asserted explicitly, because that is what the
system should produce on most days.
"""

from __future__ import annotations

from typing import List

import pytest

from src.models import Action, NewsDigest, Severity, TriggerType
from src.risk.triggers import TriggerEngine
from tests.conftest import (
    make_diversified_portfolio,
    make_fundamental,
    make_portfolio,
    make_position,
    make_scores,
    make_technical,
)


@pytest.fixture
def engine(config) -> TriggerEngine:
    return TriggerEngine(config)


def types_of(triggers: List) -> set:
    return {t.trigger_type for t in triggers}


def find(triggers: List, trigger_type: TriggerType):
    return next((t for t in triggers if t.trigger_type == trigger_type), None)


# ----------------------------------------------------------------------
# The quiet day: this must be the common case
# ----------------------------------------------------------------------
def test_healthy_portfolio_fires_no_triggers(
    engine, diversified_technicals, diversified_scores, diversified_fundamentals
) -> None:
    portfolio = make_diversified_portfolio(cash=100_000)

    triggers = engine.evaluate(
        portfolio,
        diversified_scores,
        diversified_fundamentals,
        diversified_technicals,
    )

    assert triggers == [], f"unexpected triggers: {[t.detail for t in triggers]}"


def test_modest_gains_and_losses_do_not_trigger_trades(
    engine, diversified_technicals, diversified_scores, diversified_fundamentals
) -> None:
    """Ordinary daily drift must not produce trades: trade rarely, not daily."""
    portfolio = make_diversified_portfolio(cash=100_000)
    for index, position in enumerate(portfolio.positions):
        # Spread returns between -12% and +25%, all inside the trigger bands.
        position.last_price = position.avg_cost * (0.88 + index * 0.028)
        position.peak_price = max(position.avg_cost, position.last_price)

    triggers = engine.evaluate(
        portfolio,
        diversified_scores,
        diversified_fundamentals,
        diversified_technicals,
    )

    assert triggers == [], f"unexpected triggers: {[t.detail for t in triggers]}"


# ----------------------------------------------------------------------
# 1. Stop loss (down from cost)
# ----------------------------------------------------------------------
def test_stop_loss_fires_below_threshold(engine, technicals, scores) -> None:
    # -20% from cost, past the -15% stop.
    position = make_position("TCS", quantity=100, avg_cost=100, last_price=80)
    portfolio = make_portfolio([position])

    triggers = engine.evaluate(
        portfolio, scores, {"TCS": make_fundamental("TCS")}, technicals
    )

    trigger = find(triggers, TriggerType.STOP_LOSS)
    assert trigger is not None
    assert trigger.severity == Severity.MANDATORY
    assert trigger.suggested_action == Action.SELL
    assert trigger.suggested_quantity == 100, "a stop-loss exits the whole position"


def test_stop_loss_silent_just_above_threshold(engine, technicals, scores) -> None:
    position = make_position("TCS", avg_cost=100, last_price=86)  # -14%
    portfolio = make_portfolio([position])

    triggers = engine.evaluate(
        portfolio, scores, {"TCS": make_fundamental("TCS")}, technicals
    )

    assert TriggerType.STOP_LOSS not in types_of(triggers)


# ----------------------------------------------------------------------
# 2. Trailing stop (down from peak)
# ----------------------------------------------------------------------
def test_trailing_stop_protects_a_gain(engine, technicals, scores) -> None:
    # Ran to 150, now 115: down 23% from peak but still up 15% on cost.
    position = make_position("TCS", avg_cost=100, last_price=115, peak_price=150)
    portfolio = make_portfolio([position])

    triggers = engine.evaluate(
        portfolio, scores, {"TCS": make_fundamental("TCS")}, technicals
    )

    trigger = find(triggers, TriggerType.TRAILING_STOP)
    assert trigger is not None
    assert trigger.severity == Severity.MANDATORY


def test_trailing_stop_does_not_double_up_on_stop_loss(
    engine, technicals, scores
) -> None:
    """A name that fell straight from entry is a stop-loss, not a trailing stop."""
    position = make_position("TCS", avg_cost=100, last_price=78, peak_price=100)
    portfolio = make_portfolio([position])

    triggers = engine.evaluate(
        portfolio, scores, {"TCS": make_fundamental("TCS")}, technicals
    )

    assert TriggerType.STOP_LOSS in types_of(triggers)
    assert TriggerType.TRAILING_STOP not in types_of(triggers)


# ----------------------------------------------------------------------
# 3. Profit target (up from cost)
# ----------------------------------------------------------------------
def test_profit_target_trims_rather_than_exits(engine, technicals, scores) -> None:
    position = make_position("TCS", quantity=100, avg_cost=100, last_price=140)
    portfolio = make_portfolio([position])

    triggers = engine.evaluate(
        portfolio, scores, {"TCS": make_fundamental("TCS")}, technicals
    )

    trigger = find(triggers, TriggerType.PROFIT_TARGET)
    assert trigger is not None
    assert trigger.severity == Severity.REVIEW, "a winner is reviewed, not auto-sold"
    assert trigger.suggested_action == Action.TRIM
    assert trigger.suggested_quantity == 50


# ----------------------------------------------------------------------
# 4. Target price reached
# ----------------------------------------------------------------------
def test_target_price_trigger(engine, technicals, scores) -> None:
    position = make_position(
        "TCS", avg_cost=100, last_price=125, target_price=120
    )
    portfolio = make_portfolio([position])

    triggers = engine.evaluate(
        portfolio, scores, {"TCS": make_fundamental("TCS")}, technicals
    )

    assert find(triggers, TriggerType.TARGET_PRICE) is not None


# ----------------------------------------------------------------------
# 5. Valuation stretch
# ----------------------------------------------------------------------
def test_valuation_stretch_fires_on_rerating(engine, technicals, scores) -> None:
    # Bought at 20x, now 35x: past the 1.5x multiple.
    position = make_position("TCS", avg_cost=100, last_price=120, entry_pe=20.0)
    portfolio = make_portfolio([position])
    fundamentals = {"TCS": make_fundamental("TCS", pe=35.0)}

    triggers = engine.evaluate(portfolio, scores, fundamentals, technicals)

    trigger = find(triggers, TriggerType.VALUATION_STRETCH)
    assert trigger is not None
    assert trigger.suggested_action == Action.TRIM


def test_valuation_stretch_silent_without_entry_pe(engine, technicals, scores) -> None:
    """Without a recorded entry multiple there is no basis for the comparison."""
    position = make_position("TCS", avg_cost=100, last_price=120, entry_pe=None)
    portfolio = make_portfolio([position])
    fundamentals = {"TCS": make_fundamental("TCS", pe=90.0)}

    triggers = engine.evaluate(portfolio, scores, fundamentals, technicals)

    assert TriggerType.VALUATION_STRETCH not in types_of(triggers)


# ----------------------------------------------------------------------
# 6. Fundamental deterioration (thesis break)
# ----------------------------------------------------------------------
def test_fundamental_deterioration_on_gate_failure(engine, technicals) -> None:
    position = make_position("TCS", avg_cost=100, last_price=101)
    portfolio = make_portfolio([position])
    scores = {
        "TCS": make_scores(
            "TCS",
            composite=-0.5,
            passes_gates=False,
            failures=["ROE 4.0% < 10.0%", "debt/equity 3.10 > 2.0"],
        )
    }

    triggers = engine.evaluate(
        portfolio, scores, {"TCS": make_fundamental("TCS", roe=4.0)}, technicals
    )

    trigger = find(triggers, TriggerType.FUNDAMENTAL_DETERIORATION)
    assert trigger is not None
    assert "ROE" in trigger.detail


def test_data_gaps_are_not_treated_as_thesis_break(engine, technicals) -> None:
    """Missing data is a data problem, not a reason to sell a good business."""
    position = make_position("TCS", avg_cost=100, last_price=101)
    portfolio = make_portfolio([position])
    scores = {
        "TCS": make_scores(
            "TCS",
            passes_gates=False,
            failures=["insufficient fundamental data", "avg daily turnover Rs 1.0cr below Rs 5.0cr floor"],
        )
    }

    triggers = engine.evaluate(
        portfolio, scores, {"TCS": make_fundamental("TCS")}, technicals
    )

    assert TriggerType.FUNDAMENTAL_DETERIORATION not in types_of(triggers)


# ----------------------------------------------------------------------
# 7. Opportunity cost: deep review only
# ----------------------------------------------------------------------
def test_opportunity_cost_only_in_deep_review(engine, technicals) -> None:
    positions = [
        make_position("TCS", avg_cost=100, last_price=101),
        make_position("INFY", avg_cost=100, last_price=102),
    ]
    portfolio = make_portfolio(positions)
    scores = {
        "TCS": make_scores("TCS", composite=0.1),
        "INFY": make_scores("INFY", composite=1.0),
    }
    fundamentals = {s: make_fundamental(s) for s in ("TCS", "INFY")}
    candidate = make_scores("NEWCO", composite=1.8)

    daily = engine.evaluate(
        portfolio, scores, fundamentals, technicals, best_candidate=candidate
    )
    assert TriggerType.OPPORTUNITY_COST not in types_of(daily)

    quarterly = engine.evaluate(
        portfolio,
        scores,
        fundamentals,
        technicals,
        best_candidate=candidate,
        is_deep_review=True,
    )
    trigger = find(quarterly, TriggerType.OPPORTUNITY_COST)
    assert trigger is not None
    assert trigger.symbol == "TCS", "the weakest holding is the rotation candidate"
    assert trigger.severity == Severity.OPPORTUNITY


def test_new_positions_get_a_grace_period(engine, technicals) -> None:
    """A recent purchase is not churned out for a marginally better score."""
    positions = [
        make_position("TCS", avg_cost=100, last_price=101, entry_days_ago=10),
        make_position("INFY", avg_cost=100, last_price=102, entry_days_ago=500),
    ]
    portfolio = make_portfolio(positions)
    scores = {
        "TCS": make_scores("TCS", composite=0.1),
        "INFY": make_scores("INFY", composite=1.0),
    }
    fundamentals = {s: make_fundamental(s) for s in ("TCS", "INFY")}

    triggers = engine.evaluate(
        portfolio,
        scores,
        fundamentals,
        technicals,
        best_candidate=make_scores("NEWCO", composite=1.8),
        is_deep_review=True,
    )

    assert TriggerType.OPPORTUNITY_COST not in types_of(triggers)


# ----------------------------------------------------------------------
# Portfolio-level triggers
# ----------------------------------------------------------------------
def test_position_cap_breach_is_mandatory(engine, technicals, scores) -> None:
    # One position at ~91% of the fund.
    positions = [
        make_position("TCS", quantity=1000, avg_cost=100, last_price=100),
        make_position("INFY", quantity=100, avg_cost=100, last_price=100),
    ]
    portfolio = make_portfolio(positions, cash=0)
    fundamentals = {s: make_fundamental(s) for s in ("TCS", "INFY")}

    triggers = engine.evaluate(portfolio, scores, fundamentals, technicals)

    trigger = find(triggers, TriggerType.POSITION_CAP)
    assert trigger is not None
    assert trigger.severity == Severity.MANDATORY
    assert trigger.suggested_action == Action.TRIM


def test_sector_cap_trims_the_weakest_name_in_the_sector(
    engine, technicals, scores
) -> None:
    positions = [
        make_position("TCS", avg_cost=100, last_price=110, sector="Information Technology"),
        make_position("INFY", avg_cost=100, last_price=95, sector="Information Technology"),
        make_position("WIPRO", avg_cost=100, last_price=105, sector="Information Technology"),
    ]
    portfolio = make_portfolio(positions, cash=0)
    fundamentals = {s: make_fundamental(s) for s in ("TCS", "INFY", "WIPRO")}

    triggers = engine.evaluate(portfolio, scores, fundamentals, technicals)

    trigger = find(triggers, TriggerType.SECTOR_CAP)
    assert trigger is not None
    assert trigger.symbol == "INFY", "the worst performer in the sector is trimmed"
    assert trigger.severity == Severity.MANDATORY


def test_flagged_news_raises_a_review_trigger(engine, technicals, scores) -> None:
    from datetime import date as date_type

    position = make_position("TCS", avg_cost=100, last_price=101)
    portfolio = make_portfolio([position])
    digest = NewsDigest(
        as_of=date_type.today(),
        flagged_symbols=["TCS"],
        holdings_notes={"TCS": "CFO resigned unexpectedly; auditor raised concerns."},
    )

    triggers = engine.evaluate(
        portfolio, scores, {"TCS": make_fundamental("TCS")}, technicals, digest=digest
    )

    trigger = find(triggers, TriggerType.NEWS_EVENT)
    assert trigger is not None
    assert trigger.severity == Severity.REVIEW
    assert "CFO resigned" in trigger.detail


def test_triggers_are_ordered_by_severity(engine, technicals) -> None:
    positions = [
        make_position("TCS", quantity=100, avg_cost=100, last_price=80),  # stop loss
        make_position("INFY", quantity=100, avg_cost=100, last_price=140),  # profit
    ]
    portfolio = make_portfolio(positions, cash=500_000)
    scores = {s: make_scores(s) for s in ("TCS", "INFY")}
    fundamentals = {s: make_fundamental(s) for s in ("TCS", "INFY")}

    triggers = engine.evaluate(portfolio, scores, fundamentals, technicals)

    severities = [t.severity for t in triggers]
    assert severities[0] == Severity.MANDATORY
    assert severities == sorted(
        severities,
        key=lambda s: {Severity.MANDATORY: 0, Severity.REVIEW: 1, Severity.OPPORTUNITY: 2}[s],
    )


def test_empty_portfolio_yields_no_triggers(engine, technicals, scores) -> None:
    assert engine.evaluate(make_portfolio([]), scores, {}, technicals) == []
