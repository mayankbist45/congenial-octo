"""Trade journal persistence.

Every decision the fund makes, taken or rejected, is written here with its
rationale. That record is what makes the fund reviewable: months later it must
be possible to ask why a position was opened and what the manager believed at
the time.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from src.config import Config, get_config
from src.journal.db import session
from src.logging_setup import get_logger
from src.models import (
    Action,
    DailyDecision,
    NewsDigest,
    PortfolioRiskProfile,
    PortfolioState,
    TradeProposal,
)

logger = get_logger(__name__)


def _iso(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None


class JournalRepository:
    """Reads and writes the fund's records."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------
    def start_run(
        self,
        run_date: date,
        is_deep_review: bool,
        portfolio_value: float,
        cash: float,
    ) -> int:
        with session(self._config.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO runs (run_date, started_at, is_deep_review,
                                  portfolio_value, cash, status)
                VALUES (?, ?, ?, ?, ?, 'running')
                """,
                (
                    run_date.isoformat(),
                    datetime.now().isoformat(timespec="seconds"),
                    int(is_deep_review),
                    portfolio_value,
                    cash,
                ),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        memo: str,
        llm_used: bool,
        turnover_used_pct: float,
        status: str = "complete",
    ) -> None:
        with session(self._config.db_path) as conn:
            conn.execute(
                """
                UPDATE runs
                   SET finished_at = ?, memo = ?, llm_used = ?,
                       turnover_used_pct = ?, status = ?
                 WHERE id = ?
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    memo,
                    int(llm_used),
                    turnover_used_pct,
                    status,
                    run_id,
                ),
            )

    def latest_run(self) -> Optional[Dict[str, Any]]:
        with session(self._config.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def recent_runs(self, limit: int = 30) -> List[Dict[str, Any]]:
        with session(self._config.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Proposals
    # ------------------------------------------------------------------
    def save_proposals(
        self, run_id: int, proposals: List[TradeProposal]
    ) -> List[int]:
        ids: List[int] = []
        now = datetime.now().isoformat(timespec="seconds")
        with session(self._config.db_path) as conn:
            for proposal in proposals:
                cursor = conn.execute(
                    """
                    INSERT INTO proposals (
                        run_id, created_at, symbol, company_name, sector, action,
                        quantity, reference_price, limit_price, estimated_value,
                        rationale, thesis, target_price, trigger_types,
                        confidence, warnings, source, status
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')
                    """,
                    (
                        run_id,
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
                ids.append(int(cursor.lastrowid))
        if ids:
            logger.info("Journalled %d proposal(s) for run %d", len(ids), run_id)
        return ids

    def save_rejected(
        self, run_id: int, rejected: List[Tuple[TradeProposal, str]]
    ) -> None:
        """Record proposals the risk engine blocked, with the reason.

        Rejections are as informative as approvals when reviewing the fund: they
        show where the decision layer keeps pushing against its limits.
        """
        now = datetime.now().isoformat(timespec="seconds")
        with session(self._config.db_path) as conn:
            for proposal, reason in rejected:
                conn.execute(
                    """
                    INSERT INTO proposals (
                        run_id, created_at, symbol, company_name, sector, action,
                        quantity, reference_price, estimated_value, rationale,
                        trigger_types, confidence, warnings, source, status,
                        decided_at, decided_by, rejection_reason
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                              'rejected', ?, 'risk_engine', ?)
                    """,
                    (
                        run_id,
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
        with session(self._config.db_path) as conn:
            rows = conn.execute(
                """
                SELECT p.*, r.run_date
                  FROM proposals p JOIN runs r ON r.id = p.run_id
                 WHERE p.status = 'pending'
                 ORDER BY p.id
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def approved_proposals(self) -> List[Dict[str, Any]]:
        """Proposals the human approved that have not yet been sent to the broker."""
        with session(self._config.db_path) as conn:
            rows = conn.execute(
                """
                SELECT p.*, r.run_date
                  FROM proposals p JOIN runs r ON r.id = p.run_id
                 WHERE p.status = 'approved'
                 ORDER BY p.id
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_proposal(self, proposal_id: int) -> Optional[Dict[str, Any]]:
        with session(self._config.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            return dict(row) if row else None

    def proposals_for_run(self, run_id: int) -> List[Dict[str, Any]]:
        with session(self._config.db_path) as conn:
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
        with session(self._config.db_path) as conn:
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

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------
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
        with session(self._config.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO trades (
                    proposal_id, executed_at, symbol, action, quantity, price,
                    value, broker_order_id, status, error, dry_run, realised_pnl
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    proposal_id,
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
            return int(cursor.lastrowid)

    def trades(self, limit: int = 200) -> List[Dict[str, Any]]:
        with session(self._config.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def trades_for_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        with session(self._config.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM trades
                 WHERE symbol = ? AND status IN ('filled','submitted','dry_run')
                 ORDER BY executed_at
                """,
                (symbol.upper(),),
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Position reconstruction (used by the simulated broker)
    # ------------------------------------------------------------------
    def reconstruct_positions(self) -> List[Dict[str, Any]]:
        """Rebuild holdings and average cost from the fill history."""
        with session(self._config.db_path) as conn:
            rows = conn.execute(
                """
                SELECT symbol, action, quantity, price, executed_at
                  FROM trades
                 WHERE status IN ('filled','submitted','dry_run')
                 ORDER BY executed_at, id
                """
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
                # Sells reduce cost at the running average, so the remaining
                # average cost is unchanged by a partial sale.
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
        """Net rupees moved into positions (buys minus sell proceeds)."""
        with session(self._config.db_path) as conn:
            row = conn.execute(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN action IN ('BUY','ADD')
                                    THEN value ELSE 0 END), 0) AS bought,
                  COALESCE(SUM(CASE WHEN action IN ('SELL','TRIM')
                                    THEN value ELSE 0 END), 0) AS sold
                  FROM trades
                 WHERE status IN ('filled','submitted','dry_run')
                """
            ).fetchone()
        return float(row["bought"]) - float(row["sold"])

    def total_realised_pnl(self) -> float:
        with session(self._config.db_path) as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(realised_pnl), 0) AS total FROM trades
                 WHERE status IN ('filled','submitted','dry_run')
                """
            ).fetchone()
        return float(row["total"])

    def traded_value_between(
        self, start: date, end: date
    ) -> Tuple[float, float]:
        """Purchase and sale value in a window, for turnover measurement."""
        with session(self._config.db_path) as conn:
            row = conn.execute(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN action IN ('BUY','ADD')
                                    THEN value ELSE 0 END), 0) AS purchases,
                  COALESCE(SUM(CASE WHEN action IN ('SELL','TRIM')
                                    THEN value ELSE 0 END), 0) AS sales
                  FROM trades
                 WHERE status IN ('filled','submitted','dry_run')
                   AND date(executed_at) BETWEEN ? AND ?
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchone()
        return float(row["purchases"]), float(row["sales"])

    def average_net_assets_between(self, start: date, end: date) -> float:
        with session(self._config.db_path) as conn:
            row = conn.execute(
                """
                SELECT AVG(total_value) AS average FROM nav_history
                 WHERE run_date BETWEEN ? AND ?
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchone()
        average = row["average"] if row else None
        return float(average) if average else 0.0

    # ------------------------------------------------------------------
    # Position metadata
    # ------------------------------------------------------------------
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
        """Store the context a sell trigger needs later.

        Entry P/E, entry date and the written thesis cannot be recovered from
        broker data, so they are captured when the position is opened.
        """
        now = datetime.now().isoformat(timespec="seconds")
        with session(self._config.db_path) as conn:
            conn.execute(
                """
                INSERT INTO position_meta (
                    symbol, entry_date, entry_price, entry_pe, peak_price,
                    thesis, target_price, updated_at
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                    entry_date   = COALESCE(excluded.entry_date, position_meta.entry_date),
                    entry_price  = COALESCE(excluded.entry_price, position_meta.entry_price),
                    entry_pe     = COALESCE(excluded.entry_pe, position_meta.entry_pe),
                    -- The peak only ever ratchets upward.
                    peak_price   = MAX(COALESCE(excluded.peak_price, 0),
                                       COALESCE(position_meta.peak_price, 0)),
                    thesis       = COALESCE(excluded.thesis, position_meta.thesis),
                    target_price = COALESCE(excluded.target_price, position_meta.target_price),
                    updated_at   = excluded.updated_at
                """,
                (
                    symbol.upper(),
                    _iso(entry_date),
                    entry_price,
                    entry_pe,
                    peak_price,
                    thesis,
                    target_price,
                    now,
                ),
            )

    def position_meta(self) -> Dict[str, Dict[str, Any]]:
        with session(self._config.db_path) as conn:
            rows = conn.execute("SELECT * FROM position_meta").fetchall()
        return {row["symbol"]: dict(row) for row in rows}

    def clear_position_meta(self, symbol: str) -> None:
        with session(self._config.db_path) as conn:
            conn.execute("DELETE FROM position_meta WHERE symbol = ?", (symbol.upper(),))

    # ------------------------------------------------------------------
    # NAV, risk and digest history
    # ------------------------------------------------------------------
    def record_nav(
        self,
        run_date: date,
        portfolio: PortfolioState,
        benchmark_close: Optional[float] = None,
        daily_return_pct: Optional[float] = None,
        benchmark_return_pct: Optional[float] = None,
    ) -> None:
        unrealised = sum(p.unrealised_pnl for p in portfolio.positions)
        with session(self._config.db_path) as conn:
            conn.execute(
                """
                INSERT INTO nav_history (
                    run_date, total_value, invested_value, cash, position_count,
                    benchmark_close, daily_return_pct, benchmark_return_pct,
                    realised_pnl, unrealised_pnl
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_date) DO UPDATE SET
                    total_value = excluded.total_value,
                    invested_value = excluded.invested_value,
                    cash = excluded.cash,
                    position_count = excluded.position_count,
                    benchmark_close = excluded.benchmark_close,
                    daily_return_pct = excluded.daily_return_pct,
                    benchmark_return_pct = excluded.benchmark_return_pct,
                    realised_pnl = excluded.realised_pnl,
                    unrealised_pnl = excluded.unrealised_pnl
                """,
                (
                    run_date.isoformat(),
                    portfolio.total_value,
                    portfolio.invested_value,
                    portfolio.cash,
                    len(portfolio.positions),
                    benchmark_close,
                    daily_return_pct,
                    benchmark_return_pct,
                    portfolio.realised_pnl_to_date,
                    unrealised,
                ),
            )

    def nav_history(self) -> List[Dict[str, Any]]:
        with session(self._config.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM nav_history ORDER BY run_date"
            ).fetchall()
            return [dict(r) for r in rows]

    def record_position_snapshot(
        self,
        run_date: date,
        portfolio: PortfolioState,
        scores: Optional[Dict[str, Any]] = None,
        risk_profile: Optional[PortfolioRiskProfile] = None,
    ) -> None:
        """Freeze the holdings and their metrics for this run.

        Called twice a day: once pre-market with the full analytics, and again
        after the close to re-mark at closing prices. The second call carries no
        scores or risk profile, so any already recorded for the day are retained
        rather than overwritten with nulls.
        """
        scores = scores or {}
        weights = portfolio.weights()
        per_position = risk_profile.per_position if risk_profile else {}

        with session(self._config.db_path) as conn:
            existing = {
                row["symbol"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM position_snapshots WHERE run_date = ?",
                    (run_date.isoformat(),),
                ).fetchall()
            }
            conn.execute(
                "DELETE FROM position_snapshots WHERE run_date = ?",
                (run_date.isoformat(),),
            )
            for position in portfolio.positions:
                score = scores.get(position.symbol)
                metrics = per_position.get(position.symbol)
                prior = existing.get(position.symbol, {})

                def carried(new_value, column: str, _prior=prior):
                    return new_value if new_value is not None else _prior.get(column)

                conn.execute(
                    """
                    INSERT INTO position_snapshots (
                        run_date, symbol, company_name, sector, quantity,
                        avg_cost, last_price, market_value, weight_pct,
                        return_pct, unrealised_pnl, holding_days,
                        composite_score, beta, idiosyncratic_vol,
                        risk_contribution_pct, pct_of_adv, thesis
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_date.isoformat(),
                        position.symbol,
                        position.company_name,
                        position.sector,
                        position.quantity,
                        position.avg_cost,
                        position.last_price,
                        position.market_value,
                        weights.get(position.symbol),
                        position.return_pct,
                        position.unrealised_pnl,
                        position.holding_days(run_date),
                        carried(getattr(score, "composite", None), "composite_score"),
                        carried(getattr(metrics, "beta", None), "beta"),
                        carried(
                            getattr(metrics, "idiosyncratic_vol", None),
                            "idiosyncratic_vol",
                        ),
                        carried(
                            getattr(metrics, "risk_contribution_pct", None),
                            "risk_contribution_pct",
                        ),
                        carried(getattr(metrics, "pct_of_adv", None), "pct_of_adv"),
                        carried(position.thesis or None, "thesis"),
                    ),
                )

    def latest_position_snapshot(self) -> List[Dict[str, Any]]:
        with session(self._config.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM position_snapshots
                 WHERE run_date = (SELECT MAX(run_date) FROM position_snapshots)
                 ORDER BY market_value DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def record_risk_profile(
        self, run_date: date, profile: PortfolioRiskProfile
    ) -> None:
        payload = asdict(profile)
        payload["as_of"] = _iso(profile.as_of)
        with session(self._config.db_path) as conn:
            conn.execute(
                """
                INSERT INTO risk_history (
                    run_date, portfolio_beta, portfolio_vol, idio_share_pct,
                    tracking_error_pct, active_share_pct, concentration_top5_pct,
                    avg_pairwise_correlation, payload
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_date) DO UPDATE SET
                    portfolio_beta = excluded.portfolio_beta,
                    portfolio_vol = excluded.portfolio_vol,
                    idio_share_pct = excluded.idio_share_pct,
                    tracking_error_pct = excluded.tracking_error_pct,
                    active_share_pct = excluded.active_share_pct,
                    concentration_top5_pct = excluded.concentration_top5_pct,
                    avg_pairwise_correlation = excluded.avg_pairwise_correlation,
                    payload = excluded.payload
                """,
                (
                    run_date.isoformat(),
                    profile.portfolio_beta,
                    profile.portfolio_vol_annualised,
                    profile.active_selection_share_pct,
                    profile.tracking_error_pct,
                    profile.active_share_pct,
                    profile.concentration_top5_pct,
                    profile.avg_pairwise_correlation,
                    json.dumps(payload, default=str),
                ),
            )

    def latest_risk_profile(self) -> Optional[Dict[str, Any]]:
        with session(self._config.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM risk_history ORDER BY run_date DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def record_digest(self, digest: NewsDigest) -> None:
        with session(self._config.db_path) as conn:
            conn.execute(
                """
                INSERT INTO news_digests (
                    run_date, macro_summary, market_summary, holdings_notes,
                    watchlist_notes, flagged_symbols, article_count,
                    high_impact_count, generated_by_llm
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_date) DO UPDATE SET
                    macro_summary = excluded.macro_summary,
                    market_summary = excluded.market_summary,
                    holdings_notes = excluded.holdings_notes,
                    watchlist_notes = excluded.watchlist_notes,
                    flagged_symbols = excluded.flagged_symbols,
                    article_count = excluded.article_count,
                    high_impact_count = excluded.high_impact_count,
                    generated_by_llm = excluded.generated_by_llm
                """,
                (
                    digest.as_of.isoformat(),
                    digest.macro_summary,
                    digest.market_summary,
                    json.dumps(digest.holdings_notes),
                    json.dumps(digest.watchlist_notes),
                    json.dumps(digest.flagged_symbols),
                    digest.article_count,
                    digest.high_impact_count,
                    int(digest.generated_by_llm),
                ),
            )

    def latest_digest(self) -> Optional[Dict[str, Any]]:
        with session(self._config.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM news_digests ORDER BY run_date DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None
