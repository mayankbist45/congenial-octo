"""Proposal, trade, and position-meta persistence."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from src.journal._base import JournalRepositoryBase
from src.journal._shared import iso_date
from src.journal.db import session
from src.logging_setup import get_logger
from src.models import TradeProposal

logger = get_logger(__name__)


class ProposalRepository(JournalRepositoryBase):
    def save_proposals(self, run_id: int, proposals: List[TradeProposal]) -> List[int]:
        ids: List[int] = []
        now = datetime.now().isoformat(timespec="seconds")
        with session(self._config) as conn:
            for proposal in proposals:
                proposal_id = conn.insert_returning_id(
                    """
                    INSERT INTO proposals (
                        run_id, persona_id, created_at, symbol, company_name,
                        sector, action, quantity, reference_price, limit_price,
                        estimated_value, rationale, thesis, target_price,
                        trigger_types, confidence, warnings, source, status
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')
                    """,
                    (
                        run_id,
                        self._persona_id,
                        now,
                        proposal.symbol,
                        proposal.company_name,
                        proposal.sector,
                        proposal.action.value,
                        proposal.quantity,
                        proposal.reference_price,
                        proposal.limit_price,
                        proposal.estimated_value,
                        proposal.rationale,
                        proposal.thesis,
                        proposal.target_price,
                        json.dumps(proposal.trigger_types),
                        proposal.confidence,
                        json.dumps(proposal.warnings),
                        proposal.source,
                    ),
                )
                ids.append(proposal_id)
        if ids:
            logger.info("Journalled %d proposal(s) for run %d", len(ids), run_id)
        return ids

    def save_rejected(self, run_id: int, rejected: List[Tuple[TradeProposal, str]]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with session(self._config) as conn:
            for proposal, reason in rejected:
                conn.execute(
                    """
                    INSERT INTO proposals (
                        run_id, persona_id, created_at, symbol, company_name,
                        sector, action, quantity, reference_price, estimated_value,
                        rationale, trigger_types, confidence, warnings, source,
                        status, decided_at, decided_by, rejection_reason
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                              'rejected', ?, 'risk_engine', ?)
                    """,
                    (
                        run_id,
                        self._persona_id,
                        now,
                        proposal.symbol,
                        proposal.company_name,
                        proposal.sector,
                        proposal.action.value,
                        proposal.quantity,
                        proposal.reference_price,
                        proposal.estimated_value,
                        proposal.rationale,
                        json.dumps(proposal.trigger_types),
                        proposal.confidence,
                        json.dumps(proposal.warnings),
                        proposal.source,
                        now,
                        reason,
                    ),
                )
        if rejected:
            logger.info("Journalled %d risk-rejected proposal(s)", len(rejected))

    def pending_proposals(self) -> List[Dict[str, Any]]:
        with session(self._config) as conn:
            rows = conn.execute(
                """
                SELECT p.*, r.run_date
                  FROM proposals p JOIN runs r ON r.id = p.run_id
                 WHERE p.status = 'pending' AND p.persona_id = ?
                 ORDER BY p.id
                """,
                (self._persona_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def approved_proposals(self) -> List[Dict[str, Any]]:
        with session(self._config) as conn:
            rows = conn.execute(
                """
                SELECT p.*, r.run_date
                  FROM proposals p JOIN runs r ON r.id = p.run_id
                 WHERE p.status = 'approved' AND p.persona_id = ?
                 ORDER BY p.id
                """,
                (self._persona_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_proposal(self, proposal_id: int) -> Optional[Dict[str, Any]]:
        with session(self._config) as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            return dict(row) if row else None

    def proposals_for_run(self, run_id: int) -> List[Dict[str, Any]]:
        with session(self._config) as conn:
            rows = conn.execute(
                "SELECT * FROM proposals WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def set_proposal_status(
        self,
        proposal_id: int,
        status: str,
        decided_by: str = "user",
        rejection_reason: Optional[str] = None,
    ) -> bool:
        with session(self._config) as conn:
            cursor = conn.execute(
                """
                UPDATE proposals
                   SET status = ?, decided_at = ?, decided_by = ?,
                       rejection_reason = COALESCE(?, rejection_reason)
                 WHERE id = ?
                """,
                (
                    status,
                    datetime.now().isoformat(timespec="seconds"),
                    decided_by,
                    rejection_reason,
                    proposal_id,
                ),
            )
            return cursor.rowcount > 0

    def record_trade(
        self,
        symbol: str,
        action: str,
        quantity: int,
        price: float,
        status: str,
        proposal_id: Optional[int] = None,
        broker_order_id: Optional[str] = None,
        error: Optional[str] = None,
        dry_run: bool = False,
        realised_pnl: Optional[float] = None,
        executed_at: Optional[datetime] = None,
    ) -> int:
        with session(self._config) as conn:
            return conn.insert_returning_id(
                """
                INSERT INTO trades (
                    proposal_id, persona_id, executed_at, symbol, action,
                    quantity, price, value, broker_order_id, status, error,
                    dry_run, realised_pnl
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    proposal_id,
                    self._persona_id,
                    (executed_at or datetime.now()).isoformat(timespec="seconds"),
                    symbol.upper(),
                    action.upper(),
                    quantity,
                    price,
                    quantity * price,
                    broker_order_id,
                    status,
                    error,
                    int(dry_run),
                    realised_pnl,
                ),
            )

    def trades(self, limit: int = 200) -> List[Dict[str, Any]]:
        with session(self._config) as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE persona_id = ? ORDER BY id DESC LIMIT ?",
                (self._persona_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def trades_for_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        with session(self._config) as conn:
            rows = conn.execute(
                """
                SELECT * FROM trades
                 WHERE persona_id = ? AND symbol = ?
                   AND status IN ('filled','submitted','dry_run')
                 ORDER BY executed_at
                """,
                (self._persona_id, symbol.upper()),
            ).fetchall()
            return [dict(r) for r in rows]

    def reconstruct_positions(self) -> List[Dict[str, Any]]:
        with session(self._config) as conn:
            rows = conn.execute(
                """
                SELECT symbol, action, quantity, price, executed_at
                  FROM trades
                 WHERE persona_id = ?
                   AND status IN ('filled','submitted','dry_run')
                 ORDER BY executed_at, id
                """,
                (self._persona_id,),
            ).fetchall()

        books: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            symbol = row["symbol"]
            book = books.setdefault(
                symbol,
                {"symbol": symbol, "quantity": 0, "cost": 0.0, "first_buy": None},
            )
            quantity = int(row["quantity"])
            price = float(row["price"])

            if row["action"] in ("BUY", "ADD"):
                book["quantity"] += quantity
                book["cost"] += quantity * price
                if book["first_buy"] is None:
                    book["first_buy"] = row["executed_at"]
            else:
                if book["quantity"] > 0:
                    average = book["cost"] / book["quantity"]
                    sold = min(quantity, book["quantity"])
                    book["quantity"] -= sold
                    book["cost"] -= sold * average
                if book["quantity"] <= 0:
                    book["quantity"] = 0
                    book["cost"] = 0.0
                    book["first_buy"] = None

        return [
            {
                "symbol": book["symbol"],
                "quantity": book["quantity"],
                "avg_cost": book["cost"] / book["quantity"] if book["quantity"] else 0.0,
                "entry_date": book["first_buy"],
            }
            for book in books.values()
            if book["quantity"] > 0
        ]

    def net_cash_deployed(self) -> float:
        with session(self._config) as conn:
            row = conn.execute(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN action IN ('BUY','ADD')
                                    THEN value ELSE 0 END), 0) AS bought,
                  COALESCE(SUM(CASE WHEN action IN ('SELL','TRIM')
                                    THEN value ELSE 0 END), 0) AS sold
                  FROM trades
                 WHERE persona_id = ?
                   AND status IN ('filled','submitted','dry_run')
                """,
                (self._persona_id,),
            ).fetchone()
        return float(row["bought"]) - float(row["sold"])

    def total_realised_pnl(self) -> float:
        with session(self._config) as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(realised_pnl), 0) AS total FROM trades
                 WHERE persona_id = ?
                   AND status IN ('filled','submitted','dry_run')
                """,
                (self._persona_id,),
            ).fetchone()
        return float(row["total"])

    def traded_value_between(self, start: date, end: date) -> Tuple[float, float]:
        with session(self._config) as conn:
            row = conn.execute(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN action IN ('BUY','ADD')
                                    THEN value ELSE 0 END), 0) AS purchases,
                  COALESCE(SUM(CASE WHEN action IN ('SELL','TRIM')
                                    THEN value ELSE 0 END), 0) AS sales
                  FROM trades
                 WHERE persona_id = ?
                   AND status IN ('filled','submitted','dry_run')
                   AND substr(executed_at, 1, 10) BETWEEN ? AND ?
                """,
                (self._persona_id, start.isoformat(), end.isoformat()),
            ).fetchone()
        return float(row["purchases"]), float(row["sales"])

    def upsert_position_meta(
        self,
        symbol: str,
        entry_date: Optional[date] = None,
        entry_price: Optional[float] = None,
        entry_pe: Optional[float] = None,
        peak_price: Optional[float] = None,
        thesis: Optional[str] = None,
        target_price: Optional[float] = None,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with session(self._config) as conn:
            conn.execute(
                """
                INSERT INTO position_meta (
                    persona_id, symbol, entry_date, entry_price, entry_pe,
                    peak_price, thesis, target_price, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(persona_id, symbol) DO UPDATE SET
                    entry_date   = COALESCE(excluded.entry_date, position_meta.entry_date),
                    entry_price  = COALESCE(excluded.entry_price, position_meta.entry_price),
                    entry_pe     = COALESCE(excluded.entry_pe, position_meta.entry_pe),
                    peak_price   = GREATEST(COALESCE(excluded.peak_price, 0),
                                            COALESCE(position_meta.peak_price, 0)),
                    thesis       = COALESCE(excluded.thesis, position_meta.thesis),
                    target_price = COALESCE(excluded.target_price, position_meta.target_price),
                    updated_at   = excluded.updated_at
                """,
                (
                    self._persona_id,
                    symbol.upper(),
                    iso_date(entry_date),
                    entry_price,
                    entry_pe,
                    peak_price,
                    thesis,
                    target_price,
                    now,
                ),
            )

    def position_meta(self) -> Dict[str, Dict[str, Any]]:
        with session(self._config) as conn:
            rows = conn.execute(
                "SELECT * FROM position_meta WHERE persona_id = ?",
                (self._persona_id,),
            ).fetchall()
        return {row["symbol"]: dict(row) for row in rows}

    def clear_position_meta(self, symbol: str) -> None:
        with session(self._config) as conn:
            conn.execute(
                "DELETE FROM position_meta WHERE persona_id = ? AND symbol = ?",
                (self._persona_id, symbol.upper()),
            )
