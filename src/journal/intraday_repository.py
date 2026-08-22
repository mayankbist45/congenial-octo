"""Intraday-specific journal persistence."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.journal._base import JournalRepositoryBase
from src.journal._shared import decode_meta_decision
from src.journal.db import session


class IntradayRepository(JournalRepositoryBase):
    def insert_meta_decision(self, row: Dict[str, Any]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with session(self._config) as conn:
            return conn.insert_returning_id(
                """
                INSERT INTO meta_decisions (
                    decision_id, persona_id, symbol, decision_ts, strategy, side,
                    gate_decision, gate_method, model_version, model_score,
                    features, rationale, news_refs, gate_reason,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["decision_id"],
                    self._persona_id,
                    str(row["symbol"]).upper(),
                    row["decision_ts"],
                    row.get("strategy"),
                    row.get("side"),
                    row["gate_decision"],
                    row.get("gate_method"),
                    row.get("model_version"),
                    row.get("model_score"),
                    json.dumps(row.get("features") or {}),
                    row.get("rationale"),
                    json.dumps(row.get("news_refs") or []),
                    row.get("gate_reason"),
                    now,
                    now,
                ),
            )

    def update_meta_decision_outcome(
        self,
        decision_id: str,
        *,
        outcome_label: Optional[int] = None,
        meta_label: Optional[int] = None,
        net_pnl: Optional[float] = None,
        exit_reason: Optional[str] = None,
    ) -> bool:
        now = datetime.now().isoformat(timespec="seconds")
        with session(self._config) as conn:
            cursor = conn.execute(
                """
                UPDATE meta_decisions
                   SET outcome_label = ?,
                       meta_label = ?,
                       net_pnl = ?,
                       exit_reason = ?,
                       labeled_at = ?,
                       updated_at = ?
                 WHERE persona_id = ? AND decision_id = ?
                """,
                (
                    outcome_label,
                    meta_label,
                    net_pnl,
                    exit_reason,
                    now,
                    now,
                    self._persona_id,
                    decision_id,
                ),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def meta_decisions(
        self,
        *,
        limit: int = 500,
        unlabeled_only: bool = False,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses = ["persona_id = ?"]
        params: List[Any] = [self._persona_id]
        if unlabeled_only:
            clauses.append("labeled_at IS NULL")
        if since:
            clauses.append("decision_ts >= ?")
            params.append(since)
        where = " AND ".join(clauses)
        params.append(limit)
        with session(self._config) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM meta_decisions
                 WHERE {where}
                 ORDER BY decision_ts DESC, id DESC
                 LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            return [decode_meta_decision(dict(r)) for r in rows]
