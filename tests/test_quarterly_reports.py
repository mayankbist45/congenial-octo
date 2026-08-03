from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List

from src.decision.engine import DecisionEngine
from src.dashboard.service import DashboardService
from src.fundamentals.reports import QuarterlyReportsService
from src.fundamentals.screener import ScreenerClient
from src.journal.repository import JournalRepository
from src.models import QuarterlyReport, QuarterlyReportAnalysis, QuarterlyReportContext
from src.risk.triggers import TriggerEngine
from tests.conftest import (
    make_diversified_portfolio,
    make_fundamental,
    make_portfolio,
    make_position,
    make_scores,
    make_technical,
)


class FakeLLM:
    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        self.available = True
        self.prompts: List[str] = []

    def complete_json(self, system_prompt: str, user_prompt: str, **_: Any) -> Any:
        self.prompts.append(user_prompt)
        return self.payload


class FakeScreener:
    def __init__(self, reports: Dict[str, List[QuarterlyReport]]):
        self._reports = reports

    def fetch_quarterly_reports(self, symbol: str) -> List[QuarterlyReport]:
        return list(self._reports.get(symbol.upper(), []))

    def close(self) -> None:
        return None


def sample_reports(symbol: str = "TCS") -> List[QuarterlyReport]:
    periods = ["Jun 2025", "Sep 2025", "Dec 2025", "Mar 2026", "Jun 2026"]
    revenue = [100.0, 103.0, 108.0, 110.0, 98.0]
    profit = [20.0, 21.0, 22.0, 23.0, 17.0]
    eps = [10.0, 10.5, 11.0, 11.5, 8.0]
    opm = [22.0, 22.0, 21.0, 20.5, 17.5]
    borrowings = [40.0, 40.0, 42.0, 43.0, 55.0]
    reports: List[QuarterlyReport] = []
    for idx, period in enumerate(periods):
        reports.append(
            QuarterlyReport(
                symbol=symbol,
                fiscal_period=period,
                report_key=f"{symbol}:{period}",
                source="screener.in",
                fetched_at=datetime(2026, 8, 3, 9, 0, 0),
                revenue=revenue[idx],
                net_profit=profit[idx],
                eps=eps[idx],
                opm_pct=opm[idx],
                borrowings=borrowings[idx],
            )
        )
    return reports


def test_screener_parser_extracts_quarterly_rows() -> None:
    html = """
    <div id="top-ratios"></div>
    <section id="quarters">
      <table>
        <thead>
          <tr><th></th><th>Mar 2026</th><th>Jun 2026</th></tr>
        </thead>
        <tbody>
          <tr><td>Sales +</td><td>100</td><td>110</td></tr>
          <tr><td>Operating Profit +</td><td>20</td><td>22</td></tr>
          <tr><td>OPM %</td><td>20%</td><td>20%</td></tr>
          <tr><td>Net Profit +</td><td>10</td><td>11</td></tr>
          <tr><td>EPS in Rs</td><td>5</td><td>5.5</td></tr>
        </tbody>
      </table>
    </section>
    <section id="balance-sheet">
      <table>
        <thead>
          <tr><th></th><th>Mar 2026</th><th>Jun 2026</th></tr>
        </thead>
        <tbody>
          <tr><td>Borrowings +</td><td>40</td><td>42</td></tr>
        </tbody>
      </table>
    </section>
    <section id="shareholding">
      <table>
        <thead>
          <tr><th></th><th>Mar 2026</th><th>Jun 2026</th></tr>
        </thead>
        <tbody>
          <tr><td>Promoters +</td><td>50</td><td>51</td></tr>
          <tr><td>Pledged +</td><td>0</td><td>1</td></tr>
        </tbody>
      </table>
    </section>
    """
    client = ScreenerClient()
    client._fetch_html = lambda symbol: html  # type: ignore[assignment]

    reports = client.fetch_quarterly_reports("TCS")

    assert [report.fiscal_period for report in reports] == ["Mar 2026", "Jun 2026"]
    assert reports[-1].revenue == 110.0
    assert reports[-1].borrowings == 42.0
    assert reports[-1].promoter_pledge_pct == 1.0
    client.close()


def test_duplicate_runs_do_not_reingest_the_same_report(temp_config) -> None:
    repo = JournalRepository(temp_config)
    service = QuarterlyReportsService(
        temp_config,
        repo=repo,
        screener=FakeScreener({"TCS": sample_reports()}),
    )

    first = service.detect_new_reports(["TCS"], date(2026, 8, 3))
    second = service.detect_new_reports(["TCS"], date(2026, 8, 4))

    assert first.new_symbols == ["TCS"]
    assert second.new_symbols == []
    assert len(repo.quarterly_history("TCS")) == 5


def test_negative_quarter_creates_review_trigger(temp_config) -> None:
    service = QuarterlyReportsService(
        temp_config,
        repo=JournalRepository(temp_config),
        screener=FakeScreener({"TCS": sample_reports()}),
    )
    quarterly_context = service.detect_new_reports(["TCS"], date(2026, 8, 3))
    portfolio = make_diversified_portfolio()

    triggers = TriggerEngine(temp_config).evaluate(
        portfolio=portfolio,
        scores={symbol: make_scores(symbol) for symbol in portfolio.symbols},
        fundamentals={symbol: make_fundamental(symbol) for symbol in portfolio.symbols},
        technicals={symbol: make_technical(symbol) for symbol in portfolio.symbols},
        quarterly_context=quarterly_context,
    )

    assert any(t.trigger_type.value == "quarterly_report_review" for t in triggers)


def test_decision_prompt_includes_quarterly_report_updates(temp_config) -> None:
    llm = FakeLLM(
        {
            "market_view": "quiet",
            "portfolio_assessment": "steady",
            "action_taken": "no_trades",
            "reasoning": "no change",
            "proposals": [],
        }
    )
    portfolio = make_diversified_portfolio()
    context = QuarterlyReportContext(
        new_symbols=["TCS"],
        analyses={
            "TCS": QuarterlyReportAnalysis(
                symbol="TCS",
                report_key="TCS:Jun 2026",
                run_date=date(2026, 8, 3),
                fiscal_period="Jun 2026",
                summary="Revenue slowed and margin compressed.",
                thesis_impact="negative",
                flags=["revenue_slowdown", "margin_compression"],
            )
        },
        histories={"TCS": sample_reports()[::-1]},
    )

    DecisionEngine(config=temp_config, llm=llm).decide(
        portfolio=portfolio,
        scores={"TCS": make_scores("TCS")},
        fundamentals={"TCS": make_fundamental("TCS")},
        technicals={"TCS": make_technical("TCS")},
        triggers=[],
        candidates=[],
        digest=None,
        risk_profile=None,
        quarterly_context=context,
    )

    prompt = llm.prompts[0]
    assert "QUARTERLY REPORT UPDATES" in prompt
    assert "TCS: fresh quarterly report analysed." in prompt


def test_dashboard_reads_quarterly_history(temp_config) -> None:
    repo = JournalRepository(temp_config)
    repo.save_quarterly_reports(sample_reports())
    repo.save_quarterly_report_analysis(
        QuarterlyReportAnalysis(
            symbol="TCS",
            report_key="TCS:Jun 2026",
            run_date=date.today(),
            fiscal_period="Jun 2026",
            summary="Fresh quarter was weak.",
            thesis_impact="negative",
            flags=["profit_decline"],
        )
    )
    portfolio = make_portfolio([make_position("TCS")])
    repo.record_position_snapshot(
        date.today(),
        portfolio,
        scores={"TCS": make_scores("TCS")},
        risk_profile=None,
    )
    cards = DashboardService(temp_config).quarterly_report_cards()

    assert any(card["symbol"] == "TCS" for card in cards)
