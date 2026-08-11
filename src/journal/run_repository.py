"""Run and candidate-pack persistence."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from src.journal._base import JournalRepositoryBase
from src.journal.db import session
from src.logging_setup import get_logger

logger = get_logger(__name__)


class RunRepository(JournalRepositoryBase):
    def start_run(
        self,
        run_date: date,
        is_deep_review: bool,
        portfolio_value: float,
        cash: float,
        command: str = "analyse",
        trigger_source: str = "manual",
        candidate_pack_date: Optional[date] = None,
        candidate_pack_age_days: Optional[float] = None,
        market_regime: Optional[str] = None,
        market_regime_score: Optional[float] = None,
    ) -> int:
        with session(self._config) as conn:
            return conn.insert_returning_id(
                """
                INSERT INTO runs (persona_id, run_date, started_at, is_deep_review,
                                  portfolio_value, cash, status, command,
                                  trigger_source, candidate_pack_date,
                                  candidate_pack_age_days, market_regime,
                                  market_regime_score)
                VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._persona_id,
                    run_date.isoformat(),
                    datetime.now().isoformat(timespec="seconds"),
                    int(is_deep_review),
                    portfolio_value,
                    cash,
                    command,
                    trigger_source,
                    candidate_pack_date.isoformat() if candidate_pack_date else None,
                    candidate_pack_age_days,
                    market_regime,
                    market_regime_score,
                ),
            )

    def finish_run(
        self,
        run_id: int,
        memo: str,
        llm_used: bool,
        turnover_used_pct: float,
        status: str = "complete",
        stage_status: Optional[Dict[str, Any]] = None,
    ) -> None:
        with session(self._config) as conn:
            conn.execute(
                """
                UPDATE runs
                   SET finished_at = ?, memo = ?, llm_used = ?,
                       turnover_used_pct = ?, status = ?, memo_length = ?,
                       stage_status = ?
                 WHERE id = ?
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    memo,
                    int(llm_used),
                    turnover_used_pct,
                    status,
                    len(memo or ""),
                    json.dumps(stage_status or {}),
                    run_id,
                ),
            )

    def latest_run(self) -> Optional[Dict[str, Any]]:
        with session(self._config) as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE persona_id = ? ORDER BY id DESC LIMIT 1",
                (self._persona_id,),
            ).fetchone()
            return dict(row) if row else None

    def recent_runs(self, limit: int = 30) -> List[Dict[str, Any]]:
        with session(self._config) as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE persona_id = ? ORDER BY id DESC LIMIT ?",
                (self._persona_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def record_run_stage(
        self,
        run_id: int,
        stage_name: str,
        status: str,
        duration_ms: Optional[float] = None,
        details: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        with session(self._config) as conn:
            conn.execute(
                """
                INSERT INTO run_stage_events (
                    run_id, persona_id, stage_name, status, duration_ms,
                    details, error, created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    self._persona_id,
                    stage_name,
                    status,
                    duration_ms,
                    json.dumps(details or {}),
                    error,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )

    def run_stage_events(self, run_id: int) -> List[Dict[str, Any]]:
        with session(self._config) as conn:
            rows = conn.execute(
                """
                SELECT * FROM run_stage_events
                 WHERE run_id = ? AND persona_id = ?
                 ORDER BY id
                """,
                (run_id, self._persona_id),
            ).fetchall()
            return [dict(r) for r in rows]

    def save_candidate_pack(
        self,
        research_date: date,
        candidates: List[Any],
        source_run_id: Optional[int] = None,
    ) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with session(self._config) as conn:
            existing = conn.execute(
                "SELECT id FROM candidate_packs WHERE persona_id = ? AND research_date = ?",
                (self._persona_id, research_date.isoformat()),
            ).fetchone()
            pack_payload = []
            for score in candidates:
                pack_payload.append(
                    {
                        "symbol": score.symbol,
                        "rank": score.rank,
                        "composite": score.composite,
                        "percentile": score.percentile,
                        "passes_gates": score.passes_gates,
                        "gate_failures": list(score.gate_failures),
                        "sector_model": score.sector_model,
                        "supported_metrics": list(score.supported_metrics),
                        "unsupported_metrics": list(score.unsupported_metrics),
                        "metric_coverage_pct": score.metric_coverage_pct,
                        "valuation_summary": dict(score.valuation_summary),
                        "macro_exposures": dict(score.macro_exposures),
                        "suppressed_reasons": list(
                            getattr(score, "suppressed_reasons", [])
                        ),
                    }
                )
            if existing:
                pack_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE candidate_packs
                       SET created_at = ?, source_run_id = ?, candidate_count = ?, payload = ?
                     WHERE id = ?
                    """,
                    (
                        now,
                        source_run_id,
                        len(pack_payload),
                        json.dumps(pack_payload),
                        pack_id,
                    ),
                )
                conn.execute(
                    "DELETE FROM candidate_pack_members WHERE pack_id = ?",
                    (pack_id,),
                )
            else:
                pack_id = conn.insert_returning_id(
                    """
                    INSERT INTO candidate_packs (
                        persona_id, research_date, created_at, source_run_id,
                        candidate_count, payload
                    ) VALUES (?,?,?,?,?,?)
                    """,
                    (
                        self._persona_id,
                        research_date.isoformat(),
                        now,
                        source_run_id,
                        len(pack_payload),
                        json.dumps(pack_payload),
                    ),
                )
            for item in pack_payload:
                conn.execute(
                    """
                    INSERT INTO candidate_pack_members (
                        pack_id, persona_id, research_date, symbol, rank, composite, payload
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        pack_id,
                        self._persona_id,
                        research_date.isoformat(),
                        item["symbol"],
                        item["rank"],
                        item["composite"],
                        json.dumps(item),
                    ),
                )
        return pack_id

    def latest_candidate_pack(self) -> Optional[Dict[str, Any]]:
        with session(self._config) as conn:
            row = conn.execute(
                """
                SELECT * FROM candidate_packs
                 WHERE persona_id = ?
                 ORDER BY research_date DESC, id DESC
                 LIMIT 1
                """,
                (self._persona_id,),
            ).fetchone()
            return dict(row) if row else None

    def candidate_pack_members(self, pack_id: int) -> List[Dict[str, Any]]:
        with session(self._config) as conn:
            rows = conn.execute(
                """
                SELECT * FROM candidate_pack_members
                 WHERE pack_id = ?
                 ORDER BY rank ASC, composite DESC, symbol ASC
                """,
                (pack_id,),
            ).fetchall()
            return [dict(r) for r in rows]
