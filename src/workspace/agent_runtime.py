"""Durable Temporal runtime for research-only agent work."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any, Dict, Optional

from src.config import Config, get_config
from src.workspace.agent_platform import AgentRunInput, AgentWorkRepository, EvidenceArtifact, SourceRecord, agent_contract, build_pydantic_agent, coverage_status, source_conflicts, validate_evidence
from src.workspace.agent_sources import AgentSourceCollector

TASK_QUEUE = "nivelo-agent-work"
MODEL_TASK_QUEUE = "nivelo-agent-model"


class AgentRuntime:
    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    def dispatch(self, run_id: str) -> None:
        repo = AgentWorkRepository(self._config)
        dispatch = repo.register_dispatch(run_id)
        address = os.getenv("TEMPORAL_ADDRESS", "").strip()
        if not address:
            repo.record_event(run_id, "dispatch", "queued", {"reason": "Temporal worker is not configured.", **dispatch})
            return
        asyncio.run(self._start(address, run_id, dispatch["workflow_id"]))

    async def _start(self, address: str, run_id: str, workflow_id: str) -> None:
        from temporalio.client import Client
        from temporalio.exceptions import WorkflowAlreadyStartedError

        client = await Client.connect(address, namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))
        try:
            await client.start_workflow(AgentRunWorkflow.run, run_id, id=workflow_id, task_queue=TASK_QUEUE)
            AgentWorkRepository(self._config).record_event(run_id, "dispatch", "started", {"workflow_id": workflow_id})
        except WorkflowAlreadyStartedError:
            AgentWorkRepository(self._config).record_event(run_id, "dispatch", "duplicate", {"workflow_id": workflow_id})

    def cancel(self, run_id: str, temporal_workflow_id: str) -> None:
        address = os.getenv("TEMPORAL_ADDRESS", "").strip()
        if not address or not temporal_workflow_id:
            AgentWorkRepository(self._config).set_run_status(run_id, "cancelled")
            return
        asyncio.run(self._cancel(address, run_id, temporal_workflow_id))

    async def _cancel(self, address: str, run_id: str, temporal_workflow_id: str) -> None:
        from temporalio.client import Client

        client = await Client.connect(address, namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))
        await client.get_workflow_handle(temporal_workflow_id).cancel()
        AgentWorkRepository(self._config).set_run_status(run_id, "cancelled")


def _artifact_prompt(run: Dict[str, Any], sources: list[Dict[str, Any]]) -> str:
    return (
        "You are a research-only public-markets work agent. Return only the requested structured output. "
        "Do not provide trade instructions. Do not introduce facts beyond the supplied source material. If verified source data is unavailable, set coverage_status to unavailable, "
        "confidence to 0, assessment to investigate, and clearly explain the limitation.\n\n"
        f"Agent: {run['agent_type']}\nBrief: {run['input']['brief']}\nScope: {run['input']['scope']}\n"
        f"Approved source coverage: {sources}"
    )


def _completed_case_artifacts(repo: AgentWorkRepository, run: Dict[str, Any]) -> list[Dict[str, Any]]:
    case = repo.case(run["case_id"])
    return [{"run_id": item["id"], "artifact": artifact} for item in case["runs"] if item["id"] != run["id"] and item["status"] == "completed" for artifact in item["artifacts"]]


def _usage_tokens(result: Any) -> int:
    usage = getattr(result, "usage", None)
    usage = usage() if callable(usage) else usage
    return int(getattr(usage, "total_tokens", 0) or 0)


def _usage_cost(result: Any) -> Optional[float]:
    usage = getattr(result, "usage", None)
    usage = usage() if callable(usage) else usage
    cost = getattr(usage, "cost", None)
    return float(cost) if isinstance(cost, (int, float)) else None


def _prepare_run(run_id: str) -> Dict[str, Any]:
    repo = AgentWorkRepository()
    run = repo.run(run_id)
    if run["status"] in {"completed", "failed", "cancelled"} or repo.is_cancellation_requested(run_id):
        if run["status"] != "cancelled":
            repo.set_run_status(run_id, "cancelled")
        return {"terminal": True}
    if run["status"] == "queued":
        repo.set_run_status(run_id, "running")
    contract = agent_contract(AgentRunInput.model_validate(run["input"]).agent_type)
    repo.record_event(run_id, "prepare", "completed", {"contract_version": contract.version})
    return {"terminal": False, "source_attempts": contract.retry_limit + 1, "timeout_seconds": contract.timeout_seconds}


def _collect_sources(run_id: str) -> list[Dict[str, Any]]:
    repo = AgentWorkRepository()
    run = repo.run(run_id)
    if repo.is_cancellation_requested(run_id):
        return []
    payload = AgentRunInput.model_validate(run["input"])
    contract = agent_contract(payload.agent_type)
    case = repo.case(run["case_id"])
    artifacts = _completed_case_artifacts(repo, run)
    completed = len(artifacts)
    sources = AgentSourceCollector().collect(contract, payload.scope, completed)
    if payload.agent_type == "ic_memo":
        sources[0].details["artifact_run_ids"] = sorted({item["run_id"] for item in artifacts})
    repo.record_event(run_id, "collect_sources", "completed", {"coverage": coverage_status(sources, contract.required_sources), "conflicts": source_conflicts(sources)})
    return [source.model_dump(mode="json") for source in sources]


def _generate_output(run_id: str, source_payloads: list[Dict[str, Any]]) -> Dict[str, Any]:
    repo = AgentWorkRepository()
    run = repo.run(run_id)
    if repo.is_cancellation_requested(run_id):
        return {"cancelled": True}
    model_name = os.getenv("WORKSPACE_AGENT_MODEL", "").strip()
    if not model_name:
        raise RuntimeError("Agent model is not configured.")
    # The default OpenAI provider routes to the Responses API, which DeepSeek
    # does not support and whose tool_choice its thinking mode rejects. Use the
    # chat-completions model and disable DeepSeek thinking so tool calls work.
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
    api_key = os.getenv("OPENAI_API_KEY", "").strip() or None
    model = OpenAIChatModel(
        model_name.removeprefix("openai:"),
        provider=OpenAIProvider(base_url=base_url, api_key=api_key),
    )
    model_settings: Dict[str, Any] = {"max_tokens": 2000}
    if base_url and "deepseek" in base_url.lower():
        model_settings["extra_body"] = {"thinking": {"type": "disabled"}}
    payload = AgentRunInput.model_validate(run["input"])
    contract = agent_contract(payload.agent_type)
    prompt_sources = source_payloads
    if payload.agent_type == "ic_memo":
        prompt_sources = [{**source, "completed_artifacts": _completed_case_artifacts(repo, run)} for source in source_payloads]
    result = build_pydantic_agent(contract, model).run_sync(_artifact_prompt(run, prompt_sources), model_settings=model_settings)
    artifact = EvidenceArtifact.model_validate(result.output)
    tokens, cost = _usage_tokens(result), _usage_cost(result)
    if tokens > contract.token_budget:
        raise ValueError("Agent token budget exceeded.")
    repo.record_event(run_id, "generate_output", "completed", {"tokens": tokens, "cost_usd": cost})
    return {"artifact": artifact.model_dump(mode="json"), "usage_tokens": tokens, "usage_cost_usd": cost}


def _persist_result(run_id: str, source_payloads: list[Dict[str, Any]], output: Dict[str, Any]) -> bool:
    if output.get("cancelled"):
        return False
    repo = AgentWorkRepository()
    run = repo.run(run_id)
    if repo.is_cancellation_requested(run_id):
        return False
    payload = AgentRunInput.model_validate(run["input"])
    contract = agent_contract(payload.agent_type)
    sources = [SourceRecord.model_validate(item) for item in source_payloads]
    artifact = EvidenceArtifact.model_validate(output["artifact"])
    validate_evidence(contract, artifact, sources)
    persisted = repo.save_artifact(run_id, artifact, sources, int(output.get("usage_tokens", 0)), output.get("usage_cost_usd"))
    repo.record_event(run_id, "persist", "completed" if persisted else "cancelled")
    return persisted


def _finalize_run(run_id: str, persisted: bool) -> None:
    repo = AgentWorkRepository()
    repo.set_run_status(run_id, "cancelled" if repo.is_cancellation_requested(run_id) or not persisted else "completed")


def _fail_run(run_id: str, stage: str, error_type: str) -> None:
    repo = AgentWorkRepository()
    if repo.is_cancellation_requested(run_id):
        repo.set_run_status(run_id, "cancelled")
    else:
        failure_status = "timeout" if "timeout" in error_type.lower() else "failed"
        repo.record_event(run_id, "failure", failure_status, {"stage": stage, "type": error_type})
        repo.set_run_status(run_id, "failed", error=f"{stage} failed ({error_type}).")


try:  # API imports remain valid in the Python 3.9 web process.
    from temporalio import activity, workflow
    from temporalio.common import RetryPolicy

    @activity.defn
    def prepare_agent_run_activity(run_id: str) -> Dict[str, Any]:
        return _prepare_run(run_id)

    @activity.defn
    def collect_agent_sources_activity(run_id: str) -> list[Dict[str, Any]]:
        return _collect_sources(run_id)

    @activity.defn
    def generate_agent_output_activity(run_id: str, source_payloads: list[Dict[str, Any]]) -> Dict[str, Any]:
        return _generate_output(run_id, source_payloads)

    @activity.defn
    def persist_agent_result_activity(run_id: str, source_payloads: list[Dict[str, Any]], output: Dict[str, Any]) -> bool:
        return _persist_result(run_id, source_payloads, output)

    @activity.defn
    def finalize_agent_run_activity(run_id: str, persisted: bool) -> None:
        _finalize_run(run_id, persisted)

    @activity.defn
    def fail_agent_run_activity(run_id: str, stage: str, error_type: str) -> None:
        _fail_run(run_id, stage, error_type)

    @workflow.defn
    class AgentRunWorkflow:
        @workflow.run
        async def run(self, run_id: str) -> None:
            stage = "prepare"
            try:
                prepared = await workflow.execute_activity(prepare_agent_run_activity, run_id, start_to_close_timeout=timedelta(minutes=1))
                if prepared["terminal"]:
                    return
                stage = "collect_sources"
                sources = await workflow.execute_activity(collect_agent_sources_activity, run_id, start_to_close_timeout=timedelta(seconds=prepared["timeout_seconds"]), retry_policy=RetryPolicy(maximum_attempts=prepared["source_attempts"]))
                stage = "generate_output"
                output = await workflow.execute_activity(generate_agent_output_activity, args=[run_id, sources], task_queue=MODEL_TASK_QUEUE, start_to_close_timeout=timedelta(seconds=prepared["timeout_seconds"]), retry_policy=RetryPolicy(maximum_attempts=1))
                stage = "persist"
                persisted = await workflow.execute_activity(persist_agent_result_activity, args=[run_id, sources, output], start_to_close_timeout=timedelta(minutes=1))
                await workflow.execute_activity(finalize_agent_run_activity, args=[run_id, persisted], start_to_close_timeout=timedelta(minutes=1))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await workflow.execute_activity(fail_agent_run_activity, args=[run_id, stage, type(exc).__name__], start_to_close_timeout=timedelta(minutes=1))
except ImportError:  # pragma: no cover - exercised in the production worker image
    class AgentRunWorkflow:  # type: ignore[no-redef]
        @staticmethod
        async def run(run_id: str) -> None:
            raise RuntimeError("Temporal is not installed. Use the Python 3.11 agent worker image.")
