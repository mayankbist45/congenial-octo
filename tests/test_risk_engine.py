"""Risk engine validation tests.

The engine is the last gate before a trade reaches the approval queue, so these
tests focus on what it must refuse: oversized positions, sector concentration,
breaching the cash buffer, selling what is not held, and letting the decision
layer trade beyond the turnover budget.
"""

from __future__ import annotations

from typing import List

import pytest

from src.models import Action, TradeProposal, TriggerType
from src.risk.engine import RiskEngine
from src.risk.turnover import TurnoverStatus
from tests.conftest import (
    make_diversified_portfolio,
    make_fundamental,
    make_portfolio,
    make_position,
    make_scores,
    make_technical,
)


@pytest.fixture
def engine(config) -> RiskEngine:
    return RiskEngine(config)


@pytest.fixture
def open_budget() -> TurnoverStatus:
    """A budget with plenty of headroom."""
    return TurnoverStatus(
        budget_pct=75.0,
        used_pct=5.0,
        purchases=100_000.0,
        sales=100_000.0,
        average_net_assets=1_500_000.0,
        window_days=365,
    )


@pytest.fixture
def spent_budget() -> TurnoverStatus:
    return TurnoverStatus(
        budget_pct=75.0,
        used_pct=80.0,
        purchases=1_200_000.0,
        sales=1_200_000.0,
        average_net_assets=1_500_000.0,
        window_days=365,
    )


def proposal(
    symbol: str,
    action: Action = Action.BUY,
    quantity: int = 100,
    price: float = 100.0,
    triggers: List[str] = None,
) -> TradeProposal:
    return TradeProposal(
        symbol=symbol,
        action=action,
        quantity=quantity,
        rationale="test",
        reference_price=price,
        estimated_value=quantity * price,
        trigger_types=triggers or [],
    )


def reasons(result) -> str:
    return " | ".join(reason for _p, reason in result.rejected)


# ----------------------------------------------------------------------
# Well-formedness
# ----------------------------------------------------------------------
def test_rejects_symbol_outside_universe(engine, open_budget) -> None:
    portfolio = make_diversified_portfolio(cash=500_000)
    technicals = {"NOTREAL": make_technical("NOTREAL")}

    result = engine.validate(
        [proposal("NOTREAL")], portfolio, technicals, {}, open_budget
    )

    assert not result.approved
    assert "not in the Nifty 500" in reasons(result)


def test_rejects_zero_quantity(engine, open_budget) -> None:
    portfolio = make_diversified_portfolio(cash=500_000)
    technicals = {"WIPRO": make_technical("WIPRO")}

    result = engine.validate(
        [proposal("WIPRO", quantity=0)], portfolio, technicals, {}, open_budget
    )

    assert "quantity must be positive" in reasons(result)


def test_rejects_buy_without_a_price(engine, open_budget) -> None:
    portfolio = make_diversified_portfolio(cash=500_000)
    technicals = {"WIPRO": make_technical("WIPRO", last_price=0.0)}
    candidate = proposal("WIPRO")
    candidate.reference_price = None

    result = engine.validate([candidate], portfolio, technicals, {}, open_budget)

    assert "no reliable price" in reasons(result)


# ----------------------------------------------------------------------
# Sell-side
# ----------------------------------------------------------------------
def test_rejects_selling_what_is_not_held(engine, open_budget) -> None:
    portfolio = make_diversified_portfolio()
    technicals = {"WIPRO": make_technical("WIPRO")}

    result = engine.validate(
        [proposal("WIPRO", action=Action.SELL)], portfolio, technicals, {}, open_budget
    )

    assert "not held" in reasons(result)


def test_rejects_overselling_a_position(engine, open_budget) -> None:
    portfolio = make_diversified_portfolio()
    technicals = {"TCS": make_technical("TCS")}

    result = engine.validate(
        [proposal("TCS", action=Action.SELL, quantity=5000)],
        portfolio,
        technicals,
        {},
        open_budget,
    )

    assert "only 1000 held" in reasons(result)


def test_accepts_a_valid_full_exit(engine, open_budget) -> None:
    portfolio = make_diversified_portfolio()
    technicals = {"TCS": make_technical("TCS")}

    result = engine.validate(
        [proposal("TCS", action=Action.SELL, quantity=1000)],
        portfolio,
        technicals,
        {},
        open_budget,
    )

    assert len(result.approved) == 1
    assert not result.rejected


# ----------------------------------------------------------------------
# Caps and buffers
# ----------------------------------------------------------------------
def test_rejects_buy_breaching_position_cap(engine, open_budget) -> None:
    portfolio = make_diversified_portfolio(cash=800_000)
    technicals = {"WIPRO": make_technical("WIPRO")}
    scores = {"WIPRO": make_scores("WIPRO")}

    # Total fund is ~2.2m; 8% cap is ~176k. A 400k order must be refused.
    result = engine.validate(
        [proposal("WIPRO", quantity=4000)], portfolio, technicals, scores, open_budget
    )

    assert "position cap" in reasons(result)


def test_rejects_buy_breaching_sector_cap(engine, open_budget) -> None:
    """IT is already ~2 names; adding a large third must breach the 25% cap."""
    positions = [
        make_position("TCS", quantity=1000, avg_cost=100, last_price=100, sector="Information Technology"),
        make_position("INFY", quantity=1000, avg_cost=100, last_price=100, sector="Information Technology"),
        make_position("RELIANCE", quantity=1000, avg_cost=100, last_price=100, sector="Oil Gas & Consumable Fuels"),
        make_position("ITC", quantity=1000, avg_cost=100, last_price=100, sector="Fast Moving Consumer Goods"),
    ]
    portfolio = make_portfolio(positions, cash=100_000)
    technicals = {"WIPRO": make_technical("WIPRO")}
    scores = {"WIPRO": make_scores("WIPRO")}

    result = engine.validate(
        [proposal("WIPRO", quantity=300)], portfolio, technicals, scores, open_budget
    )

    assert "sector cap" in reasons(result)


def test_rejects_buy_that_would_breach_cash_buffer(engine, open_budget) -> None:
    portfolio = make_diversified_portfolio(cash=50_000)
    technicals = {"WIPRO": make_technical("WIPRO")}
    scores = {"WIPRO": make_scores("WIPRO")}

    result = engine.validate(
        [proposal("WIPRO", quantity=450)], portfolio, technicals, scores, open_budget
    )

    assert "buffer" in reasons(result)


def test_rejects_illiquid_order(engine, open_budget) -> None:
    """An order too large versus daily turnover cannot be exited later."""
    portfolio = make_diversified_portfolio(cash=500_000)
    # Only Rs 10 lakh a day of turnover, so a Rs 1 lakh order is 10% of volume.
    technicals = {"WIPRO": make_technical("WIPRO", turnover=1e6)}
    scores = {"WIPRO": make_scores("WIPRO")}

    result = engine.validate(
        [proposal("WIPRO", quantity=1000)], portfolio, technicals, scores, open_budget
    )

    assert "average daily" in reasons(result)


# ----------------------------------------------------------------------
# Gates and duplicates
# ----------------------------------------------------------------------
def test_rejects_buy_failing_fundamental_gates(engine, open_budget) -> None:
    portfolio = make_diversified_portfolio(cash=500_000)
    technicals = {"WIPRO": make_technical("WIPRO")}
    scores = {
        "WIPRO": make_scores(
            "WIPRO", passes_gates=False, failures=["ROE 3.0% < 10.0%"]
        )
    }

    result = engine.validate(
        [proposal("WIPRO", quantity=100)], portfolio, technicals, scores, open_budget
    )

    assert "fails fundamental gates" in reasons(result)


def test_rejects_buy_of_an_existing_holding(engine, open_budget) -> None:
    portfolio = make_diversified_portfolio(cash=500_000)
    technicals = {"TCS": make_technical("TCS")}
    scores = {"TCS": make_scores("TCS")}

    result = engine.validate(
        [proposal("TCS", quantity=10)], portfolio, technicals, scores, open_budget
    )

    assert "already held" in reasons(result)


def test_rejects_add_to_a_position_not_held(engine, open_budget) -> None:
    portfolio = make_diversified_portfolio(cash=500_000)
    technicals = {"WIPRO": make_technical("WIPRO")}
    scores = {"WIPRO": make_scores("WIPRO")}

    result = engine.validate(
        [proposal("WIPRO", action=Action.ADD, quantity=10)],
        portfolio,
        technicals,
        scores,
        open_budget,
    )

    assert "cannot add" in reasons(result)


# ----------------------------------------------------------------------
# Turnover budget
# ----------------------------------------------------------------------
def test_discretionary_trade_blocked_when_budget_spent(engine, spent_budget) -> None:
    portfolio = make_diversified_portfolio(cash=500_000)
    technicals = {"WIPRO": make_technical("WIPRO")}
    scores = {"WIPRO": make_scores("WIPRO")}

    result = engine.validate(
        [proposal("WIPRO", quantity=100)], portfolio, technicals, scores, spent_budget
    )

    assert "turnover budget exhausted" in reasons(result)


def test_mandatory_risk_trade_survives_a_spent_budget(engine, spent_budget) -> None:
    """Risk control is never discretionary, budget or no budget."""
    portfolio = make_diversified_portfolio()
    technicals = {"TCS": make_technical("TCS")}

    result = engine.validate(
        [
            proposal(
                "TCS",
                action=Action.SELL,
                quantity=1000,
                triggers=[TriggerType.STOP_LOSS.value],
            )
        ],
        portfolio,
        technicals,
        {},
        spent_budget,
    )

    assert len(result.approved) == 1, reasons(result)


# ----------------------------------------------------------------------
# Batch behaviour
# ----------------------------------------------------------------------
def test_batch_of_buys_cannot_collectively_breach_the_cash_buffer(
    engine, open_budget
) -> None:
    """Each buy fits alone; together they must not drain the buffer."""
    portfolio = make_diversified_portfolio(cash=200_000)
    symbols = ["WIPRO", "HCLTECH", "TECHM", "LTIM"]
    technicals = {s: make_technical(s) for s in symbols}
    scores = {s: make_scores(s) for s in symbols}

    proposals = [proposal(s, quantity=700) for s in symbols]
    result = engine.validate(proposals, portfolio, technicals, scores, open_budget)

    approved_value = sum(p.estimated_value or 0 for p in result.approved)
    buffer = 0.05 * portfolio.total_value
    assert approved_value <= portfolio.cash - buffer + 1.0
    assert result.rejected, "at least one buy should have been refused"


def test_sells_are_validated_before_buys(engine, open_budget) -> None:
    """Selling first frees the cash a later buy in the same batch depends on.

    The fund is Rs 1.46m with only Rs 60k cash, so the Rs 80k purchase is only
    affordable once the Rs 100k sale is accounted for.
    """
    portfolio = make_diversified_portfolio(cash=60_000)
    technicals = {"TCS": make_technical("TCS"), "WIPRO": make_technical("WIPRO")}
    scores = {"WIPRO": make_scores("WIPRO")}

    # Buy listed first, so ordering by side is what makes this pass.
    proposals = [
        proposal("WIPRO", action=Action.BUY, quantity=800),
        proposal("TCS", action=Action.SELL, quantity=1000),
    ]
    result = engine.validate(proposals, portfolio, technicals, scores, open_budget)

    approved = [(p.symbol, p.action) for p in result.approved]
    assert ("TCS", Action.SELL) in approved
    assert ("WIPRO", Action.BUY) in approved, reasons(result)


def test_buy_alone_is_refused_without_the_funding_sale(engine, open_budget) -> None:
    """Confirms the previous test passes because of the sale, not by luck."""
    portfolio = make_diversified_portfolio(cash=60_000)
    technicals = {"WIPRO": make_technical("WIPRO")}
    scores = {"WIPRO": make_scores("WIPRO")}

    result = engine.validate(
        [proposal("WIPRO", action=Action.BUY, quantity=800)],
        portfolio,
        technicals,
        scores,
        open_budget,
    )

    assert not result.approved
    assert "buffer" in reasons(result)


# ----------------------------------------------------------------------
# Warnings
# ----------------------------------------------------------------------
def test_overbought_entry_produces_a_warning_not_a_rejection(
    engine, open_budget
) -> None:
    portfolio = make_diversified_portfolio(cash=500_000)
    technicals = {"WIPRO": make_technical("WIPRO", rsi=82.0)}
    scores = {"WIPRO": make_scores("WIPRO")}

    candidate = proposal("WIPRO", quantity=100)
    result = engine.validate([candidate], portfolio, technicals, scores, open_budget)

    assert len(result.approved) == 1
    assert any("overbought" in w for w in candidate.warnings)


def test_downtrend_produces_a_warning(engine, open_budget) -> None:
    portfolio = make_diversified_portfolio(cash=500_000)
    technicals = {"WIPRO": make_technical("WIPRO", above_sma_200=False)}
    scores = {"WIPRO": make_scores("WIPRO")}

    candidate = proposal("WIPRO", quantity=100)
    engine.validate([candidate], portfolio, technicals, scores, open_budget)

    assert any("200-day" in w for w in candidate.warnings)


# ----------------------------------------------------------------------
# Mandatory trade generation
# ----------------------------------------------------------------------
def test_mandatory_triggers_become_proposals(engine) -> None:
    from src.risk.triggers import TriggerEngine

    portfolio = make_diversified_portfolio(cash=100_000)
    portfolio.positions[0].last_price = 70.0  # -30%: stop loss on TCS
    technicals = {p.symbol: make_technical(p.symbol) for p in portfolio.positions}
    scores = {p.symbol: make_scores(p.symbol) for p in portfolio.positions}
    fundamentals = {p.symbol: make_fundamental(p.symbol) for p in portfolio.positions}

    triggers = TriggerEngine(engine._config).evaluate(
        portfolio, scores, fundamentals, technicals
    )
    proposals = engine.mandatory_proposals(triggers, portfolio, technicals)

    assert any(p.symbol == "TCS" and p.action == Action.SELL for p in proposals)
    stop = next(p for p in proposals if p.symbol == "TCS")
    assert stop.source == "risk_engine"
    assert stop.confidence == 1.0
    assert TriggerType.STOP_LOSS.value in stop.trigger_types


# ----------------------------------------------------------------------
# Idle cash
# ----------------------------------------------------------------------
def test_excess_cash_raises_a_deployment_trigger(engine) -> None:
    portfolio = make_portfolio([], cash=1_000_000)
    candidates = [make_scores("TCS", composite=1.5)]

    triggers = engine.cash_deployment_triggers(portfolio, candidates)

    assert len(triggers) == 1
    assert triggers[0].trigger_type == TriggerType.INITIAL_BUILD
    assert triggers[0].suggested_action == Action.BUY


def test_normal_cash_level_raises_no_deployment_trigger(engine) -> None:
    portfolio = make_diversified_portfolio(cash=70_000)  # ~4.8% of the fund
    candidates = [make_scores("WIPRO", composite=1.5)]

    assert engine.cash_deployment_triggers(portfolio, candidates) == []
