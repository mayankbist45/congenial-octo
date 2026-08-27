"""Temporal dispatch and worker definitions for research-only agent runs.

The API only creates auditable work. A separately deployed Temporal worker is
the only process that may execute an agent prompt.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any, Dict, Optional

from src.config import Config, get_config
from src.workspace.agent_platform import AgentRunInput, AgentWorkRepository, agent_contract, build_pydantic_agent, validate_evidence
from src.workspace.agent_sources import AgentSourceCollector

TASK_QUEUE = "nivelo-agent-work"


class AgentRuntime:
    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    def dispatch(self, run_id: str) -> None:
        """Start one durable workflow when Temporal is configured.

        # ponytail: queued work waits for the production Temporal worker when
        # TEMPORAL_ADDRESS is absent; add no in-process agent executor.
        """
        address = os.getenv("TEMPORAL_ADDRESS", "").strip()
        if not address:
            AgentWorkRepository(self._config).record_event(run_id, "dispatch", "queued", {"reason": "Temporal worker is not configured."})
            return
        asyncio.run(self._start(address, run_id))

    async def _start(self, address: str, run_id: str) -> None:
        from temporalio.client import Client

        client = await Client.connect(address, namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))
        workflow_id = f"agent-run-{run_id}"
        await client.start_workflow(AgentRunWorkflow.run, run_id, id=workflow_id, task_queue=TASK_QUEUE)
        AgentWorkRepository(self._config).set_run_status(run_id, "queued", temporal_workflow_id=workflow_id)

    def cancel(self, temporal_workflow_id: str) -> None:
        address = os.getenv("TEMPORAL_ADDRESS", "").strip()
        if address and temporal_workflow_id:
            asyncio.run(self._cancel(address, temporal_workflow_id))

    async def _cancel(self, address: str, temporal_workflow_id: str) -> None:
        from temporalio.client import Client

        client = await Client.connect(address, namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))
        await client.get_workflow_handle(temporal_workflow_id).cancel()


def _artifact_prompt(run: Dict[str, Any], sources: list[Dict[str, Any]]) -> str:
    return (
        "You are a research-only public-markets work agent. Return only the requested structured output. "
        "Do not provide trade instructions. If verified source data is unavailable, set coverage_status to unavailable, "
        "confidence to 0, assessment to investigate, and clearly explain the limitation.\n\n"
        f"Agent: {run['agent_type']}\nBrief: {run['input']['brief']}\nScope: {run['input']['scope']}\n"
        f"Approved source coverage: {sources}"
    )


def execute_agent_run(run_id: str) -> None:
    """Temporal activity: validate the agent result before it reaches the journal."""
    repo = AgentWorkRepository()
    run = repo.run(run_id)
    if run["status"] == "cancelled":
        return
    repo.set_run_status(run_id, "running")
    try:
        model = os.getenv("WORKSPACE_AGENT_MODEL", "").strip()
        if not model:
            raise RuntimeError("WORKSPACE_AGENT_MODEL is not configured.")
        input_payload = AgentRunInput.model_validate(run["input"])
        contract = agent_contract(input_payload.agent_type)
        case = repo.case(run["case_id"])
        completed_artifacts = sum(len(item.get("artifacts", [])) for item in case["runs"] if item["status"] == "completed")
        sources = AgentSourceCollector().collect(contract, input_payload.scope, completed_artifacts)
        result = build_pydantic_agent(contract, model).run_sync(
            _artifact_prompt(run, [source.model_dump(mode="json") for source in sources])
        )
        artifact = result.output
        validate_evidence(contract, artifact, sources)
        repo.save_artifact(run_id, artifact, sources)
        repo.set_run_status(run_id, "completed")
    except Exception as exc:
        repo.set_run_status(run_id, "failed", error=str(exc))


try:  # Keep API/test imports working until the Python 3.11 worker image is deployed.
    from temporalio import activity, workflow

    @activity.defn
    def run_agent_activity(run_id: str) -> None:
        execute_agent_run(run_id)

    @workflow.defn
    class AgentRunWorkflow:
        @workflow.run
        async def run(self, run_id: str) -> None:
            await workflow.execute_activity(run_agent_activity, run_id, start_to_close_timeout=timedelta(minutes=15))
except ImportError:  # pragma: no cover - exercised in the production worker image
    class AgentRunWorkflow:  # type: ignore[no-redef]
        @staticmethod
        async def run(run_id: str) -> None:
            raise RuntimeError("Temporal is not installed. Use the Python 3.11 agent worker image.")
