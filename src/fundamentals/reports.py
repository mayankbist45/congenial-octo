"""Quarterly report ingestion and analysis.

Keeps quarterly statement history separate from the latest fundamental snapshot
used by the quant screen. Report rows are stored once per symbol, while each
persona records its own interpretation of the latest quarter.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional

from src.config import Config, get_config
from src.fundamentals.parsing import growth_pct
from src.fundamentals.screener import ScreenerClient
from src.journal.repository import JournalRepository
from src.logging_setup import get_logger
from src.models import QuarterlyReport, QuarterlyReportAnalysis, QuarterlyReportContext

logger = get_logger(__name__)


class QuarterlyReportAnalyzer:
    """Turns one latest quarter plus history into an investor-readable view."""

    def analyze(
        self,
        symbol: str,
        history: List[QuarterlyReport],
        run_date: date,
    ) -> QuarterlyReportAnalysis:
        latest = history[0]
        prior = history[1] if len(history) > 1 else None
        year_ago = history[4] if len(history) > 4 else None

        latest = replace(
            latest,
            revenue_yoy_pct=latest.revenue_yoy_pct
            if latest.revenue_yoy_pct is not None
            else growth_pct(latest.revenue, year_ago.revenue if year_ago else None),
            profit_yoy_pct=latest.profit_yoy_pct
            if latest.profit_yoy_pct is not None
            else growth_pct(latest.net_profit, year_ago.net_profit if year_ago else None),
            eps_yoy_pct=latest.eps_yoy_pct
            if latest.eps_yoy_pct is not None
            else growth_pct(latest.eps, year_ago.eps if year_ago else None),
            opm_delta_pct=latest.opm_delta_pct
            if latest.opm_delta_pct is not None
            else (
                latest.opm_pct - prior.opm_pct
                if prior and latest.opm_pct is not None and prior.opm_pct is not None
                else None
            ),
            debt_delta_pct=latest.debt_delta_pct
            if latest.debt_delta_pct is not None
            else growth_pct(
                latest.borrowings, prior.borrowings if prior else None
            ),
        )

        flags: List[str] = []
        if latest.revenue_yoy_pct is not None and latest.revenue_yoy_pct < 0:
            flags.append("revenue_decline")
        elif latest.revenue_yoy_pct is not None and latest.revenue_yoy_pct < 5:
            flags.append("revenue_slowdown")

        if latest.profit_yoy_pct is not None and latest.profit_yoy_pct < 0:
            flags.append("profit_decline")
        if latest.eps_yoy_pct is not None and latest.eps_yoy_pct < 0:
            flags.append("eps_decline")
        if latest.opm_delta_pct is not None and latest.opm_delta_pct <= -2.0:
            flags.append("margin_compression")
        if latest.debt_delta_pct is not None and latest.debt_delta_pct >= 15.0:
            flags.append("debt_rise")
        if (
            prior
            and latest.promoter_pledge_pct is not None
            and prior.promoter_pledge_pct is not None
            and latest.promoter_pledge_pct > prior.promoter_pledge_pct + 0.5
        ):
            flags.append("pledge_increase")

        negative_flags = {
            "profit_decline",
            "eps_decline",
            "margin_compression",
            "debt_rise",
            "pledge_increase",
            "revenue_decline",
        }
        positive = (
            latest.revenue_yoy_pct is not None
            and latest.profit_yoy_pct is not None
            and latest.revenue_yoy_pct > 12
            and latest.profit_yoy_pct > 12
            and "margin_compression" not in flags
        )
        thesis_impact = (
            "negative"
            if any(flag in negative_flags for flag in flags)
            else "positive"
            if positive
            else "neutral"
        )

        summary_parts = [
            f"{latest.fiscal_period}: revenue {self._fmt(latest.revenue)}",
            f"net profit {self._fmt(latest.net_profit)}",
            f"EPS {self._fmt(latest.eps)}",
        ]
        if latest.revenue_yoy_pct is not None:
            summary_parts.append(f"revenue YoY {latest.revenue_yoy_pct:+.1f}%")
        if latest.profit_yoy_pct is not None:
            summary_parts.append(f"profit YoY {latest.profit_yoy_pct:+.1f}%")
        if latest.opm_delta_pct is not None:
            summary_parts.append(f"margin delta {latest.opm_delta_pct:+.1f} pts")
        if flags:
            summary_parts.append("flags: " + ", ".join(flags))
        else:
            summary_parts.append("no thesis-changing issue detected")

        revenue_trend = self._trend(history, "revenue")
        margin_trend = self._trend(history, "opm_pct")
        return QuarterlyReportAnalysis(
            symbol=symbol.upper(),
            report_key=latest.report_key,
            run_date=run_date,
            fiscal_period=latest.fiscal_period,
            summary=" | ".join(summary_parts),
            thesis_impact=thesis_impact,
            flags=flags,
            highlights={
                "revenue_trend": revenue_trend,
                "margin_trend": margin_trend,
            },
            metrics={
                "revenue": latest.revenue,
                "net_profit": latest.net_profit,
                "eps": latest.eps,
                "revenue_yoy_pct": latest.revenue_yoy_pct,
                "profit_yoy_pct": latest.profit_yoy_pct,
                "eps_yoy_pct": latest.eps_yoy_pct,
                "opm_pct": latest.opm_pct,
                "opm_delta_pct": latest.opm_delta_pct,
                "debt_delta_pct": latest.debt_delta_pct,
            },
            source="heuristic",
        )

    @staticmethod
    def _fmt(value: Optional[float]) -> str:
        return "n/a" if value is None else f"{value:.1f}"

    @staticmethod
    def _trend(history: List[QuarterlyReport], field: str) -> str:
        values = [getattr(row, field, None) for row in history[:3]]
        usable = [value for value in reversed(values) if value is not None]
        if len(usable) < 2:
            return "insufficient history"
        if usable[-1] > usable[0]:
            return "improving over the last 3 quarters"
        if usable[-1] < usable[0]:
            return "weakening over the last 3 quarters"
        return "stable over the last 3 quarters"


class QuarterlyReportsService:
    """Fetches and stores fresh quarterly reports only when a new one appears."""

    def __init__(
        self,
        config: Optional[Config] = None,
        repo: Optional[JournalRepository] = None,
        screener: Optional[ScreenerClient] = None,
        analyzer: Optional[QuarterlyReportAnalyzer] = None,
    ):
        self._config = config or get_config()
        self._repo = repo or JournalRepository(self._config)
        self._screener = screener or ScreenerClient(
            request_delay=float(
                self._config.get("fundamentals.request_delay_seconds", 0.5) or 0.5
            )
        )
        self._analyzer = analyzer or QuarterlyReportAnalyzer()

    def history(self, symbol: str, limit: int = 12) -> List[QuarterlyReport]:
        return [
            self._report_from_row(row)
            for row in self._repo.quarterly_history(symbol, limit)
        ]

    def detect_new_reports(
        self,
        symbols: Iterable[str],
        as_of: date,
    ) -> QuarterlyReportContext:
        context = QuarterlyReportContext()
        for symbol in [s.upper() for s in dict.fromkeys(symbols)]:
            try:
                fetched = self._screener.fetch_quarterly_reports(symbol)
            except Exception as exc:
                logger.error("Quarterly report fetch failed for %s: %s", symbol, exc)
                context.errors[symbol] = str(exc)
                context.histories[symbol] = self.history(symbol)
                continue

            if not fetched:
                context.histories[symbol] = self.history(symbol)
                continue

            latest_key = fetched[-1].report_key
            stored_key = self._repo.latest_report_key(symbol)
            if stored_key != latest_key:
                self._populate_derived_metrics(fetched)
                self._repo.save_quarterly_reports(fetched)
                history = self.history(symbol)
                context.histories[symbol] = history
                if history:
                    analysis = self._analyzer.analyze(symbol, history, as_of)
                    self._repo.save_quarterly_report_analysis(analysis)
                    context.analyses[symbol] = analysis
                    context.new_symbols.append(symbol)
            else:
                history = self.history(symbol)
                context.histories[symbol] = history
                existing = self._repo.latest_quarterly_report_analysis(symbol)
                if existing:
                    context.analyses[symbol] = self._analysis_from_row(existing)
        return context

    def close(self) -> None:
        self._screener.close()

    def _populate_derived_metrics(self, reports: List[QuarterlyReport]) -> None:
        for index, report in enumerate(reports):
            prior = reports[index - 1] if index > 0 else None
            year_ago = reports[index - 4] if index > 3 else None
            reports[index] = replace(
                report,
                revenue_yoy_pct=growth_pct(report.revenue, year_ago.revenue if year_ago else None),
                profit_yoy_pct=growth_pct(report.net_profit, year_ago.net_profit if year_ago else None),
                eps_yoy_pct=growth_pct(report.eps, year_ago.eps if year_ago else None),
                opm_delta_pct=(
                    report.opm_pct - prior.opm_pct
                    if prior and report.opm_pct is not None and prior.opm_pct is not None
                    else None
                ),
                debt_delta_pct=growth_pct(
                    report.borrowings, prior.borrowings if prior else None
                ),
            )

    @staticmethod
    def _report_from_row(row: Dict[str, object]) -> QuarterlyReport:
        period_end = row.get("period_end")
        fetched_at = row.get("fetched_at")
        return QuarterlyReport(
            symbol=str(row["symbol"]),
            fiscal_period=str(row["fiscal_period"]),
            report_key=str(row["report_key"]),
            source=str(row.get("source") or "screener.in"),
            fetched_at=datetime.fromisoformat(str(fetched_at)) if fetched_at else None,
            period_end=date.fromisoformat(str(period_end)) if period_end else None,
            revenue=row.get("revenue"),
            operating_profit=row.get("operating_profit"),
            opm_pct=row.get("opm_pct"),
            net_profit=row.get("net_profit"),
            eps=row.get("eps"),
            interest=row.get("interest"),
            borrowings=row.get("borrowings"),
            promoter_holding_pct=row.get("promoter_holding_pct"),
            promoter_pledge_pct=row.get("promoter_pledge_pct"),
            revenue_yoy_pct=row.get("revenue_yoy_pct"),
            profit_yoy_pct=row.get("profit_yoy_pct"),
            eps_yoy_pct=row.get("eps_yoy_pct"),
            opm_delta_pct=row.get("opm_delta_pct"),
            debt_delta_pct=row.get("debt_delta_pct"),
        )

    @staticmethod
    def _analysis_from_row(row: Dict[str, object]) -> QuarterlyReportAnalysis:
        return QuarterlyReportAnalysis(
            symbol=str(row["symbol"]),
            report_key=str(row["report_key"]),
            run_date=date.fromisoformat(str(row["run_date"])),
            fiscal_period=str(row["fiscal_period"]),
            summary=str(row["summary"]),
            thesis_impact=str(row.get("thesis_impact") or "unclear"),
            flags=[] if not row.get("flags") else json.loads(str(row["flags"])),
            highlights=json.loads(str(row["highlights"])) if row.get("highlights") else {},
            metrics=json.loads(str(row["metrics"])) if row.get("metrics") else {},
            source=str(row.get("source") or "heuristic"),
            raw_error=str(row.get("raw_error") or ""),
        )
