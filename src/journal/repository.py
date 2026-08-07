"""Trade journal persistence.

Every decision the fund makes, taken or rejected, is written here with its
rationale. That record is what makes the fund reviewable: months later it must
be possible to ask why a position was opened and what the manager believed at
the time.
"""

from __future__ import annotations

import json
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
    QuarterlyReport,
    QuarterlyReportAnalysis,
    TradeProposal,
)

logger = get_logger(__name__)


def _iso(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None


def _iso_dt(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


class JournalRepository:
    """Reads and writes the fund's records."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._persona_id = self._config.persona_id

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
        with session(self._config) as conn:
            return conn.insert_returning_id(
                """
                INSERT INTO runs (persona_id, run_date, started_at, is_deep_review,
                                  portfolio_value, cash, status)
                VALUES (?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    self._persona_id,
                    run_date.isoformat(),
                    datetime.now().isoformat(timespec="seconds"),
                    int(is_deep_review),
                    portfolio_value,
                    cash,
                ),
            )

    def finish_run(
        self,
        run_id: int,
        memo: str,
        llm_used: bool,
        turnover_used_pct: float,
        status: str = "complete",
    ) -> None:
        with session(self._config) as conn:
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

    # ------------------------------------------------------------------
    # Proposals
    # ------------------------------------------------------------------
    def save_proposals(
        self, run_id: int, proposals: List[TradeProposal]
    ) -> List[int]:
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

    def save_rejected(
        self, run_id: int, rejected: List[Tuple[TradeProposal, str]]
    ) -> None:
        """Record proposals the risk engine blocked, with the reason."""
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
        """Proposals the human approved that have not yet been sent to the broker."""
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

    # ------------------------------------------------------------------
    # Position reconstruction (used by the simulated broker)
    # ------------------------------------------------------------------
    def reconstruct_positions(self) -> List[Dict[str, Any]]:
        """Rebuild holdings and average cost from the fill history."""
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

    def traded_value_between(
        self, start: date, end: date
    ) -> Tuple[float, float]:
        """Purchase and sale value in a window, for turnover measurement."""
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

    def average_net_assets_between(self, start: date, end: date) -> float:
        with session(self._config) as conn:
            row = conn.execute(
                """
                SELECT AVG(total_value) AS average FROM nav_history
                 WHERE persona_id = ? AND run_date BETWEEN ? AND ?
                """,
                (self._persona_id, start.isoformat(), end.isoformat()),
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
        """Store the context a sell trigger needs later."""
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
        with session(self._config) as conn:
            conn.execute(
                """
                INSERT INTO nav_history (
                    persona_id, run_date, total_value, invested_value, cash,
                    position_count, benchmark_close, daily_return_pct,
                    benchmark_return_pct, realised_pnl, unrealised_pnl
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(persona_id, run_date) DO UPDATE SET
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
                    self._persona_id,
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
        with session(self._config) as conn:
            rows = conn.execute(
                "SELECT * FROM nav_history WHERE persona_id = ? ORDER BY run_date",
                (self._persona_id,),
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

        with session(self._config) as conn:
            existing = {
                row["symbol"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM position_snapshots "
                    "WHERE persona_id = ? AND run_date = ?",
                    (self._persona_id, run_date.isoformat()),
                ).fetchall()
            }
            conn.execute(
                "DELETE FROM position_snapshots "
                "WHERE persona_id = ? AND run_date = ?",
                (self._persona_id, run_date.isoformat()),
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
                        persona_id, run_date, symbol, company_name, sector,
                        quantity, avg_cost, last_price, market_value, weight_pct,
                        return_pct, unrealised_pnl, holding_days,
                        composite_score, beta, idiosyncratic_vol,
                        risk_contribution_pct, pct_of_adv, thesis
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        self._persona_id,
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
        with session(self._config) as conn:
            rows = conn.execute(
                """
                SELECT * FROM position_snapshots
                 WHERE persona_id = ?
                   AND run_date = (
                       SELECT MAX(run_date) FROM position_snapshots
                        WHERE persona_id = ?
                   )
                 ORDER BY market_value DESC
                """,
                (self._persona_id, self._persona_id),
            ).fetchall()
            return [dict(r) for r in rows]

    def record_risk_profile(
        self, run_date: date, profile: PortfolioRiskProfile
    ) -> None:
        payload = asdict(profile)
        payload["as_of"] = _iso(profile.as_of)
        with session(self._config) as conn:
            conn.execute(
                """
                INSERT INTO risk_history (
                    persona_id, run_date, portfolio_beta, portfolio_vol,
                    idio_share_pct, tracking_error_pct, active_share_pct,
                    concentration_top5_pct, avg_pairwise_correlation, payload
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(persona_id, run_date) DO UPDATE SET
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
                    self._persona_id,
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
        with session(self._config) as conn:
            row = conn.execute(
                "SELECT * FROM risk_history WHERE persona_id = ? "
                "ORDER BY run_date DESC LIMIT 1",
                (self._persona_id,),
            ).fetchone()
            return dict(row) if row else None

    def record_digest(self, digest: NewsDigest) -> None:
        with session(self._config) as conn:
            conn.execute(
                """
                INSERT INTO news_digests (
                    persona_id, run_date, macro_summary, market_summary,
                    holdings_notes, watchlist_notes, flagged_symbols,
                    article_count, high_impact_count, generated_by_llm
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(persona_id, run_date) DO UPDATE SET
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
                    self._persona_id,
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
        with session(self._config) as conn:
            row = conn.execute(
                "SELECT * FROM news_digests WHERE persona_id = ? "
                "ORDER BY run_date DESC LIMIT 1",
                (self._persona_id,),
            ).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # Quarterly report history
    # ------------------------------------------------------------------
    def save_quarterly_reports(self, reports: List[QuarterlyReport]) -> List[int]:
        ids: List[int] = []
        with session(self._config) as conn:
            for report in reports:
                existing = conn.execute(
                    """
                    SELECT id FROM quarterly_reports
                     WHERE symbol = ? AND fiscal_period = ? AND source = ?
                    """,
                    (report.symbol.upper(), report.fiscal_period, report.source),
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE quarterly_reports
                           SET report_key = ?, fetched_at = ?, period_end = ?,
                               revenue = ?, operating_profit = ?, opm_pct = ?,
                               net_profit = ?, eps = ?, interest = ?, borrowings = ?,
                               promoter_holding_pct = ?, promoter_pledge_pct = ?,
                               revenue_yoy_pct = ?, profit_yoy_pct = ?,
                               eps_yoy_pct = ?, opm_delta_pct = ?, debt_delta_pct = ?
                         WHERE id = ?
                        """,
                        (
                            report.report_key,
                            _iso_dt(report.fetched_at) or datetime.now().isoformat(timespec="seconds"),
                            _iso(report.period_end),
                            report.revenue,
                            report.operating_profit,
                            report.opm_pct,
                            report.net_profit,
                            report.eps,
                            report.interest,
                            report.borrowings,
                            report.promoter_holding_pct,
                            report.promoter_pledge_pct,
                            report.revenue_yoy_pct,
                            report.profit_yoy_pct,
                            report.eps_yoy_pct,
                            report.opm_delta_pct,
                            report.debt_delta_pct,
                            existing["id"],
                        ),
                    )
                    ids.append(int(existing["id"]))
                    continue

                report_id = conn.insert_returning_id(
                    """
                    INSERT INTO quarterly_reports (
                        symbol, fiscal_period, report_key, source, fetched_at,
                        period_end, revenue, operating_profit, opm_pct, net_profit,
                        eps, interest, borrowings, promoter_holding_pct,
                        promoter_pledge_pct, revenue_yoy_pct, profit_yoy_pct,
                        eps_yoy_pct, opm_delta_pct, debt_delta_pct
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        report.symbol.upper(),
                        report.fiscal_period,
                        report.report_key,
                        report.source,
                        _iso_dt(report.fetched_at) or datetime.now().isoformat(timespec="seconds"),
                        _iso(report.period_end),
                        report.revenue,
                        report.operating_profit,
                        report.opm_pct,
                        report.net_profit,
                        report.eps,
                        report.interest,
                        report.borrowings,
                        report.promoter_holding_pct,
                        report.promoter_pledge_pct,
                        report.revenue_yoy_pct,
                        report.profit_yoy_pct,
                        report.eps_yoy_pct,
                        report.opm_delta_pct,
                        report.debt_delta_pct,
                    ),
                )
                ids.append(report_id)
        return ids

    def latest_report_key(self, symbol: str) -> Optional[str]:
        with session(self._config) as conn:
            row = conn.execute(
                """
                SELECT report_key
                  FROM quarterly_reports
                 WHERE symbol = ?
                 ORDER BY period_end DESC, fetched_at DESC, id DESC
                 LIMIT 1
                """,
                (symbol.upper(),),
            ).fetchone()
            return str(row["report_key"]) if row and row["report_key"] else None

    def quarterly_history(self, symbol: str, limit: int = 12) -> List[Dict[str, Any]]:
        with session(self._config) as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM quarterly_reports
                 WHERE symbol = ?
                 ORDER BY period_end DESC, fetched_at DESC, id DESC
                 LIMIT ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_quarterly_report(self, symbol: str) -> Optional[Dict[str, Any]]:
        rows = self.quarterly_history(symbol, limit=1)
        return rows[0] if rows else None

    def save_quarterly_report_analysis(
        self, analysis: QuarterlyReportAnalysis
    ) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with session(self._config) as conn:
            existing = conn.execute(
                """
                SELECT id FROM quarterly_report_analyses
                 WHERE persona_id = ? AND symbol = ? AND report_key = ?
                """,
                (self._persona_id, analysis.symbol.upper(), analysis.report_key),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE quarterly_report_analyses
                       SET fiscal_period = ?, run_date = ?, summary = ?,
                           thesis_impact = ?, sector_model = ?, flags = ?, highlights = ?,
                           metrics = ?, source = ?, raw_error = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        analysis.fiscal_period,
                        analysis.run_date.isoformat(),
                        analysis.summary,
                        analysis.thesis_impact,
                        analysis.sector_model,
                        json.dumps(analysis.flags),
                        json.dumps(analysis.highlights),
                        json.dumps(analysis.metrics),
                        analysis.source,
                        analysis.raw_error,
                        now,
                        existing["id"],
                    ),
                )
                return int(existing["id"])

            return conn.insert_returning_id(
                """
                INSERT INTO quarterly_report_analyses (
                    persona_id, symbol, fiscal_period, report_key, run_date,
                    summary, thesis_impact, sector_model, flags, highlights,
                    metrics, source, raw_error, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    self._persona_id,
                    analysis.symbol.upper(),
                    analysis.fiscal_period,
                    analysis.report_key,
                    analysis.run_date.isoformat(),
                    analysis.summary,
                    analysis.thesis_impact,
                    analysis.sector_model,
                    json.dumps(analysis.flags),
                    json.dumps(analysis.highlights),
                    json.dumps(analysis.metrics),
                    analysis.source,
                    analysis.raw_error,
                    now,
                    now,
                ),
            )

    def latest_quarterly_report_analysis(self, symbol: str) -> Optional[Dict[str, Any]]:
        with session(self._config) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM quarterly_report_analyses
                 WHERE persona_id = ? AND symbol = ?
                 ORDER BY run_date DESC, updated_at DESC
                 LIMIT 1
                """,
                (self._persona_id, symbol.upper()),
            ).fetchone()
        return dict(row) if row else None

    def quarterly_report_analyses_for_run(self, run_date: date) -> List[Dict[str, Any]]:
        with session(self._config) as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM quarterly_report_analyses
                 WHERE persona_id = ? AND run_date = ?
                 ORDER BY symbol
                """,
                (self._persona_id, run_date.isoformat()),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Liquid fund positions
    # ------------------------------------------------------------------
    def record_liquid_position(
        self,
        run_date: date,
        scheme_code: str,
        scheme_name: str,
        units: float,
        nav: float,
        market_value: float,
        daily_return_pct: float,
    ) -> None:
        with session(self._config) as conn:
            conn.execute(
                """
                INSERT INTO liquid_positions (
                    persona_id, run_date, scheme_code, scheme_name,
                    units, nav, market_value, daily_return_pct
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(persona_id, run_date) DO UPDATE SET
                    scheme_code = excluded.scheme_code,
                    scheme_name = excluded.scheme_name,
                    units = excluded.units,
                    nav = excluded.nav,
                    market_value = excluded.market_value,
                    daily_return_pct = excluded.daily_return_pct
                """,
                (
                    self._persona_id,
                    run_date.isoformat(),
                    scheme_code,
                    scheme_name,
                    units,
                    nav,
                    market_value,
                    daily_return_pct,
                ),
            )

    def latest_liquid_position(self) -> Optional[Dict[str, Any]]:
        with session(self._config) as conn:
            row = conn.execute(
                "SELECT * FROM liquid_positions WHERE persona_id = ? "
                "ORDER BY run_date DESC LIMIT 1",
                (self._persona_id,),
            ).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # Learning reviews and lessons
    # ------------------------------------------------------------------
    def reviewable_proposals(self) -> List[Dict[str, Any]]:
        """Executed/rejected proposals with the context needed for learning."""
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
        """Persist one outcome review, replacing the same proposal/window."""
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

    def insert_meta_decision(self, row: Dict[str, Any]) -> int:
        """Append one intraday gate decision for meta-label training."""
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
        """Fill outcome fields after the session or backtest labels the row."""
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
        """List gate decisions newest first."""
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
            return [_decode_meta_decision(dict(r)) for r in rows]


def _decode_meta_decision(row: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("features", "news_refs"):
        raw = row.get(key)
        if isinstance(raw, str) and raw:
            try:
                row[key] = json.loads(raw)
            except (TypeError, ValueError):
                row[key] = {} if key == "features" else []
    return row
