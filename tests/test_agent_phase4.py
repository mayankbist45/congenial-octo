from __future__ import annotations

import pytest

from src.workspace.agent_platform import EvidenceArtifact, SourceRecord, agent_contract, validate_evidence


def _artifact(status: str) -> EvidenceArtifact:
    return EvidenceArtifact(
        coverage_status=status,
        memo="Evidence is limited to the supplied source records.",
        assessment="investigate",
        confidence=0 if status == "unavailable" else 0.5,
        next_step="Refresh the missing source coverage.",
        source_labels=["Market", "Filings"],
    )


def test_evidence_cannot_be_more_optimistic_than_required_sources():
    sources = [
        SourceRecord(label="Market", category="fundamentals", coverage_status="available"),
        SourceRecord(label="Filings", category="filings", coverage_status="unavailable"),
    ]
    with pytest.raises(ValueError, match="missing required sources"):
        validate_evidence(agent_contract("company_deep_dive"), _artifact("available"), sources)
    validate_evidence(agent_contract("company_deep_dive"), _artifact("partial"), sources)


def test_conflicting_sources_and_ic_memo_references_are_inspectable():
    conflicting = [
        SourceRecord(label="Market live", category="fundamentals", coverage_status="available"),
        SourceRecord(label="Market stale", category="fundamentals", coverage_status="stale"),
        SourceRecord(label="Filings", category="filings", coverage_status="available"),
    ]
    validate_evidence(agent_contract("company_deep_dive"), _artifact("partial").model_copy(update={"source_labels": ["Market live", "Filings"]}), conflicting)

    memo = EvidenceArtifact(coverage_status="available", memo="Synthesis only.", assessment="monitor", confidence=0.5, next_step="Review the completed evidence.", source_labels=["Completed case artifacts"], source_artifact_run_ids=["run-1"])
    source = SourceRecord(label="Completed case artifacts", category="case_artifacts", coverage_status="available", details={"artifact_run_ids": ["run-1"]})
    validate_evidence(agent_contract("ic_memo"), memo, [source])

    with pytest.raises(ValueError, match="unknown completed case artifact"):
        validate_evidence(agent_contract("ic_memo"), memo.model_copy(update={"source_artifact_run_ids": ["run-2"]}), [source])
