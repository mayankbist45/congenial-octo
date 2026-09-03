"""Auditable, research-only agent work for the shared workspace.

This module deliberately knows nothing about brokers, proposals, or trades.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

from src.config import Config, get_config
from src.journal.db import connect

Assessment = Literal["strengthen", "monitor", "investigate", "no_action", "escalate"]
CoverageStatus = Literal["available", "partial", "stale", "unavailable"]
AgentType = Literal["company_deep_dive", "event_impact", "comparative_screen", "portfolio_risk_review", "ic_memo"]
SourceCategory = Literal["market_data", "fundamentals", "news", "filings", "portfolio_snapshot", "case_artifacts"]
ToolName = Literal["fetch_eodhd_market_data", "fetch_eodhd_fundamentals", "fetch_eodhd_news", "fetch_primary_filings", "read_portfolio_snapshot", "read_case_artifacts"]
_COVERAGE_ORDER = {"available": 0, "stale": 1, "partial": 2, "unavailable": 3}
_SECRET = re.compile(r"(?i)(bearer\s+|sk-[a-z0-9_-]{12,}|api[_-]?key\s*[=:]\s*)[^\s,;]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def source_conflicts(sources: Iterable["SourceRecord"]) -> Dict[str, List[str]]:
    grouped: Dict[str, set[str]] = {}
    for source in sources:
        grouped.setdefault(source.category, set()).add(source.coverage_status)
    return {category: sorted(statuses) for category, statuses in grouped.items() if len(statuses) > 1}


def coverage_status(sources: List["SourceRecord"], required_sources: Iterable[str] = ()) -> CoverageStatus:
    """Return the least optimistic truthful status for a source set."""
    statuses = {source.coverage_status for source in sources}
    categories = {source.category for source in sources}
    if set(required_sources) - categories or source_conflicts(sources):
        return "partial"
    if not statuses or statuses == {"unavailable"}:
        return "unavailable"
    if "unavailable" in statuses or "partial" in statuses:
        return "partial"
    return "stale" if "stale" in statuses else "available"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[redacted]" if key.lower() in {"api_key", "authorization", "password", "secret", "credential"} else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return _SECRET.sub(r"\1[redacted]", value) if isinstance(value, str) else value


class WorkScope(BaseModel):
    symbols: List[str] = Field(default_factory=list, max_length=50)
    market: Optional[str] = Field(default=None, max_length=64)
    exchange: Optional[str] = Field(default=None, max_length=64)
    currency: Optional[str] = Field(default=None, max_length=8)
    portfolio_persona_id: Optional[str] = Field(default=None, max_length=64)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, values: List[str]) -> List[str]:
        return list(dict.fromkeys(value.strip().upper() for value in values if value.strip()))


class CaseInput(BaseModel):
    title: str = Field(min_length=3, max_length=180)
    brief: str = Field(min_length=10, max_length=8_000)
    scope: WorkScope = Field(default_factory=WorkScope)
    deadline_at: Optional[datetime] = None


class AgentRunInput(BaseModel):
    agent_type: AgentType
    brief: str = Field(min_length=10, max_length=8_000)
    scope: WorkScope = Field(default_factory=WorkScope)


class CommentInput(BaseModel):
    body: str = Field(min_length=1, max_length=4_000)


class FinalizeInput(BaseModel):
    assessment: Assessment
    note: str = Field(min_length=1, max_length=8_000)


class SourceRecord(BaseModel):
    label: str
    category: SourceCategory = "case_artifacts"
    source_url: Optional[str] = None
    observed_at: Optional[str] = None
    coverage_status: CoverageStatus
    details: Dict[str, Any] = Field(default_factory=dict)


class EvidenceArtifact(BaseModel):
    structured_data: Dict[str, Any] = Field(default_factory=dict)
    key_signals: List[str] = Field(default_factory=list)
    coverage_status: CoverageStatus
    caveats: List[str] = Field(default_factory=list)
    memo: str
    assessment: Assessment
    confidence: float = Field(ge=0, le=1)
    next_step: str
    source_labels: List[str] = Field(default_factory=list, max_length=50)
    source_artifact_run_ids: List[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unavailable_artifacts_are_explicit(self) -> "EvidenceArtifact":
        if self.coverage_status == "unavailable" and (self.confidence != 0 or self.assessment != "investigate"):
            raise ValueError("Unavailable evidence must have zero confidence and an investigate assessment.")
        if self.coverage_status in {"partial", "stale"} and self.confidence > 0.6:
            raise ValueError("Partial or stale evidence cannot have confidence above 0.6.")
        return self


class AgentContract(BaseModel):
    id: AgentType
    version: str
    label: str
    description: str
    allowed_tools: Tuple[ToolName, ...]
    required_sources: Tuple[SourceCategory, ...]
    timeout_seconds: int = Field(gt=0, le=900)
    retry_limit: int = Field(ge=0, le=3)
    token_budget: int = Field(gt=0, le=20_000)
    requires_symbols: bool = False
    minimum_symbols: int = 0
    requires_portfolio: bool = False
    requires_completed_case: bool = False


AGENT_CATALOG: Dict[str, AgentContract] = {
    "company_deep_dive": AgentContract(id="company_deep_dive", version="2", label="Company Deep Dive", description="Build a source-backed company view across fundamentals, valuation, catalysts, and risks.", allowed_tools=("fetch_eodhd_fundamentals", "fetch_primary_filings"), required_sources=("fundamentals", "filings"), requires_symbols=True, timeout_seconds=900, retry_limit=1, token_budget=18_000),
    "event_impact": AgentContract(id="event_impact", version="2", label="Event Impact", description="Assess how a company, sector, or market event changes the specified scope.", allowed_tools=("fetch_eodhd_news", "fetch_primary_filings"), required_sources=("news", "filings"), timeout_seconds=600, retry_limit=1, token_budget=12_000),
    "comparative_screen": AgentContract(id="comparative_screen", version="2", label="Comparative Screen", description="Compare a focused set of securities on normalized evidence and disclosed coverage.", allowed_tools=("fetch_eodhd_market_data", "fetch_eodhd_fundamentals"), required_sources=("market_data", "fundamentals"), requires_symbols=True, minimum_symbols=2, timeout_seconds=900, retry_limit=1, token_budget=16_000),
    "portfolio_risk_review": AgentContract(id="portfolio_risk_review", version="2", label="Portfolio Risk Review", description="Review portfolio context without changing holdings, exposures, or risk limits.", allowed_tools=("read_portfolio_snapshot", "fetch_eodhd_market_data"), required_sources=("portfolio_snapshot", "market_data"), requires_portfolio=True, timeout_seconds=600, retry_limit=1, token_budget=10_000),
    "ic_memo": AgentContract(id="ic_memo", version="2", label="IC Memo", description="Synthesize completed case evidence into a reviewable investment-committee memo.", allowed_tools=("read_case_artifacts",), required_sources=("case_artifacts",), requires_completed_case=True, timeout_seconds=600, retry_limit=0, token_budget=10_000),
}


def catalog() -> List[Dict[str, Any]]:
    return [contract.model_dump(mode="json") for contract in AGENT_CATALOG.values()]


AGENT_GUIDANCE: Dict[AgentType, str] = {
    "company_deep_dive": "Build a source-backed company view: fetch fundamentals and primary filings, then cover valuation, profitability, growth, balance-sheet strength, key catalysts, and the principal risks.",
    "event_impact": "Assess how a named event changes the picture for the scope: fetch the relevant news and primary filings, state the expected impact and magnitude, and say what evidence would change the conclusion.",
    "comparative_screen": "Compare the securities in scope on normalized evidence: fetch market data and fundamentals for EVERY symbol in scope, present an explicit side-by-side comparison, and rank order with the reasons.",
    "portfolio_risk_review": "Review portfolio context only: read the portfolio snapshot, fetch market data relevant to its holdings or exposures, and assess concentration, factor posture, and risk warnings without recommending any trade.",
    "ic_memo": "Synthesize ONLY the completed artifacts already in this case into an IC memo: read the case artifacts, cite their run_ids in source_artifact_run_ids, and never introduce outside facts as source evidence.",
}


def agent_contract(agent_type: AgentType) -> AgentContract:
    return AGENT_CATALOG[agent_type]


def validate_run_input(payload: AgentRunInput, completed_runs: int = 0) -> None:
    spec = agent_contract(payload.agent_type)
    if spec.requires_symbols and not payload.scope.symbols:
        raise ValueError("This agent requires at least one symbol.")
    if len(payload.scope.symbols) < spec.minimum_symbols:
        raise ValueError("This agent requires at least two symbols for comparison.")
    if spec.requires_portfolio and not payload.scope.portfolio_persona_id:
        raise ValueError("Portfolio Risk Review requires a portfolio persona.")
    if spec.requires_completed_case and not completed_runs:
        raise ValueError("IC Memo requires at least one completed agent run in the case.")


def validate_evidence(contract: AgentContract, artifact: EvidenceArtifact, sources: List[SourceRecord]) -> None:
    labels = {source.label for source in sources}
    if set(artifact.source_labels) - labels:
        raise ValueError("Evidence references an unknown source.")
    available = {source.category for source in sources if source.coverage_status == "available"}
    missing = set(contract.required_sources) - available
    if artifact.coverage_status == "available" and missing:
        raise ValueError("Available evidence is missing required sources.")
    expected_coverage = coverage_status(sources, contract.required_sources)
    if _COVERAGE_ORDER[artifact.coverage_status] < _COVERAGE_ORDER[expected_coverage]:
        raise ValueError("Evidence coverage is more optimistic than its sources.")
    if artifact.coverage_status != "unavailable" and not artifact.source_labels:
        raise ValueError("Evidence with coverage must cite at least one source.")
    allowed_artifact_runs = {run_id for source in sources for run_id in source.details.get("artifact_run_ids", [])}
    if contract.id == "ic_memo":
        if not artifact.source_artifact_run_ids:
            raise ValueError("IC Memo evidence must reference completed case artifacts.")
        if set(artifact.source_artifact_run_ids) - allowed_artifact_runs:
            raise ValueError("IC Memo references an unknown completed case artifact.")
    elif artifact.source_artifact_run_ids:
        raise ValueError("Only IC Memo evidence may reference case artifacts.")


def build_pydantic_agent(contract: AgentContract, model: Any, output_type: Any = EvidenceArtifact, tools: Optional[Dict[str, Any]] = None) -> Any:
    """Build a worker-only PydanticAI agent restricted to the contract's tools.

    ``tools`` maps each allowed tool name to a callable that fetches the
    evidence. When omitted, a no-op placeholder is registered so the tool
    surface (and its contract restriction) is still visible to the model and
    testable; the runtime supplies real bound collectors.
    """
    from pydantic_ai import Agent

    guidance = AGENT_GUIDANCE.get(contract.id, "")
    agent = Agent(model, output_type=output_type, instructions=(
        "You are a research-only public-markets work agent. You NEVER propose orders, sizing, "
        "or execution. Gather evidence by calling your tools; never invent facts. "
        "A tool result with coverage_status \"unavailable\" is NOT usable evidence: if the "
        "sources you need are unavailable, return coverage_status \"unavailable\", confidence 0, "
        "assessment \"investigate\", and explain the gap in caveats. In source_labels cite the "
        "exact provider value returned by each tool result you relied on. "
        f"{guidance}"
    ))
    tools = tools or {}
    for tool_name in contract.allowed_tools:
        def noop_boundary() -> str:
            """Return the approved source boundary for this agent."""
            return f"{tool_name} is an approved source boundary."
        fn = tools.get(tool_name, noop_boundary)
        agent.tool_plain(fn, name=tool_name, description=f"Approved evidence source: {tool_name}.")
    return agent


class AgentWorkRepository:
    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    def create_case(self, user_id: int, payload: CaseInput) -> Dict[str, Any]:
        case_id, now = str(uuid.uuid4()), _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO research_cases (id, title, brief, scope, owner_user_id, deadline_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (case_id, payload.title.strip(), payload.brief.strip(), payload.scope.model_dump_json(), user_id, payload.deadline_at.isoformat() if payload.deadline_at else None, now, now),
            )
            conn.commit()
        return self.case(case_id)

    def cases(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT c.*, u.display_name AS owner_name, EXISTS(SELECT 1 FROM agent_runs r WHERE r.case_id = c.id AND r.status IN ('queued', 'running')) AS has_active_runs FROM research_cases c JOIN workspace_users u ON u.id = c.owner_user_id ORDER BY c.updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._case_summary(dict(row)) for row in rows]

    def case(self, case_id: str) -> Dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT c.*, u.display_name AS owner_name FROM research_cases c JOIN workspace_users u ON u.id = c.owner_user_id WHERE c.id = ?", (case_id,)
            ).fetchone()
            if not row:
                raise KeyError("Research case not found.")
            runs = [self._run_view(dict(item)) for item in conn.execute("SELECT * FROM agent_runs WHERE case_id = ? ORDER BY created_at", (case_id,)).fetchall()]
            for run in runs:
                run["events"] = [self._event_view(dict(item)) for item in conn.execute("SELECT * FROM agent_run_events WHERE agent_run_id = ? ORDER BY id", (run["id"],)).fetchall()]
                run["artifacts"] = [_loads(item["payload"], {}) for item in conn.execute("SELECT payload FROM agent_artifacts WHERE agent_run_id = ? ORDER BY created_at", (run["id"],)).fetchall()]
                run["sources"] = [self._source_view(dict(item)) for item in conn.execute("SELECT * FROM agent_sources WHERE agent_run_id = ? ORDER BY created_at, label", (run["id"],)).fetchall()]
            comments = [self._comment_view(dict(item)) for item in conn.execute(
                "SELECT c.*, u.display_name AS author_name FROM case_comments c JOIN workspace_users u ON u.id = c.author_user_id WHERE c.case_id = ? ORDER BY c.created_at", (case_id,)
            ).fetchall()]
        return {**self._case_summary(dict(row)), "runs": runs, "comments": comments}

    def create_run(self, case_id: str, user_id: int, payload: AgentRunInput, idempotency_key: str) -> Dict[str, Any]:
        case = self.case(case_id)
        completed_artifacts = sum(len(run["artifacts"]) for run in case["runs"] if run["status"] == "completed")
        validate_run_input(payload, completed_artifacts)
        run_id, now = str(uuid.uuid4()), _now()
        with self._connection() as conn:
            existing = conn.execute("SELECT * FROM agent_runs WHERE case_id = ? AND idempotency_key = ?", (case_id, idempotency_key)).fetchone()
            if existing:
                return self._run_view(dict(existing))
            conn.execute(
                "INSERT INTO agent_runs (id, case_id, agent_type, agent_version, input_payload, idempotency_key, requested_by_user_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, case_id, payload.agent_type, agent_contract(payload.agent_type).version, payload.model_dump_json(), idempotency_key, user_id, now, now),
            )
            conn.execute("UPDATE research_cases SET updated_at = ? WHERE id = ?", (now, case_id))
            conn.commit()
        self.record_event(run_id, "queued", "queued", {"agent_type": payload.agent_type})
        return self.run(run_id)

    def run(self, run_id: str) -> Dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise KeyError("Agent run not found.")
        return self._run_view(dict(row))

    def record_event(self, run_id: str, stage_name: str, status: str, details: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> None:
        with self._connection() as conn:
            trace = conn.execute("SELECT trace_id FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
            payload = _redact(details or {})
            if trace and trace["trace_id"]:
                payload.setdefault("trace_id", trace["trace_id"])
            conn.execute(
                "INSERT INTO agent_run_events (agent_run_id, stage_name, status, details, error, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, stage_name, status, json.dumps(payload), _redact(error), _now()),
            )
            conn.commit()

    def set_run_status(self, run_id: str, status: str, error: Optional[str] = None, temporal_workflow_id: Optional[str] = None, trace_id: Optional[str] = None) -> None:
        allowed = {"queued", "running", "completed", "failed", "cancelled"}
        if status not in allowed:
            raise ValueError("Invalid agent run status.")
        now = _now()
        with self._connection() as conn:
            row = conn.execute("SELECT case_id, status FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                raise KeyError("Agent run not found.")
            if row["status"] in {"completed", "failed", "cancelled"} and row["status"] != status:
                return
            started = now if status == "running" and row["status"] == "queued" else None
            finished = now if status in {"completed", "failed", "cancelled"} else None
            clean_error = _redact(error)
            conn.execute(
                "UPDATE agent_runs SET status = ?, error = COALESCE(?, error), temporal_workflow_id = COALESCE(?, temporal_workflow_id), trace_id = COALESCE(?, trace_id), started_at = COALESCE(?, started_at), finished_at = COALESCE(?, finished_at), updated_at = ? WHERE id = ?",
                (status, clean_error, temporal_workflow_id, trace_id, started, finished, now, run_id),
            )
            conn.execute("UPDATE research_cases SET updated_at = ? WHERE id = ?", (now, row["case_id"]))
            conn.commit()
        self.record_event(run_id, status, status, error=clean_error)

    def register_dispatch(self, run_id: str) -> Dict[str, Any]:
        """Persist the stable Temporal and trace identifiers before dispatching."""
        workflow_id = f"agent-run-{run_id}"
        with self._connection() as conn:
            row = conn.execute("SELECT temporal_workflow_id, trace_id FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                raise KeyError("Agent run not found.")
            trace_id = row["trace_id"] or self._current_trace_id()
            if row["temporal_workflow_id"] and row["temporal_workflow_id"] != workflow_id:
                raise ValueError("Agent run has a conflicting workflow ID.")
            conn.execute(
                "UPDATE agent_runs SET temporal_workflow_id = ?, trace_id = ?, updated_at = ? WHERE id = ?",
                (workflow_id, trace_id, _now(), run_id),
            )
            conn.commit()
        return {"workflow_id": workflow_id, "trace_id": trace_id}

    def cancel_run(self, case_id: str, run_id: str) -> Dict[str, Any]:
        run = self.run(run_id)
        if run["case_id"] != case_id:
            raise KeyError("Agent run not found in this case.")
        if run["status"] in {"queued", "running"}:
            now = _now()
            with self._connection() as conn:
                conn.execute(
                    "UPDATE agent_runs SET cancellation_requested_at = COALESCE(cancellation_requested_at, ?), updated_at = ? WHERE id = ?",
                    (now, now, run_id),
                )
                conn.commit()
            self.record_event(run_id, "cancellation", "requested")
        return self.run(run_id)

    def save_artifact(self, run_id: str, artifact: EvidenceArtifact, sources: List[SourceRecord], usage_tokens: int = 0, usage_cost_usd: Optional[float] = None) -> bool:
        """Replace evidence atomically; cancelled runs never receive an artifact."""
        artifact_id, now = str(uuid.uuid4()), _now()
        with self._connection() as conn:
            run = conn.execute("SELECT case_id, status, cancellation_requested_at FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
            if not run:
                raise KeyError("Agent run not found.")
            if run["status"] in {"cancelled", "completed", "failed"} or run["cancellation_requested_at"]:
                return False
            clean_artifact = EvidenceArtifact.model_validate(_redact(artifact.model_dump(mode="json")))
            clean_sources = [SourceRecord.model_validate(_redact(source.model_dump(mode="json"))) for source in sources]
            conn.execute(
                "INSERT INTO agent_artifacts (id, agent_run_id, artifact_type, payload, created_at) VALUES (?, ?, 'evidence_brief', ?, ?) ON CONFLICT(agent_run_id, artifact_type) DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
                (artifact_id, run_id, clean_artifact.model_dump_json(), now),
            )
            conn.execute("DELETE FROM agent_sources WHERE agent_run_id = ?", (run_id,))
            for source in clean_sources:
                conn.execute(
                    "INSERT INTO agent_sources (id, agent_run_id, label, source_url, observed_at, coverage_status, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), run_id, source.label, source.source_url, source.observed_at, source.coverage_status, json.dumps({**source.details, "category": source.category}), now),
                )
            conn.execute(
                "UPDATE agent_runs SET usage_tokens = ?, usage_cost_usd = ?, usage_cost_status = ?, updated_at = ? WHERE id = ?",
                (usage_tokens, usage_cost_usd or 0, "known" if usage_cost_usd is not None else "unknown", now, run_id),
            )
            conn.commit()
        self.record_event(run_id, "coverage", coverage_status(sources), {"sources": [{"label": source.label, "status": source.coverage_status} for source in sources], "conflicts": source_conflicts(sources)})
        self.record_event(run_id, "usage", "completed" if usage_cost_usd is not None else "unknown", {"tokens": usage_tokens, "cost_usd": usage_cost_usd, "cost_status": "known" if usage_cost_usd is not None else "unknown"})
        return True

    def is_cancellation_requested(self, run_id: str) -> bool:
        with self._connection() as conn:
            row = conn.execute("SELECT cancellation_requested_at FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise KeyError("Agent run not found.")
        return bool(row["cancellation_requested_at"])

    def add_comment(self, case_id: str, user_id: int, payload: CommentInput) -> Dict[str, Any]:
        comment_id, now = str(uuid.uuid4()), _now()
        with self._connection() as conn:
            if not conn.execute("SELECT 1 FROM research_cases WHERE id = ?", (case_id,)).fetchone():
                raise KeyError("Research case not found.")
            conn.execute("INSERT INTO case_comments (id, case_id, author_user_id, body, created_at) VALUES (?, ?, ?, ?, ?)", (comment_id, case_id, user_id, payload.body.strip(), now))
            conn.execute("UPDATE research_cases SET updated_at = ? WHERE id = ?", (now, case_id))
            conn.commit()
        return {"id": comment_id, "body": payload.body.strip(), "author_user_id": user_id, "created_at": now}

    def finalize_case(self, case_id: str, user_id: int, payload: FinalizeInput) -> Dict[str, Any]:
        now = _now()
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE research_cases SET status = 'finalized', final_assessment = ?, final_note = ?, finalized_by_user_id = ?, finalized_at = ?, updated_at = ? WHERE id = ?",
                (payload.assessment, payload.note.strip(), user_id, now, now, case_id),
            )
            conn.commit()
        if cursor.rowcount == 0:
            raise KeyError("Research case not found.")
        return self.case(case_id)

    def _case_summary(self, row: Dict[str, Any]) -> Dict[str, Any]:
        row["scope"] = _loads(row.pop("scope"), {})
        return row

    def _run_view(self, row: Dict[str, Any]) -> Dict[str, Any]:
        row["input"] = _loads(row.pop("input_payload"), {})
        return row

    @staticmethod
    def _event_view(row: Dict[str, Any]) -> Dict[str, Any]:
        row["details"] = _loads(row.pop("details"), {})
        return row

    @staticmethod
    def _source_view(row: Dict[str, Any]) -> Dict[str, Any]:
        row["details"] = _loads(row.pop("details"), {})
        return row

    @staticmethod
    def _comment_view(row: Dict[str, Any]) -> Dict[str, Any]:
        return row

    class _ConnectionContext:
        def __init__(self, config: Config):
            self.conn = connect(config)

        def __enter__(self):
            return self.conn

        def __exit__(self, *_):
            self.conn.close()

    def _connection(self):
        return self._ConnectionContext(self._config)

    @staticmethod
    def _current_trace_id() -> str:
        try:
            from opentelemetry import trace
            trace_id = trace.get_current_span().get_span_context().trace_id
            if trace_id:
                return f"{trace_id:032x}"
        except ImportError:
            pass
        return uuid.uuid4().hex
