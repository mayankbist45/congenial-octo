"""Auditable, research-only agent work for the shared workspace.

This module deliberately knows nothing about brokers, proposals, or trades.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from src.config import Config, get_config
from src.journal.db import connect

Assessment = Literal["strengthen", "monitor", "investigate", "no_action", "escalate"]
CoverageStatus = Literal["available", "partial", "stale", "unavailable"]
AgentType = Literal["company_deep_dive", "event_impact", "comparative_screen", "portfolio_risk_review", "ic_memo"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


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


AGENT_CATALOG: Dict[str, Dict[str, Any]] = {
    "company_deep_dive": {"label": "Company Deep Dive", "description": "Build a source-backed company view across fundamentals, valuation, catalysts, and risks.", "requires_symbols": True, "timeout_seconds": 900, "token_budget": 18_000},
    "event_impact": {"label": "Event Impact", "description": "Assess how a company, sector, or market event changes the specified scope.", "requires_symbols": False, "timeout_seconds": 600, "token_budget": 12_000},
    "comparative_screen": {"label": "Comparative Screen", "description": "Compare a focused set of securities on normalized evidence and disclosed coverage.", "requires_symbols": True, "minimum_symbols": 2, "timeout_seconds": 900, "token_budget": 16_000},
    "portfolio_risk_review": {"label": "Portfolio Risk Review", "description": "Review portfolio context without changing holdings, exposures, or risk limits.", "requires_portfolio": True, "timeout_seconds": 600, "token_budget": 10_000},
    "ic_memo": {"label": "IC Memo", "description": "Synthesize completed case evidence into a reviewable investment-committee memo.", "requires_completed_case": True, "timeout_seconds": 600, "token_budget": 10_000},
}


def catalog() -> List[Dict[str, Any]]:
    return [{"id": key, **value} for key, value in AGENT_CATALOG.items()]


def validate_run_input(payload: AgentRunInput, completed_runs: int = 0) -> None:
    spec = AGENT_CATALOG[payload.agent_type]
    if spec.get("requires_symbols") and not payload.scope.symbols:
        raise ValueError("This agent requires at least one symbol.")
    if len(payload.scope.symbols) < spec.get("minimum_symbols", 0):
        raise ValueError("This agent requires at least two symbols for comparison.")
    if spec.get("requires_portfolio") and not payload.scope.portfolio_persona_id:
        raise ValueError("Portfolio Risk Review requires a portfolio persona.")
    if spec.get("requires_completed_case") and not completed_runs:
        raise ValueError("IC Memo requires at least one completed agent run in the case.")


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
                "SELECT c.*, u.display_name AS owner_name FROM research_cases c JOIN workspace_users u ON u.id = c.owner_user_id ORDER BY c.updated_at DESC LIMIT ?",
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
                run["sources"] = [self._source_view(dict(item)) for item in conn.execute("SELECT * FROM agent_sources WHERE agent_run_id = ? ORDER BY created_at", (run["id"],)).fetchall()]
            comments = [self._comment_view(dict(item)) for item in conn.execute(
                "SELECT c.*, u.display_name AS author_name FROM case_comments c JOIN workspace_users u ON u.id = c.author_user_id WHERE c.case_id = ? ORDER BY c.created_at", (case_id,)
            ).fetchall()]
        return {**self._case_summary(dict(row)), "runs": runs, "comments": comments}

    def create_run(self, case_id: str, user_id: int, payload: AgentRunInput, idempotency_key: str) -> Dict[str, Any]:
        case = self.case(case_id)
        validate_run_input(payload, sum(run["status"] == "completed" for run in case["runs"]))
        run_id, now = str(uuid.uuid4()), _now()
        with self._connection() as conn:
            existing = conn.execute("SELECT * FROM agent_runs WHERE case_id = ? AND idempotency_key = ?", (case_id, idempotency_key)).fetchone()
            if existing:
                return self._run_view(dict(existing))
            conn.execute(
                "INSERT INTO agent_runs (id, case_id, agent_type, agent_version, input_payload, idempotency_key, requested_by_user_id, created_at, updated_at) VALUES (?, ?, ?, '1', ?, ?, ?, ?, ?)",
                (run_id, case_id, payload.agent_type, payload.model_dump_json(), idempotency_key, user_id, now, now),
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
            conn.execute(
                "INSERT INTO agent_run_events (agent_run_id, stage_name, status, details, error, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, stage_name, status, json.dumps(details or {}), error, _now()),
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
            started = now if status == "running" and row["status"] == "queued" else None
            finished = now if status in {"completed", "failed", "cancelled"} else None
            conn.execute(
                "UPDATE agent_runs SET status = ?, error = COALESCE(?, error), temporal_workflow_id = COALESCE(?, temporal_workflow_id), trace_id = COALESCE(?, trace_id), started_at = COALESCE(?, started_at), finished_at = COALESCE(?, finished_at), updated_at = ? WHERE id = ?",
                (status, error, temporal_workflow_id, trace_id, started, finished, now, run_id),
            )
            conn.execute("UPDATE research_cases SET updated_at = ? WHERE id = ?", (now, row["case_id"]))
            conn.commit()
        self.record_event(run_id, status, status, error=error)

    def cancel_run(self, case_id: str, run_id: str) -> Dict[str, Any]:
        run = self.run(run_id)
        if run["case_id"] != case_id:
            raise KeyError("Agent run not found in this case.")
        if run["status"] in {"queued", "running"}:
            self.set_run_status(run_id, "cancelled")
        return self.run(run_id)

    def save_artifact(self, run_id: str, artifact: EvidenceArtifact, sources: List[SourceRecord]) -> None:
        artifact_id, now = str(uuid.uuid4()), _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO agent_artifacts (id, agent_run_id, artifact_type, payload, created_at) VALUES (?, ?, 'evidence_brief', ?, ?) ON CONFLICT(agent_run_id, artifact_type) DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
                (artifact_id, run_id, artifact.model_dump_json(), now),
            )
            for source in sources:
                conn.execute(
                    "INSERT INTO agent_sources (id, agent_run_id, label, source_url, observed_at, coverage_status, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), run_id, source.label, source.source_url, source.observed_at, source.coverage_status, json.dumps(source.details), now),
                )
            conn.commit()

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
