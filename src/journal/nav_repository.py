"""Portfolio state, analytics, and history persistence."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from src.journal._base import JournalRepositoryBase
from src.journal._shared import iso_date, iso_datetime
from src.journal.db import session
from src.models import (
    NewsDigest,
    PortfolioRiskProfile,
    PortfolioState,
    QuarterlyReport,
    QuarterlyReportAnalysis,
)


class NavRepository(JournalRepositoryBase):
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

    def record_risk_profile(self, run_date: date, profile: PortfolioRiskProfile) -> None:
        payload = asdict(profile)
        payload["as_of"] = iso_date(profile.as_of)
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
                            iso_datetime(report.fetched_at)
                            or datetime.now().isoformat(timespec="seconds"),
                            iso_date(report.period_end),
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
                        iso_datetime(report.fetched_at)
                        or datetime.now().isoformat(timespec="seconds"),
                        iso_date(report.period_end),
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

    def save_quarterly_report_analysis(self, analysis: QuarterlyReportAnalysis) -> int:
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
