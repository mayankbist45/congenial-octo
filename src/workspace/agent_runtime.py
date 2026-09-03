"""Durable Temporal runtime for research-only agent work."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any, Dict, Optional

from src.config import Config, get_config
from src.workspace.agent_platform import (
    AGENT_GUIDANCE,
    AgentRunInput,
    AgentWorkRepository,
    EvidenceArtifact,
    SourceRecord,
    agent_contract,
    coverage_status,
    source_conflicts,
    validate_evidence,
)
from src.workspace.agent_sources import AgentSourceCollector, invoke_tool, tool_schema

TASK_QUEUE = "nivelo-agent-work"
MODEL_TASK_QUEUE = "nivelo-agent-model"
MAX_GATHER_ROUNDS = 3

_ARTIFACT_INSTRUCTIONS = (
    "Return ONLY a valid JSON object for the research EvidenceArtifact with exactly these fields: "
    "structured_data (object of decision-relevant data), key_signals (array of strings), "
    "coverage_status (one of \"available\", \"partial\", \"stale\", \"unavailable\"), caveats (array of strings), "
    "memo (the source-backed analysis), assessment (one of \"strengthen\", \"monitor\", \"investigate\", \"no_action\", \"escalate\"), "
    "confidence (number 0..1), next_step (string), source_labels (array of the exact provider labels you relied on, e.g. \"EODHD\" or \"SEC EDGAR\"), "
    "and source_artifact_run_ids (array of run ids; IC Memo only, otherwise an empty array). "
    "Set coverage_status from the tool results you received: available only when EVERY required source category "
    "returned usable data; partial when at least one did but not all; unavailable only when NONE did. "
    "Never mark the whole artifact unavailable when a tool returned usable data - analyse that evidence and use partial. "
    "coverage unavailable requires confidence 0 and assessment \"investigate\"; coverage partial or stale caps confidence at 0.6. "
    "Never recommend orders, sizing, or execution."
)


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


def _live_prompt(run: Dict[str, Any], contract: Any) -> str:
    return (
        "You are a research-only public-markets work agent. You NEVER propose orders, sizing, or execution. "
        "Gather evidence by calling the tools available to you before writing your analysis; never invent facts. "
        "A tool result with coverage_status \"unavailable\" is NOT usable evidence. Set your final coverage_status "
        "from the tool results you actually received: available only when EVERY required source category returned "
        "usable data, partial when at least one did but not all, unavailable only when NONE did. "
        "Cite the exact provider label of every tool result you relied on in source_labels.\n\n"
        f"{AGENT_GUIDANCE.get(contract.id, '')}\n\n"
        f"Agent: {run['agent_type']}\n"
        f"Brief: {run['input']['brief']}\n"
        f"Scope: {run['input']['scope']}\n\n"
        f"{_ARTIFACT_INSTRUCTIONS}"
    )


def _model_client() -> tuple[Any, str]:
    from openai import OpenAI

    model_name = os.getenv("WORKSPACE_AGENT_MODEL", "").strip().removeprefix("openai:")
    if not model_name:
        raise RuntimeError("Agent model is not configured.")
    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
    api_key = os.getenv("OPENAI_API_KEY", "").strip() or None
    client = OpenAI(base_url=base_url, api_key=api_key)
    return client, model_name


def _request_kwargs() -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {"max_tokens": 4000}
    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
    # The default OpenAI provider routes to the Responses API, which DeepSeek
    # does not support and whose tool_choice its thinking mode rejects. Use the
    # chat-completions model and disable DeepSeek thinking so tool calls work.
    if base_url and "deepseek" in base_url.lower():
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return kwargs


def _message_with_tool_calls(message: Any) -> Dict[str, Any]:
    calls = [
        {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}}
        for call in (message.tool_calls or [])
    ]
    entry: Dict[str, Any] = {"role": "assistant", "content": message.content}
    if calls:  # deepseek rejects an empty tool_calls array
        entry["tool_calls"] = calls
    return entry


def _json_artifact(raw: str) -> EvidenceArtifact:
    import json as _json

    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    parsed = _json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Artifact JSON must be an object.")
    try:
        return EvidenceArtifact.model_validate(parsed)
    except Exception:
        return _coerce_artifact(parsed)


def _coerce_artifact(raw: Dict[str, Any]) -> EvidenceArtifact:
    """Repair a model's drifted JSON into a valid EvidenceArtifact.

    Models occasionally rename fields or omit optionals (e.g. using run_ids
    instead of source_artifact_run_ids). We fill defaults and apply the same
    coverage/confidence caps the schema enforces; _honest_artifact then
    normalizes coverage to the recorded sources.
    """
    valid_coverage = {"available", "partial", "stale", "unavailable"}
    valid_assessment = {"strengthen", "monitor", "investigate", "no_action", "escalate"}
    coverage = raw.get("coverage_status") if raw.get("coverage_status") in valid_coverage else "unavailable"
    assessment = raw.get("assessment") if raw.get("assessment") in valid_assessment else "investigate"
    try:
        confidence = float(raw.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if coverage == "unavailable":
        confidence, assessment = 0.0, "investigate"
    elif coverage in {"partial", "stale"}:
        confidence = min(confidence, 0.6)
    caveat = raw.get("caveat")
    caveats = list(raw.get("caveats") or ([caveat] if caveat else []))
    return EvidenceArtifact(
        structured_data=raw.get("structured_data") or {},
        key_signals=list(raw.get("key_signals") or raw.get("signals") or []),
        coverage_status=coverage,
        caveats=caveats,
        memo=str(raw.get("memo") or raw.get("summary") or ""),
        assessment=assessment,
        confidence=min(max(confidence, 0.0), 1.0),
        next_step=str(raw.get("next_step") or raw.get("recommendation") or "Review the evidence and decide next steps."),
        source_labels=list(raw.get("source_labels") or []),
        source_artifact_run_ids=list(raw.get("source_artifact_run_ids") or raw.get("run_ids") or raw.get("artifact_run_ids") or []),
    )


def _generate_output(run_id: str) -> Dict[str, Any]:
    import json as _json

    repo = AgentWorkRepository()
    run = repo.run(run_id)
    if repo.is_cancellation_requested(run_id):
        return {"cancelled": True}
    client, model_name = _model_client()
    kwargs = _request_kwargs()

    payload = AgentRunInput.model_validate(run["input"])
    contract = agent_contract(payload.agent_type)
    collector = AgentSourceCollector(config=get_config(), scope=payload.scope, repo=repo, run=run)
    schemas = [schema for name in contract.allowed_tools if (schema := tool_schema(name)) is not None]
    if not schemas:
        raise RuntimeError("Agent contract has no enabled source tools.")

    messages: list[Dict[str, Any]] = [
        {"role": "system", "content": _live_prompt(run, contract)},
        {"role": "user", "content": f"Resolve the research task by calling the tools you need, then return the EvidenceArtifact JSON.\nTask: {run['input']['brief']}"},
    ]
    tokens = 0
    completion_tokens = 0
    for _round in range(MAX_GATHER_ROUNDS):
        response = client.chat.completions.create(model=model_name, messages=messages, tools=schemas, tool_choice="auto", **kwargs)
        tokens += int((response.usage.total_tokens if response.usage else 0) or 0)
        completion_tokens += int((response.usage.completion_tokens if response.usage else 0) or 0)
        message = response.choices[0].message
        messages.append(_message_with_tool_calls(message))
        if not message.tool_calls:
            break
        for call in message.tool_calls:
            try:
                arguments = _json.loads(call.function.arguments or "{}")
            except ValueError:
                arguments = {}
            result = invoke_tool(collector, call.function.name, arguments)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": _json.dumps(result)})

    messages.append({"role": "user", "content": "Return the final EvidenceArtifact as JSON now."})
    artifact: Optional[EvidenceArtifact] = None
    last_raw = ""
    for _attempt in range(2):
        response = client.chat.completions.create(model=model_name, messages=messages, response_format={"type": "json_object"}, **kwargs)
        tokens += int((response.usage.total_tokens if response.usage else 0) or 0)
        completion_tokens += int((response.usage.completion_tokens if response.usage else 0) or 0)
        # The budget guards generation output, not the re-sent tool history.
        if completion_tokens > contract.token_budget:
            raise ValueError("Agent token budget exceeded.")
        last_raw = response.choices[0].message.content or ""
        try:
            artifact = _json_artifact(last_raw)
            break
        except Exception:
            messages.append({"role": "user", "content": "That was not a valid EvidenceArtifact JSON. Return a corrected JSON object now."})
    if artifact is None:
        raise ValueError(f"Agent did not return a valid EvidenceArtifact: {last_raw[:200]}")

    sources = collector.sources
    artifact = _honest_artifact(contract, artifact, sources)
    if contract.id == "ic_memo":
        allowed = sorted({rid for source in sources for rid in source.details.get("artifact_run_ids", [])})
        if allowed:
            artifact = artifact.model_copy(update={"source_artifact_run_ids": allowed})
    coverage = "unavailable" if not sources else coverage_status(sources, contract.required_sources)
    repo.record_event(run_id, "collect_sources", "completed", {"coverage": coverage, "conflicts": source_conflicts(sources), "source_count": len(sources)})
    repo.record_event(run_id, "generate_output", "completed", {"tokens": tokens, "cost_usd": None, "tool_calls": len(sources)})
    return {
        "artifact": artifact.model_dump(mode="json"),
        "usage_tokens": tokens,
        "usage_cost_usd": None,
        "sources": [source.model_dump(mode="json") for source in sources],
    }


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


def _honest_artifact(contract: Any, artifact: EvidenceArtifact, sources: list[SourceRecord]) -> EvidenceArtifact:
    """Normalize the artifact to the truthful source-based state.

    This is the required-sources backstop: coverage reflects which of the
    contract's required source categories actually returned usable evidence,
    and source_labels only ever cites providers the run actually consulted, so
    a lazy or over-cautious model can neither hide real evidence nor cite
    sources it never reached.
    """
    expected = coverage_status(sources, contract.required_sources) if sources else "unavailable"
    labels = [source.label for source in sources]
    available_labels = list(dict.fromkeys(source.label for source in sources if source.coverage_status == "available"))
    fields = artifact.model_dump()
    changed = artifact.coverage_status != expected
    if changed:
        available = sum(1 for source in sources if source.coverage_status == "available")
        if expected == "unavailable":
            fields.update(coverage_status="unavailable", confidence=0.0, assessment="investigate")
        else:
            fields["coverage_status"] = expected
            fields["confidence"] = min(float(fields.get("confidence") or 0.0), 0.6)
        caveats = list(fields.get("caveats") or [])
        caveats.append(
            f"Coverage corrected to {expected} to match the sources: {available} of {len(contract.required_sources)} "
            f"required source categories returned available evidence (model reported {artifact.coverage_status})."
        )
        fields["caveats"] = caveats
    cited = [label for label in (fields.get("source_labels") or []) if label in labels]
    fallback = available_labels or (labels if expected != "unavailable" else [])
    if not cited:
        cited = fallback if expected != "unavailable" else []
    fields["source_labels"] = cited
    return EvidenceArtifact.model_validate(fields)


def _persist_result(run_id: str, output: Dict[str, Any]) -> bool:
    if output.get("cancelled"):
        return False
    repo = AgentWorkRepository()
    run = repo.run(run_id)
    if repo.is_cancellation_requested(run_id):
        return False
    payload = AgentRunInput.model_validate(run["input"])
    contract = agent_contract(payload.agent_type)
    sources = [SourceRecord.model_validate(item) for item in output.get("sources", [])]
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
    def generate_agent_output_activity(run_id: str) -> Dict[str, Any]:
        return _generate_output(run_id)

    @activity.defn
    def persist_agent_result_activity(run_id: str, output: Dict[str, Any]) -> bool:
        return _persist_result(run_id, output)

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
                stage = "generate_output"
                output = await workflow.execute_activity(generate_agent_output_activity, run_id, task_queue=MODEL_TASK_QUEUE, start_to_close_timeout=timedelta(seconds=prepared["timeout_seconds"]), retry_policy=RetryPolicy(maximum_attempts=1))
                stage = "persist"
                persisted = await workflow.execute_activity(persist_agent_result_activity, args=[run_id, output], start_to_close_timeout=timedelta(minutes=1), retry_policy=RetryPolicy(maximum_attempts=1))
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
