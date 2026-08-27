from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.workspace.agent_platform import (
    AgentRunInput,
    EvidenceArtifact,
    SourceRecord,
    agent_contract,
    catalog,
    validate_evidence,
    validate_run_input,
)


def _artifact(**overrides):
    payload = {
        "structured_data": {},
        "key_signals": ["Verified source coverage is available."],
        "coverage_status": "available",
        "caveats": [],
        "memo": "A bounded research memo.",
        "assessment": "monitor",
        "confidence": 0.5,
        "next_step": "Review the cited evidence.",
        "source_labels": ["EODHD", "SEC EDGAR"],
    }
    payload.update(overrides)
    return EvidenceArtifact(**payload)


def test_curated_contracts_declare_versions_sources_and_tools():
    contracts = {item["id"]: item for item in catalog()}
    assert set(contracts) == {"company_deep_dive", "event_impact", "comparative_screen", "portfolio_risk_review", "ic_memo"}
    assert contracts["ic_memo"]["allowed_tools"] == ["read_case_artifacts"]
    assert contracts["ic_memo"]["required_sources"] == ["case_artifacts"]
    assert all(item["version"] == "2" and item["retry_limit"] >= 0 for item in contracts.values())


@pytest.mark.parametrize(
    ("payload", "completed_runs", "message"),
    [
        (AgentRunInput(agent_type="company_deep_dive", brief="Review company fundamentals and filing risks.", scope={}), 0, "requires at least one symbol"),
        (AgentRunInput(agent_type="comparative_screen", brief="Compare the peer set on valuation and growth.", scope={"symbols": ["NVDA"]}), 0, "requires at least two symbols"),
        (AgentRunInput(agent_type="portfolio_risk_review", brief="Review concentration and liquidity risk context.", scope={}), 0, "requires a portfolio persona"),
        (AgentRunInput(agent_type="ic_memo", brief="Synthesize completed work into a decision memo.", scope={}), 0, "requires at least one completed"),
    ],
)
def test_contracts_reject_incomplete_scope(payload, completed_runs, message):
    with pytest.raises(ValueError, match=message):
        validate_run_input(payload, completed_runs)


def test_available_evidence_requires_all_contract_sources():
    contract = agent_contract("company_deep_dive")
    sources = [SourceRecord(label="EODHD", category="fundamentals", coverage_status="available")]
    with pytest.raises(ValueError, match="missing required sources"):
        validate_evidence(contract, _artifact(source_labels=["EODHD"]), sources)


def test_partial_and_unavailable_evidence_are_truthful():
    partial = _artifact(coverage_status="partial", confidence=0.6, source_labels=["EODHD"])
    validate_evidence(agent_contract("company_deep_dive"), partial, [SourceRecord(label="EODHD", category="fundamentals", coverage_status="partial")])
    unavailable = _artifact(coverage_status="unavailable", confidence=0, assessment="investigate", source_labels=[])
    validate_evidence(agent_contract("event_impact"), unavailable, [])
    with pytest.raises(ValueError, match="zero confidence"):
        _artifact(coverage_status="unavailable", confidence=0.1, assessment="investigate")


def test_agent_modules_do_not_depend_on_execution_or_portfolio_writes():
    root = Path(__file__).resolve().parents[1] / "src" / "workspace"
    text = "\n".join((root / name).read_text() for name in ("agent_platform.py", "agent_runtime.py", "agent_sources.py"))
    for forbidden in ("src.broker", "src.proposal", "src.trade", "save_portfolio", "update_holdings"):
        assert forbidden not in text


@pytest.mark.skipif(importlib.util.find_spec("pydantic_ai") is None, reason="PydanticAI runs in the Python 3.11 CI and worker image.")
def test_pydantic_test_model_exposes_only_contract_tools():
    from pydantic_ai.models.test import TestModel

    from src.workspace.agent_platform import build_pydantic_agent

    model = TestModel()
    agent = build_pydantic_agent(agent_contract("ic_memo"), model, output_type=str)
    result = agent.run_sync("List the available evidence boundary.")
    assert result.output
    assert [tool.name for tool in model.last_model_request_parameters.function_tools] == ["read_case_artifacts"]
