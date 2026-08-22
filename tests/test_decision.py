"""Decision layer tests.

The decision layer is the one place where untrusted output (an LLM's JSON) turns
into something that could place a real order. These tests therefore focus on
parsing hostile or sloppy responses, on the fund's sizing rules overriding what
the model asked for, and on the run surviving an LLM outage.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from src.decision.engine import DecisionEngine
from src.llm import LLMUnavailable
from src.models import Action, Severity, Trigger, TriggerType
from tests.conftest import (
    make_diversified_portfolio,
    make_fundamental,
    make_position,
    make_portfolio,
    make_scores,
    make_technical,
)


class FakeLLM:
    """Stands in for :class:`src.llm.LLMClient`."""

    def __init__(self, response: Any = None, fail: bool = False, available: bool = True):
        self._response = response
        self._fail = fail
        self.available = available
        self.prompts: List[str] = []

    def complete_json(self, system_prompt: str, user_prompt: str, **_: Any) -> Any:
        self.prompts.append(user_prompt)
        if self._fail:
            raise LLMUnavailable("simulated outage")
        return self._response


def response(proposals: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    payload = {
        "market_view": "Markets are unremarkable.",
        "portfolio_assessment": "The fund is positioned as intended.",
        "action_taken": "trades_proposed" if proposals else "no_trades",
        "reasoning": "As described above.",
        "proposals": proposals,
    }
    payload.update(extra)
    return payload


def build(config, llm: FakeLLM) -> DecisionEngine:
    return DecisionEngine(config=config, llm=llm)


def context(portfolio, symbols: List[str]):
    """Technicals, scores and fundamentals for a set of symbols."""
    return (
        {s: make_technical(s) for s in symbols},
        {s: make_scores(s) for s in symbols},
        {s: make_fundamental(s) for s in symbols},
    )


def run(engine, portfolio, symbols: List[str], triggers: Optional[List[Trigger]] = None):
    technicals, scores, fundamentals = context(portfolio, symbols)
    candidates = [scores[s] for s in symbols if portfolio.get(s) is None]
    return engine.decide(
        portfolio=portfolio,
        scores=scores,
        fundamentals=fundamentals,
        technicals=technicals,
        triggers=triggers or [],
        candidates=candidates,
        digest=None,
        risk_profile=None,
    )


# ----------------------------------------------------------------------
# The no-trade path
# ----------------------------------------------------------------------
def test_no_trade_day_produces_a_memo_and_no_proposals(config) -> None:
    llm = FakeLLM(response([]))
    portfolio = make_diversified_portfolio(cash=80_000)

    decision = run(build(config, llm), portfolio, ["TCS", "WIPRO"])

    assert decision.proposals == []
    assert decision.llm_used
    assert "Market view" in decision.memo
    assert "Reasoning" in decision.memo


def test_briefing_is_given_to_the_model(config) -> None:
    llm = FakeLLM(response([]))
    portfolio = make_diversified_portfolio(cash=80_000)

    run(build(config, llm), portfolio, ["TCS", "WIPRO"])

    prompt = llm.prompts[0]
    assert "DAILY BRIEFING" in prompt
    assert "MANDATE AND HARD LIMITS" in prompt
    assert "TCS" in prompt
    assert "Turnover" in prompt


# ----------------------------------------------------------------------
# LLM failure
# ----------------------------------------------------------------------
def test_outage_falls_back_to_rules_only(config) -> None:
    llm = FakeLLM(fail=True)
    portfolio = make_diversified_portfolio(cash=80_000)

    decision = run(build(config, llm), portfolio, ["TCS", "WIPRO"])

    assert not decision.llm_used
    assert decision.proposals == []
    assert "rules only" in decision.memo


def test_mandatory_trades_still_execute_during_an_outage(config) -> None:
    llm = FakeLLM(fail=True)
    portfolio = make_diversified_portfolio(cash=80_000)
    portfolio.positions[0].last_price = 70.0
    trigger = Trigger(
        symbol="TCS",
        trigger_type=TriggerType.STOP_LOSS,
        severity=Severity.MANDATORY,
        detail="down 30% from cost",
        suggested_action=Action.SELL,
    )

    decision = run(build(config, llm), portfolio, ["TCS"], triggers=[trigger])

    assert [p.symbol for p in decision.proposals] == ["TCS"]
    assert decision.proposals[0].source == "risk_engine"
    assert "Mandatory risk trades were still enforced" in decision.memo


def test_missing_api_key_runs_rules_only(config) -> None:
    llm = FakeLLM(response([{"symbol": "WIPRO", "action": "BUY"}]), available=False)
    portfolio = make_diversified_portfolio(cash=200_000)

    decision = run(build(config, llm), portfolio, ["WIPRO"])

    assert not decision.llm_used
    assert decision.proposals == []
    assert "no LLM configured" in decision.memo


# ----------------------------------------------------------------------
# Rules-only cash deployment
# ----------------------------------------------------------------------
def deployment_trigger(symbol: str = "WIPRO") -> Trigger:
    return Trigger(
        symbol=symbol,
        trigger_type=TriggerType.INITIAL_BUILD,
        severity=Severity.OPPORTUNITY,
        detail="Cash is 100% of the fund against a 5% target buffer.",
        suggested_action=Action.BUY,
    )


def test_rules_only_path_still_deploys_idle_cash(config) -> None:
    """Without this the fund would hold cash forever when no LLM is configured."""
    llm = FakeLLM(available=False)
    portfolio = make_portfolio([], cash=2_000_000)

    decision = run(
        build(config, llm),
        portfolio,
        ["WIPRO", "HCLTECH", "TECHM", "LTIM"],
        triggers=[deployment_trigger()],
    )

    assert not decision.llm_used
    assert decision.proposals
    assert all(p.source == "quant_screen" for p in decision.proposals)
    assert all(p.action == Action.BUY for p in decision.proposals)


def test_available_llm_cannot_leave_underinvested_fund_without_a_buy(config) -> None:
    llm = FakeLLM(response([]))
    portfolio = make_portfolio([], cash=2_000_000)

    decision = run(
        build(config, llm),
        portfolio,
        ["WIPRO", "HCLTECH", "TECHM", "LTIM"],
        triggers=[deployment_trigger()],
    )

    assert decision.llm_used
    assert decision.proposals
    assert all(proposal.source == "quant_screen" for proposal in decision.proposals)
    assert "Deterministic cash deployment" in decision.memo


def test_llm_buy_prevents_duplicate_cash_deployment_fallback(config) -> None:
    llm = FakeLLM(
        response(
            [
                {
                    "symbol": "WIPRO",
                    "action": "BUY",
                    "quantity": 10,
                    "rationale": "Quality and valuation pass the portfolio's gates.",
                }
            ]
        )
    )
    portfolio = make_portfolio([], cash=2_000_000)

    decision = run(
        build(config, llm),
        portfolio,
        ["WIPRO", "HCLTECH"],
        triggers=[deployment_trigger()],
    )

    assert [proposal.symbol for proposal in decision.proposals] == ["WIPRO"]
    assert "Deterministic cash deployment" not in decision.memo


def test_rules_only_buys_are_capped_per_run(config) -> None:
    """The portfolio is built gradually, under repeated human review."""
    llm = FakeLLM(available=False)
    portfolio = make_portfolio([], cash=5_000_000)
    symbols = ["WIPRO", "HCLTECH", "TECHM", "LTIM", "MPHASIS", "COFORGE"]

    decision = run(
        build(config, llm), portfolio, symbols, triggers=[deployment_trigger()]
    )

    assert len(decision.proposals) <= 3


def test_rules_only_buys_are_sized_by_the_fund_not_the_screen(config) -> None:
    llm = FakeLLM(available=False)
    portfolio = make_portfolio([], cash=2_000_000)

    decision = run(
        build(config, llm), portfolio, ["WIPRO"], triggers=[deployment_trigger()]
    )

    weight = decision.proposals[0].estimated_value / portfolio.total_value * 100
    assert weight <= float(config.get("risk.max_position_pct"))


def test_rules_only_buys_admit_they_have_no_thesis(config) -> None:
    """The record must not imply judgement that was never exercised."""
    llm = FakeLLM(available=False)
    portfolio = make_portfolio([], cash=2_000_000)

    decision = run(
        build(config, llm), portfolio, ["WIPRO"], triggers=[deployment_trigger()]
    )

    proposal = decision.proposals[0]
    assert "quant_screen" in proposal.rationale
    assert "no qualitative thesis" in proposal.thesis
    assert proposal.confidence is None
    assert "deserve more scrutiny" in decision.memo


def test_no_mechanical_buying_when_the_fund_is_fully_invested(config) -> None:
    """No deployment trigger means no mechanical purchases, cash or not."""
    llm = FakeLLM(available=False)
    portfolio = make_diversified_portfolio(cash=300_000)

    decision = run(build(config, llm), portfolio, ["WIPRO", "HCLTECH"])

    assert decision.proposals == []


def test_no_mechanical_buying_without_screened_candidates(config) -> None:
    llm = FakeLLM(available=False)
    portfolio = make_portfolio([], cash=2_000_000)
    technicals = {"WIPRO": make_technical("WIPRO")}

    decision = build(config, llm).decide(
        portfolio=portfolio,
        scores={"WIPRO": make_scores("WIPRO")},
        fundamentals={"WIPRO": make_fundamental("WIPRO")},
        technicals=technicals,
        triggers=[deployment_trigger()],
        candidates=[],
        digest=None,
        risk_profile=None,
    )

    assert decision.proposals == []


def test_mechanical_buys_are_still_risk_validated(config) -> None:
    """A screened name that fails the gates cannot slip through the fallback."""
    llm = FakeLLM(available=False)
    portfolio = make_portfolio([], cash=2_000_000)
    scores = {
        "WIPRO": make_scores("WIPRO", passes_gates=False, failures=["ROE 3% < 10%"])
    }

    decision = build(config, llm).decide(
        portfolio=portfolio,
        scores=scores,
        fundamentals={"WIPRO": make_fundamental("WIPRO", roe=3.0)},
        technicals={"WIPRO": make_technical("WIPRO")},
        triggers=[deployment_trigger()],
        candidates=[scores["WIPRO"]],
        digest=None,
        risk_profile=None,
    )

    assert decision.proposals == []
    assert "fails fundamental gates" in decision.memo


def test_non_object_response_falls_back(config) -> None:
    llm = FakeLLM(["not", "an", "object"])
    portfolio = make_diversified_portfolio(cash=200_000)

    decision = run(build(config, llm), portfolio, ["WIPRO"])

    assert decision.proposals == []
    assert "rules only" in decision.memo


# ----------------------------------------------------------------------
# Parsing malformed proposals
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "item",
    [
        {},
        {"symbol": "", "action": "BUY", "rationale": "x"},
        {"symbol": "WIPRO", "action": "LEVERAGE_UP", "rationale": "x"},
        {"symbol": "WIPRO", "action": "HOLD", "rationale": "x"},
        {"symbol": "WIPRO", "action": "BUY"},  # no rationale
        {"symbol": "WIPRO", "action": "BUY", "rationale": "   "},
        "a bare string",
        None,
    ],
)
def test_malformed_proposals_are_discarded(config, item) -> None:
    llm = FakeLLM(response([item]))
    portfolio = make_diversified_portfolio(cash=300_000)

    decision = run(build(config, llm), portfolio, ["WIPRO"])

    assert decision.proposals == []


def test_unknown_symbol_is_rejected_by_the_risk_engine(config) -> None:
    llm = FakeLLM(
        response(
            [
                {
                    "symbol": "NOTALISTEDCO",
                    "action": "BUY",
                    "rationale": "invented company",
                }
            ]
        )
    )
    portfolio = make_diversified_portfolio(cash=300_000)
    technicals, scores, fundamentals = context(portfolio, ["WIPRO"])
    technicals["NOTALISTEDCO"] = make_technical("NOTALISTEDCO")

    decision = build(config, llm).decide(
        portfolio=portfolio,
        scores=scores,
        fundamentals=fundamentals,
        technicals=technicals,
        triggers=[],
        candidates=[scores["WIPRO"]],
        digest=None,
        risk_profile=None,
    )

    assert decision.proposals == []
    assert "REJECTED BY THE RISK ENGINE" in decision.memo
    assert "not in the Nifty 500" in decision.memo


def test_duplicate_symbols_keep_only_the_first(config) -> None:
    llm = FakeLLM(
        response(
            [
                {"symbol": "WIPRO", "action": "BUY", "rationale": "first"},
                {"symbol": "WIPRO", "action": "BUY", "rationale": "second"},
            ]
        )
    )
    portfolio = make_diversified_portfolio(cash=300_000)

    decision = run(build(config, llm), portfolio, ["WIPRO"])

    assert len(decision.proposals) == 1
    assert "first" in decision.proposals[0].rationale


def test_daily_proposal_count_is_capped(config) -> None:
    symbols = ["WIPRO", "HCLTECH", "TECHM", "LTIM", "MPHASIS", "COFORGE", "PERSISTENT"]
    llm = FakeLLM(
        response(
            [
                {"symbol": s, "action": "BUY", "rationale": "candidate"}
                for s in symbols
            ]
        )
    )
    portfolio = make_portfolio([], cash=3_000_000)

    decision = run(build(config, llm), portfolio, symbols)

    # Five is the daily limit; the risk engine may cut more.
    assert len(decision.proposals) <= 5


# ----------------------------------------------------------------------
# Sizing authority
# ----------------------------------------------------------------------
def test_omitted_quantity_is_sized_by_the_fund(config) -> None:
    llm = FakeLLM(
        response([{"symbol": "WIPRO", "action": "BUY", "rationale": "good business"}])
    )
    portfolio = make_diversified_portfolio(cash=300_000)

    decision = run(build(config, llm), portfolio, ["WIPRO"])

    assert len(decision.proposals) == 1
    proposal = decision.proposals[0]
    assert proposal.quantity > 0
    # Target weight is (100 - 5) / 25 = 3.8% of a ~Rs 1.7m fund at Rs 100/share.
    weight = proposal.estimated_value / portfolio.total_value * 100
    assert 3.0 < weight < 4.5


def test_oversized_request_is_cut_to_the_fund_limit(config) -> None:
    """The model may choose direction, never position size."""
    llm = FakeLLM(
        response(
            [
                {
                    "symbol": "WIPRO",
                    "action": "BUY",
                    "quantity": 100_000,
                    "rationale": "high conviction",
                }
            ]
        )
    )
    portfolio = make_diversified_portfolio(cash=300_000)

    decision = run(build(config, llm), portfolio, ["WIPRO"])

    assert len(decision.proposals) == 1
    weight = decision.proposals[0].estimated_value / portfolio.total_value * 100
    assert weight <= float(config.get("risk.max_position_pct"))


def test_modest_request_is_honoured(config) -> None:
    llm = FakeLLM(
        response(
            [
                {
                    "symbol": "WIPRO",
                    "action": "BUY",
                    "quantity": 200,
                    "rationale": "starter position",
                }
            ]
        )
    )
    portfolio = make_diversified_portfolio(cash=300_000)

    decision = run(build(config, llm), portfolio, ["WIPRO"])

    assert decision.proposals[0].quantity == 200


def test_sell_without_a_quantity_exits_the_whole_position(config) -> None:
    llm = FakeLLM(
        response([{"symbol": "TCS", "action": "SELL", "rationale": "thesis broken"}])
    )
    portfolio = make_diversified_portfolio(cash=80_000)

    decision = run(build(config, llm), portfolio, ["TCS"])

    assert decision.proposals[0].quantity == 1000


def test_trim_without_a_quantity_halves_the_position(config) -> None:
    llm = FakeLLM(
        response([{"symbol": "TCS", "action": "TRIM", "rationale": "reduce risk"}])
    )
    portfolio = make_diversified_portfolio(cash=80_000)

    decision = run(build(config, llm), portfolio, ["TCS"])

    assert decision.proposals[0].quantity == 500


def test_cannot_sell_more_than_is_held(config) -> None:
    llm = FakeLLM(
        response(
            [
                {
                    "symbol": "TCS",
                    "action": "SELL",
                    "quantity": 99_999,
                    "rationale": "exit",
                }
            ]
        )
    )
    portfolio = make_diversified_portfolio(cash=80_000)

    decision = run(build(config, llm), portfolio, ["TCS"])

    assert decision.proposals[0].quantity == 1000


def test_selling_an_unheld_name_is_discarded(config) -> None:
    llm = FakeLLM(
        response([{"symbol": "WIPRO", "action": "SELL", "rationale": "short it"}])
    )
    portfolio = make_diversified_portfolio(cash=80_000)

    decision = run(build(config, llm), portfolio, ["WIPRO"])

    assert decision.proposals == []


# ----------------------------------------------------------------------
# Field handling
# ----------------------------------------------------------------------
def test_trigger_and_metadata_are_carried_through(config) -> None:
    llm = FakeLLM(
        response(
            [
                {
                    "symbol": "WIPRO",
                    "action": "BUY",
                    "trigger": "initial_build",
                    "rationale": "attractive on quality and value",
                    "thesis": "Steady cash generation at a reasonable multiple.",
                    "target_price": 145.0,
                    "confidence": 0.72,
                }
            ]
        )
    )
    portfolio = make_diversified_portfolio(cash=300_000)

    proposal = run(build(config, llm), portfolio, ["WIPRO"]).proposals[0]

    assert proposal.trigger_types == ["initial_build"]
    assert proposal.rationale.startswith("[initial_build]")
    assert proposal.thesis.startswith("Steady cash")
    assert proposal.target_price == 145.0
    assert proposal.confidence == 0.72
    assert proposal.source == "llm"
    assert proposal.company_name


@pytest.mark.parametrize(
    "raw,expected",
    [(1.4, 1.0), (-0.2, 0.0), ("0.6", 0.6), ("high", None), (None, None)],
)
def test_confidence_is_clamped(config, raw, expected) -> None:
    llm = FakeLLM(
        response(
            [
                {
                    "symbol": "WIPRO",
                    "action": "BUY",
                    "rationale": "x",
                    "confidence": raw,
                }
            ]
        )
    )
    portfolio = make_diversified_portfolio(cash=300_000)

    proposal = run(build(config, llm), portfolio, ["WIPRO"]).proposals[0]

    assert proposal.confidence == expected


def test_lowercase_action_and_symbol_are_normalised(config) -> None:
    llm = FakeLLM(
        response([{"symbol": "wipro", "action": "buy", "rationale": "cheap"}])
    )
    portfolio = make_diversified_portfolio(cash=300_000)

    proposal = run(build(config, llm), portfolio, ["WIPRO"]).proposals[0]

    assert proposal.symbol == "WIPRO"
    assert proposal.action == Action.BUY


# ----------------------------------------------------------------------
# Rules beat opinion
# ----------------------------------------------------------------------
def test_mandatory_sell_is_not_dependent_on_the_model_agreeing(config) -> None:
    """The model proposes nothing; the stop-loss still fires."""
    llm = FakeLLM(response([]))
    portfolio = make_diversified_portfolio(cash=80_000)
    portfolio.positions[0].last_price = 70.0
    trigger = Trigger(
        symbol="TCS",
        trigger_type=TriggerType.STOP_LOSS,
        severity=Severity.MANDATORY,
        detail="down 30% from cost",
        suggested_action=Action.SELL,
    )

    decision = run(build(config, llm), portfolio, ["TCS"], triggers=[trigger])

    assert [(p.symbol, p.action) for p in decision.proposals] == [("TCS", Action.SELL)]


def test_model_cannot_buy_a_name_failing_the_gates(config) -> None:
    llm = FakeLLM(
        response([{"symbol": "WIPRO", "action": "BUY", "rationale": "looks cheap"}])
    )
    portfolio = make_diversified_portfolio(cash=300_000)
    technicals = {"WIPRO": make_technical("WIPRO")}
    scores = {"WIPRO": make_scores("WIPRO", passes_gates=False, failures=["ROE 4% < 10%"])}

    decision = build(config, llm).decide(
        portfolio=portfolio,
        scores=scores,
        fundamentals={"WIPRO": make_fundamental("WIPRO", roe=4.0)},
        technicals=technicals,
        triggers=[],
        candidates=[],
        digest=None,
        risk_profile=None,
    )

    assert decision.proposals == []
    assert "fails fundamental gates" in decision.memo


def test_model_cannot_breach_the_sector_cap(config) -> None:
    positions = [
        make_position("TCS", quantity=1000, sector="Information Technology"),
        make_position("INFY", quantity=1000, sector="Information Technology"),
        make_position("HCLTECH", quantity=1000, sector="Information Technology"),
        make_position("RELIANCE", quantity=1000, sector="Oil Gas & Consumable Fuels"),
    ]
    portfolio = make_portfolio(positions, cash=200_000)
    llm = FakeLLM(
        response(
            [
                {
                    "symbol": "WIPRO",
                    "action": "BUY",
                    "quantity": 500,
                    "rationale": "more IT exposure",
                }
            ]
        )
    )

    decision = run(build(config, llm), portfolio, ["WIPRO"])

    assert decision.proposals == []
    assert "sector cap" in decision.memo


# ----------------------------------------------------------------------
# Deep review cadence
# ----------------------------------------------------------------------
def test_deep_review_day_is_flagged_and_changes_the_prompt(config) -> None:
    from datetime import date as _date

    llm = FakeLLM(response([]))
    portfolio = make_diversified_portfolio(cash=80_000)
    technicals, scores, fundamentals = context(portfolio, ["TCS"])

    quarter_start = _date(2026, 4, 1)
    decision = build(config, llm).decide(
        portfolio=portfolio,
        scores=scores,
        fundamentals=fundamentals,
        technicals=technicals,
        triggers=[],
        candidates=[],
        digest=None,
        risk_profile=None,
        as_of=quarter_start,
    )

    assert decision.is_deep_review
    assert "DEEP REVIEW" in llm.prompts[0]


def test_ordinary_day_is_monitoring_mode(config) -> None:
    from datetime import date as _date

    llm = FakeLLM(response([]))
    portfolio = make_diversified_portfolio(cash=80_000)
    technicals, scores, fundamentals = context(portfolio, ["TCS"])

    decision = build(config, llm).decide(
        portfolio=portfolio,
        scores=scores,
        fundamentals=fundamentals,
        technicals=technicals,
        triggers=[],
        candidates=[],
        digest=None,
        risk_profile=None,
        as_of=_date(2026, 5, 14),
    )

    assert not decision.is_deep_review
    assert "DAILY MONITORING" in llm.prompts[0]
