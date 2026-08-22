"""Learning-review persistence."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List

from src.journal._base import JournalRepositoryBase
from src.journal.db import session


class LearningRepository(JournalRepositoryBase):
    def reviewable_proposals(self) -> List[Dict[str, Any]]:
        with session(self._config) as conn:
            rows = conn.execute(
                """
                SELECT p.*, r.run_date,
                       t.id AS trade_id,
                       t.executed_at AS trade_executed_at,
                       t.price AS trade_price,
                       t.realised_pnl AS trade_realised_pnl
                  FROM proposals p
                  JOIN runs r ON r.id = p.run_id
                  LEFT JOIN trades t ON t.proposal_id = p.id
                 WHERE p.persona_id = ?
                   AND p.status IN ('executed','rejected','expired','failed')
                 ORDER BY p.id
                """,
                (self._persona_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_decision_review(self, review: Dict[str, Any]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        created_at = review.get("created_at") or now
        updated_at = now
        with session(self._config) as conn:
            existing = conn.execute(
                """
                SELECT id FROM decision_reviews
                 WHERE persona_id = ? AND proposal_id = ? AND review_window_days = ?
                """,
                (
                    self._persona_id,
                    review["proposal_id"],
                    review["review_window_days"],
                ),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO decision_reviews (
                    proposal_id, trade_id, persona_id, review_window_days,
                    proposal_date, review_date, symbol, action, status, label,
                    stock_return_pct, benchmark_return_pct, excess_return_pct,
                    max_drawdown_pct, realised_pnl, evidence, recommendation,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(persona_id, proposal_id, review_window_days)
                DO UPDATE SET
                    trade_id = excluded.trade_id,
                    proposal_date = excluded.proposal_date,
                    review_date = excluded.review_date,
                    symbol = excluded.symbol,
                    action = excluded.action,
                    status = excluded.status,
                    label = excluded.label,
                    stock_return_pct = excluded.stock_return_pct,
                    benchmark_return_pct = excluded.benchmark_return_pct,
                    excess_return_pct = excluded.excess_return_pct,
                    max_drawdown_pct = excluded.max_drawdown_pct,
                    realised_pnl = excluded.realised_pnl,
                    evidence = excluded.evidence,
                    recommendation = excluded.recommendation,
                    updated_at = excluded.updated_at
                """,
                (
                    review["proposal_id"],
                    review.get("trade_id"),
                    self._persona_id,
                    review["review_window_days"],
                    review.get("proposal_date"),
                    review["review_date"],
                    review["symbol"],
                    review["action"],
                    review["status"],
                    review["label"],
                    review.get("stock_return_pct"),
                    review.get("benchmark_return_pct"),
                    review.get("excess_return_pct"),
                    review.get("max_drawdown_pct"),
                    review.get("realised_pnl"),
                    json.dumps(review.get("evidence") or {}),
                    review.get("recommendation"),
                    created_at,
                    updated_at,
                ),
            )
            if existing:
                return int(existing["id"])
            row = conn.execute(
                """
                SELECT id FROM decision_reviews
                 WHERE persona_id = ? AND proposal_id = ? AND review_window_days = ?
                """,
                (
                    self._persona_id,
                    review["proposal_id"],
                    review["review_window_days"],
                ),
            ).fetchone()
            return int(row["id"])

    def decision_reviews(self, limit: int = 200) -> List[Dict[str, Any]]:
        with session(self._config) as conn:
            rows = conn.execute(
                """
                SELECT * FROM decision_reviews
                 WHERE persona_id = ?
                 ORDER BY review_date DESC, id DESC
                 LIMIT ?
                """,
                (self._persona_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def clear_learning_lessons(self) -> None:
        with session(self._config) as conn:
            conn.execute(
                "DELETE FROM learning_lessons WHERE persona_id = ?",
                (self._persona_id,),
            )

    def upsert_learning_lesson(self, lesson: Dict[str, Any]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        created_at = lesson.get("created_at") or now
        updated_at = now
        with session(self._config) as conn:
            existing = conn.execute(
                """
                SELECT id FROM learning_lessons
                 WHERE persona_id = ? AND label = ?
                """,
                (self._persona_id, lesson["label"]),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO learning_lessons (
                    persona_id, label, status, evidence_count, symbol_count,
                    review_date_count, avg_excess_return_pct,
                    avg_stock_return_pct, confidence, recommendation, evidence,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(persona_id, label) DO UPDATE SET
                    status = excluded.status,
                    evidence_count = excluded.evidence_count,
                    symbol_count = excluded.symbol_count,
                    review_date_count = excluded.review_date_count,
                    avg_excess_return_pct = excluded.avg_excess_return_pct,
                    avg_stock_return_pct = excluded.avg_stock_return_pct,
                    confidence = excluded.confidence,
                    recommendation = excluded.recommendation,
                    evidence = excluded.evidence,
                    updated_at = excluded.updated_at
                """,
                (
                    self._persona_id,
                    lesson["label"],
                    lesson["status"],
                    lesson["evidence_count"],
                    lesson["symbol_count"],
                    lesson["review_date_count"],
                    lesson.get("avg_excess_return_pct"),
                    lesson.get("avg_stock_return_pct"),
                    lesson.get("confidence"),
                    lesson.get("recommendation"),
                    json.dumps(lesson.get("evidence") or {}),
                    created_at,
                    updated_at,
                ),
            )
            if existing:
                return int(existing["id"])
            row = conn.execute(
                """
                SELECT id FROM learning_lessons
                 WHERE persona_id = ? AND label = ?
                """,
                (self._persona_id, lesson["label"]),
            ).fetchone()
            return int(row["id"])

    def learning_lessons(self) -> List[Dict[str, Any]]:
        with session(self._config) as conn:
            rows = conn.execute(
                """
                SELECT * FROM learning_lessons
                 WHERE persona_id = ?
                 ORDER BY CASE WHEN status = 'actionable' THEN 0 ELSE 1 END,
                          evidence_count DESC, label
                """,
                (self._persona_id,),
            ).fetchall()
            return [dict(r) for r in rows]
