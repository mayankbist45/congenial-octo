from __future__ import annotations

import asyncio
import os

import pytest

from src.config import get_config
from src.workspace.agent_platform import AgentRunInput, AgentWorkRepository, CaseInput, EvidenceArtifact, SourceRecord
from src.workspace.service import WorkspaceRepository


def _run(temp_config):
    users = WorkspaceRepository(temp_config)
    users.create_invite("pm@example.com", role="pm")
    user = users.user_for_email("pm@example.com")
    repo = AgentWorkRepository(temp_config)
    case = repo.create_case(user["id"], CaseInput(title="Runtime test", brief="Verify durable agent journal behavior.", scope={}))
    run = repo.create_run(case["id"], user["id"], AgentRunInput(agent_type="event_impact", brief="Review a disclosed company event.", scope={}), "runtime-test")
    return repo, case, run


def test_dispatch_identifiers_are_stable_and_cancellation_blocks_persistence(temp_config):
    repo, case, run = _run(temp_config)
    first, second = repo.register_dispatch(run["id"]), repo.register_dispatch(run["id"])
    assert first == second

    repo.cancel_run(case["id"], run["id"])
    artifact = EvidenceArtifact(coverage_status="unavailable", memo="Sources are unavailable.", assessment="investigate", confidence=0, next_step="Retry when sources are connected.")
    source = SourceRecord(label="filing", category="filings", coverage_status="unavailable")
    assert repo.save_artifact(run["id"], artifact, [source]) is False
    assert repo.run(run["id"])["cancellation_requested_at"]


def test_evidence_persistence_replaces_sources_and_records_unknown_cost(temp_config):
    repo, _, run = _run(temp_config)
    artifact = EvidenceArtifact(coverage_status="unavailable", memo="Sources are unavailable.", assessment="investigate", confidence=0, next_step="Retry when sources are connected.")
    source = SourceRecord(label="filing", category="filings", coverage_status="unavailable")
    assert repo.save_artifact(run["id"], artifact, [source], usage_tokens=42) is True
    assert repo.save_artifact(run["id"], artifact, [source], usage_tokens=43) is True
    view = repo.case(run["case_id"])["runs"][0]
    assert len(view["artifacts"]) == len(view["sources"]) == 1
    assert view["usage_tokens"] == 43
    assert view["usage_cost_usd"] == 0
    assert view["usage_cost_status"] == "unknown"
    assert any(event["stage_name"] == "usage" and event["status"] == "unknown" for event in view["events"])


def test_run_events_redact_secrets_and_keep_trace_linkage(temp_config):
    repo, _, run = _run(temp_config)
    repo.register_dispatch(run["id"])
    repo.record_event(run["id"], "tool", "failed", {"api_key": "secret", "message": "Bearer sk-secret-token"})
    event = repo.case(run["case_id"])["runs"][0]["events"][-1]
    assert event["details"]["api_key"] == "[redacted]"
    assert "sk-secret-token" not in event["details"]["message"]
    assert event["details"]["trace_id"] == repo.run(run["id"])["trace_id"]


def test_temporal_workflow_persists_one_audit_trail(temp_config, monkeypatch):
    temporalio = pytest.importorskip("temporalio")
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from src.workspace import agent_runtime

    database_url = os.getenv("FUND_TEST_DB_URL")
    if not database_url:
        pytest.skip("Temporal integration test requires FUND_TEST_DB_URL.")
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_config.cache_clear()
    repo, _, run = _run(temp_config)
    monkeypatch.setattr(agent_runtime, "_generate_output", lambda *_: {
        "artifact": {"coverage_status": "unavailable", "memo": "No connected sources.", "assessment": "investigate", "confidence": 0, "next_step": "Connect approved sources."},
        "usage_tokens": 7,
    })

    async def execute() -> None:
        async with await WorkflowEnvironment.start_time_skipping() as environment:
            async with Worker(environment.client, task_queue=agent_runtime.TASK_QUEUE, workflows=[agent_runtime.AgentRunWorkflow], activities=[agent_runtime.prepare_agent_run_activity, agent_runtime.collect_agent_sources_activity, agent_runtime.persist_agent_result_activity, agent_runtime.finalize_agent_run_activity, agent_runtime.fail_agent_run_activity]), Worker(environment.client, task_queue=agent_runtime.MODEL_TASK_QUEUE, activities=[agent_runtime.generate_agent_output_activity]):
                await environment.client.execute_workflow(agent_runtime.AgentRunWorkflow.run, run["id"], id=f"test-{run['id']}", task_queue=agent_runtime.TASK_QUEUE)

    asyncio.run(execute())
    view = repo.run(run["id"])
    assert view["status"] == "completed"
    assert view["usage_tokens"] == 7
    case_run = repo.case(run["case_id"])["runs"][0]
    assert len(case_run["artifacts"]) == 1
    assert len(case_run["sources"]) == 2
    assert {event["stage_name"] for event in case_run["events"]} >= {"prepare", "collect_sources", "generate_output", "persist", "usage", "completed"}
