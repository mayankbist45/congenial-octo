"""Dashboard tests.

The dashboard is the approval gate, so the tests care about two things above all:
that a pending proposal cannot be approved twice or approved after rejection, and
that every page renders on an empty database rather than 500-ing on a fresh
install.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from src.dashboard.app import create_app
from src.dashboard.service import DashboardService, pct
from src.journal.repository import JournalRepository
from src.models import (
    Action,
    MarketRegimeSnapshot,
    PortfolioRiskProfile,
    RiskMetrics,
    NewsDigest,
    TradeProposal,
)
from tests.conftest import make_diversified_portfolio, make_scores
from tests.conftest import make_portfolio

PAGES = [
    "/",
    "/proposals",
    "/holdings",
    "/news",
    "/performance",
    "/personas",
    "/intraday",
    "/journal",
]
API = [
    "/api/overview",
    "/api/holdings",
    "/api/proposals",
    "/api/performance",
    "/api/risk",
    "/api/personas",
    "/api/intraday",
]


@pytest.fixture
def repo(temp_config) -> JournalRepository:
    return JournalRepository(temp_config)


@pytest.fixture
def service(temp_config) -> DashboardService:
    return DashboardService(temp_config)


@pytest.fixture
def client(temp_config) -> TestClient:
    return TestClient(create_app(temp_config))


def proposal(
    symbol: str = "WIPRO",
    action: Action = Action.BUY,
    quantity: int = 100,
    source: str = "llm",
) -> TradeProposal:
    return TradeProposal(
        symbol=symbol,
        action=action,
        quantity=quantity,
        rationale="Cash generative business at a reasonable multiple.",
        trigger_types=["initial_build"],
        confidence=0.7,
        reference_price=100.0,
        estimated_value=quantity * 100.0,
        company_name=f"{symbol} Ltd",
        sector="Information Technology",
        source=source,
        thesis="Margins hold and the order book keeps growing.",
        target_price=140.0,
        warnings=["RSI 78 is overbought; entry may be poorly timed"],
    )


def seed_run(repo: JournalRepository, proposals=None) -> int:
    run_id = repo.start_run(date.today(), False, 1_500_000.0, 100_000.0)
    repo.save_proposals(run_id, proposals or [proposal()])
    repo.finish_run(run_id, "Market view: quiet.\n\nReasoning: holding.", True, 12.5)
    return run_id


def seed_history(repo: JournalRepository, days: int = 40) -> None:
    """A NAV series where the fund modestly beats the index."""
    portfolio = make_diversified_portfolio(cash=100_000)
    start = date.today() - timedelta(days=days)
    for offset in range(days):
        day = start + timedelta(days=offset)
        portfolio.cash = 100_000 + offset * 900
        repo.record_nav(
            run_date=day,
            portfolio=portfolio,
            benchmark_close=20_000.0 + offset * 8.0,
            daily_return_pct=0.06,
            benchmark_return_pct=0.04,
        )
    risk_profile = PortfolioRiskProfile(
            as_of=date.today(),
            portfolio_beta=0.98,
            portfolio_vol_annualised=16.4,
            tracking_error_pct=6.2,
            active_share_pct=71.0,
            active_factor_share_pct=38.0,
            active_selection_share_pct=62.0,
            concentration_top5_pct=32.0,
            effective_positions=12.4,
            avg_pairwise_correlation=0.41,
            market_regime=MarketRegimeSnapshot(
                as_of=date.today(),
                regime="elevated",
                score=48.2,
                evidence=["Benchmark volatility above its 20-day average"],
            ),
            per_position={
                p.symbol: RiskMetrics(
                    symbol=p.symbol,
                    beta=1.02,
                    idiosyncratic_vol=19.0,
                    risk_contribution_pct=7.1,
                    pct_of_adv=0.4,
                )
                for p in portfolio.positions
            },
            warnings=["Tracking error 6.2% is within the limit"],
        )
    repo.record_position_snapshot(
        date.today(),
        portfolio,
        scores={p.symbol: make_scores(p.symbol) for p in portfolio.positions},
        risk_profile=risk_profile,
    )
    repo.record_risk_profile(date.today(), risk_profile)


# ----------------------------------------------------------------------
# Empty install
# ----------------------------------------------------------------------
@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_on_an_empty_database(client, path) -> None:
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert "<html" in response.text


@pytest.mark.parametrize("path", API)
def test_every_api_endpoint_responds_when_empty(client, path) -> None:
    assert client.get(path).status_code == 200


def test_empty_overview_tells_the_user_what_to_do(client) -> None:
    body = client.get("/").text
    assert "has not run yet" in body
    assert "run_daily.py" in body


# ----------------------------------------------------------------------
# Populated pages
# ----------------------------------------------------------------------
@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_with_data(client, repo, path) -> None:
    seed_run(repo)
    seed_history(repo)

    response = client.get(path)
    assert response.status_code == 200, response.text


def test_overview_shows_the_pending_proposal_and_memo(client, repo) -> None:
    seed_run(repo)
    seed_history(repo)

    body = client.get("/").text
    assert "WIPRO" in body
    assert "awaiting your decision" in body
    assert "Market view" in body


def test_pct_signed_zero_omits_plus() -> None:
    assert pct(0.0, 2, True) == "0.00%"
    assert pct(1.25, 2, True) == "+1.25%"


def test_overview_total_return_uses_nav_history(service, repo) -> None:
    seed_run(repo)
    seed_history(repo, days=20)
    overview = service.overview()
    perf = service.performance()
    assert overview["total_return_pct"] is not None
    assert overview["total_return_pct"] == perf["total_return_pct"]
    assert overview["total_return_pct"] > 0


def test_proposal_card_shows_rationale_thesis_and_warnings(client, repo) -> None:
    seed_run(repo)

    body = client.get("/proposals").text
    assert "Cash generative business" in body
    assert "Margins hold" in body
    assert "overbought" in body
    assert "initial_build" in body


def test_mandatory_proposal_is_marked_as_rule_enforced(client, repo) -> None:
    seed_run(repo, [proposal("TCS", Action.SELL, source="risk_engine")])

    body = client.get("/proposals").text
    assert "Rule enforced" in body


def test_holdings_page_lists_positions_with_risk_metrics(client, repo) -> None:
    seed_history(repo)

    body = client.get("/holdings").text
    assert "TCS" in body
    assert "Idio vol" in body
    assert "Sector exposure" in body


def test_performance_page_reports_excess_return(client, repo) -> None:
    seed_history(repo)

    body = client.get("/performance").text
    assert "Excess return" in body
    assert "Sharpe" in body
    assert "<svg" in body


def test_news_page_shows_the_digest(client, repo, temp_config) -> None:
    repo.record_digest(
        NewsDigest(
            as_of=date.today(),
            macro_summary="Inflation eased for a third month.",
            market_summary="Indices closed flat on thin volumes.",
            holdings_notes={"TCS": "Large deal win in Europe."},
            watchlist_notes={"WIPRO": "New chief executive appointed."},
            flagged_symbols=["TATASTEEL"],
            article_count=64,
            high_impact_count=5,
            generated_by_llm=True,
        )
    )

    body = client.get("/news").text
    assert "Inflation eased" in body
    assert "Large deal win" in body
    assert "TATASTEEL" in body


def test_journal_links_to_the_run_detail(client, repo) -> None:
    run_id = seed_run(repo)

    assert f"/journal/{run_id}" in client.get("/journal").text
    detail = client.get(f"/journal/{run_id}")
    assert detail.status_code == 200
    assert "WIPRO" in detail.text


def test_unknown_run_is_a_404(client) -> None:
    assert client.get("/journal/9999").status_code == 404


# ----------------------------------------------------------------------
# Approval flow
# ----------------------------------------------------------------------
def test_approving_moves_the_proposal_out_of_the_queue(client, repo, service) -> None:
    seed_run(repo)
    [pending] = service.pending_proposals()

    response = client.post(f"/proposals/{pending['id']}/approve", follow_redirects=False)

    assert response.status_code == 303
    assert service.pending_proposals() == []
    assert [p["symbol"] for p in service.approved_proposals()] == ["WIPRO"]


def test_rejecting_records_the_reason(client, repo, service) -> None:
    seed_run(repo)
    [pending] = service.pending_proposals()

    client.post(
        f"/proposals/{pending['id']}/reject",
        data={"reason": "already enough IT exposure"},
        follow_redirects=False,
    )

    assert service.pending_proposals() == []
    assert service.approved_proposals() == []
    decided = service.recent_decisions()[0]
    assert decided["status"] == "rejected"
    assert decided["rejection_reason"] == "already enough IT exposure"


def test_a_proposal_cannot_be_approved_twice(client, repo, service) -> None:
    """Guards against a double-submitted form placing the order twice."""
    seed_run(repo)
    [pending] = service.pending_proposals()

    first = client.post(f"/proposals/{pending['id']}/approve", follow_redirects=False)
    second = client.post(f"/proposals/{pending['id']}/approve", follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 409


def test_a_rejected_proposal_cannot_then_be_approved(client, repo, service) -> None:
    seed_run(repo)
    [pending] = service.pending_proposals()

    client.post(f"/proposals/{pending['id']}/reject", follow_redirects=False)
    response = client.post(f"/proposals/{pending['id']}/approve", follow_redirects=False)

    assert response.status_code == 409
    assert service.approved_proposals() == []


def test_unknown_proposal_id_is_a_conflict_not_a_crash(client) -> None:
    assert client.post("/proposals/424242/approve").status_code == 409


def test_approve_all_clears_the_queue(client, repo, service) -> None:
    seed_run(
        repo,
        [proposal("WIPRO"), proposal("HCLTECH"), proposal("TECHM")],
    )

    client.post("/proposals/approve-all", follow_redirects=False)

    assert service.pending_proposals() == []
    assert len(service.approved_proposals()) == 3


def test_reject_all_clears_the_queue_without_approving(client, repo, service) -> None:
    seed_run(repo, [proposal("WIPRO"), proposal("HCLTECH")])

    client.post("/proposals/reject-all", follow_redirects=False)

    assert service.pending_proposals() == []
    assert service.approved_proposals() == []


# ----------------------------------------------------------------------
# Service-level checks
# ----------------------------------------------------------------------
def test_nav_series_is_rebased_to_100(service, repo) -> None:
    seed_history(repo)

    series = service.nav_series()

    assert series["fund"][0] == 100.0
    assert series["benchmark"][0] == 100.0
    assert len(series["fund"]) == len(series["dates"])


def test_attribution_sums_close_to_total_unrealised_pnl(service, repo) -> None:
    seed_history(repo)

    rows = service.attribution()
    overview = service.overview()
    total_contribution = sum(r["contribution_pct"] for r in rows)
    expected = overview["unrealised_pnl"] / overview["total_value"] * 100.0

    assert rows
    assert abs(total_contribution - expected) < 0.5


def test_sector_weights_sum_to_the_invested_share(service, repo) -> None:
    seed_history(repo)

    total = sum(row["weight_pct"] for row in service.sector_weights())
    invested_pct = 100.0 - service.overview()["cash_pct"]

    assert abs(total - invested_pct) < 0.5


def test_overview_exposes_market_regime(service, repo) -> None:
    seed_run(repo)
    seed_history(repo)

    overview = service.overview()

    assert overview["market_regime"]["regime"] == "elevated"
    assert overview["market_regime"]["score"] == 48.2


def test_performance_falls_back_to_reconstructed_book_when_nav_is_cash_only(
    service, repo
) -> None:
    portfolio = make_diversified_portfolio(cash=100_000)
    portfolio.positions[0].last_price = 120.0
    start = date.today() - timedelta(days=5)
    repo.record_nav(
        run_date=start,
        portfolio=make_portfolio([], cash=1_500_000),
        benchmark_close=20_000.0,
        daily_return_pct=0.0,
        benchmark_return_pct=0.0,
    )
    repo.record_nav(
        run_date=date.today(),
        portfolio=make_portfolio([], cash=1_500_000),
        benchmark_close=20_050.0,
        daily_return_pct=0.0,
        benchmark_return_pct=0.1,
    )
    repo.record_position_snapshot(
        date.today(),
        portfolio,
        scores={p.symbol: make_scores(p.symbol) for p in portfolio.positions},
        risk_profile=None,
    )

    perf = service.performance()

    assert perf["current_value"] > perf["starting_value"]
    assert perf["total_return_pct"] > 0


def test_risk_profile_exposes_market_regime(service, repo) -> None:
    seed_history(repo)

    risk = service.risk_profile()

    assert risk is not None
    assert risk["market_regime"]["regime"] == "elevated"
    assert risk["market_regime"]["evidence"]


def test_briefing_is_served_when_it_exists(service, temp_config) -> None:
    today = date.today()
    path = temp_config.briefing_dir / f"briefing_{today.isoformat()}.txt"
    path.write_text("DAILY BRIEFING - test", encoding="utf-8")

    assert "DAILY BRIEFING" in (service.briefing(today.isoformat()) or "")
    assert service.briefing("not-a-date") is None
    assert service.briefing("1999-01-01") is None


def test_personas_page_groups_india_and_us(client) -> None:
    body = client.get("/personas").text
    assert "India" in body
    assert "United States" in body
    assert "calan_us" in body
    assert "balanced_us" in body
    assert "calan" in body


def test_intraday_page_lists_regions(client) -> None:
    body = client.get("/intraday").text
    assert "Intraday" in body
    assert "calan_us" in body
    assert "revert" in body


def test_intraday_detail_renders_live_paper_completed_trades(client, temp_config) -> None:
    intraday_dir = temp_config.root / "data" / "intraday"
    intraday_dir.mkdir(parents=True, exist_ok=True)
    (intraday_dir / "calan_latest.json").write_text(
        """{
          "persona_id": "calan",
          "start_date": "2026-08-04",
          "end_date": "2026-08-04",
          "total_net_pnl": -180.03,
          "total_return_pct": -0.04,
          "total_trades": 2,
          "win_rate_pct": 50.0,
          "expectancy_r": -0.045,
          "label": "live paper",
          "active": true,
          "completed_trades": [{
            "symbol": "HDFCBANK",
            "strategy": "opening_range_breakout",
            "side": "SELL",
            "quantity": 134,
            "entry_time": "2026-08-04 10:10:00",
            "exit_time": "2026-08-04 11:30:00",
            "entry_price": 746.2,
            "exit_price": 742.41,
            "net_pnl": 385.22,
            "r_multiple": 0.1926,
            "exit_reason": "target"
          }],
          "open_positions": [{
            "symbol": "HINDALCO",
            "strategy": "opening_range_breakout",
            "side": "SELL",
            "quantity": 99,
            "entry_time": "2026-08-04 12:05:00",
            "entry_price": 1001.9,
            "stop_price": 1011.51,
            "target_price": 987.99
          }]
        }""",
        encoding="utf-8",
    )

    body = client.get("/intraday/calan").text
    assert "HDFCBANK" in body
    assert "HINDALCO" in body
    assert "Live paper" in body
    assert "Open positions" in body


def test_intraday_detail_renders_saved_summary(client, temp_config) -> None:
    intraday_dir = temp_config.root / "data" / "intraday"
    intraday_dir.mkdir(parents=True, exist_ok=True)
    (intraday_dir / "calan_us_latest.json").write_text(
        """{
          "persona_id": "calan_us",
          "start_date": "2026-08-03",
          "end_date": "2026-08-03",
          "total_net_pnl": 102.93,
          "total_return_pct": 0.21,
          "total_trades": 3,
          "win_rate_pct": 66.7,
          "expectancy_r": 0.172,
          "strategy_pnl": {"opening_range_breakout": 102.93},
          "trades": [{
            "symbol": "NVDA",
            "strategy": "opening_range_breakout",
            "side": "BUY",
            "quantity": 49,
            "entry_time": "2026-08-03 10:00:00",
            "exit_time": "2026-08-03 10:15:00",
            "entry_price": 202.4,
            "exit_price": 205.08,
            "net_pnl": 116.46,
            "r_multiple": 0.582,
            "exit_reason": "trailing_stop"
          }]
        }""",
        encoding="utf-8",
    )

    response = client.get("/intraday/calan_us")
    assert response.status_code == 200
    body = response.text
    assert "NVDA" in body
    assert "trailing_stop" in body
    assert "United States" in body or "region-us" in body


def test_overview_shows_intraday_us_trades(client, temp_config) -> None:
    intraday_dir = temp_config.root / "data" / "intraday"
    intraday_dir.mkdir(parents=True, exist_ok=True)
    (intraday_dir / "calan_us_latest.json").write_text(
        """{
          "persona_id": "calan_us",
          "start_date": "2026-08-05",
          "end_date": "2026-08-05",
          "starting_capital": 50000,
          "total_net_pnl": 42.5,
          "total_return_pct": 0.09,
          "total_trades": 1,
          "win_rate_pct": 100.0,
          "expectancy_r": 0.5,
          "active": false,
          "last_scan_at": "2026-08-05 16:00:00",
          "watchlist": [{"symbol": "INTC", "confidence": 0.72, "catalyst_class": "gap", "rationale": "ORB setup"}],
          "trades": [{
            "symbol": "INTC",
            "strategy": "opening_range_breakout",
            "side": "BUY",
            "quantity": 100,
            "entry_time": "2026-08-05 10:05:00",
            "exit_time": "2026-08-05 11:00:00",
            "entry_price": 21.5,
            "exit_price": 21.92,
            "net_pnl": 42.5,
            "r_multiple": 0.5,
            "exit_reason": "target"
          }]
        }""",
        encoding="utf-8",
    )

    body = client.get("/?persona=calan_us").text
    assert "<html" in body
    assert "INTC" in body
    assert "Completed trades" in body
    assert "Watchlist" in body
    assert "Intraday paper" in body


def test_default_overview_lists_intraday_sessions(client, temp_config) -> None:
    intraday_dir = temp_config.root / "data" / "intraday"
    intraday_dir.mkdir(parents=True, exist_ok=True)
    (intraday_dir / "calan_us_latest.json").write_text(
        """{
          "persona_id": "calan_us",
          "start_date": "2026-08-05",
          "end_date": "2026-08-05",
          "total_net_pnl": 0,
          "total_return_pct": 0,
          "total_trades": 0,
          "win_rate_pct": 0,
          "active": true
        }""",
        encoding="utf-8",
    )

    body = client.get("/").text
    assert "<html" in body
    assert "Intraday sessions" in body
    assert "calan_us" in body


def test_proposals_page_us_persona_shows_sp500_not_nifty(client) -> None:
    body = client.get("/proposals?persona=balanced_us").text
    assert "P 500" in body and "S&amp;P" in body
    assert "Nifty 500" not in body


def test_proposals_page_intraday_skips_swing_mandate(client) -> None:
    body = client.get("/proposals?persona=calan_us").text
    assert "Persona Constitution" not in body
    assert "Intraday personas do not use the swing approval gate" in body
    assert "Nifty 500" not in body


def test_us_overview_uses_dollar_formatting(client) -> None:
    body = client.get("/?persona=balanced_us").text
    assert "S&amp;P 500" in body or "United States" in body
    assert "Rs " not in body


def test_intraday_news_page_shows_watchlist(client, temp_config) -> None:
    intraday_dir = temp_config.root / "data" / "intraday"
    intraday_dir.mkdir(parents=True, exist_ok=True)
    (intraday_dir / "calan_us_latest.json").write_text(
        """{
          "persona_id": "calan_us",
          "start_date": "2026-08-05",
          "end_date": "2026-08-05",
          "active": true,
          "watchlist": [{"symbol": "INTC", "confidence": 0.7, "catalyst_class": "gap", "rationale": "ORB setup"}]
        }""",
        encoding="utf-8",
    )
    body = client.get("/news?persona=calan_us").text
    assert "Watchlist catalysts" in body
    assert "INTC" in body
    assert "Session scan" in body


def test_personas_page_hides_persona_dropdown(client) -> None:
    body = client.get("/personas").text
    assert "Trade Bot Dashboard" in body
    assert 'class="persona-select"' not in body


def test_intraday_list_hides_persona_dropdown(client) -> None:
    body = client.get("/intraday").text
    assert 'class="persona-select"' not in body


def test_api_personas_returns_regions(client) -> None:
    payload = client.get("/api/personas").json()
    assert "IN" in payload
    assert "US" in payload
    assert any(row["persona_id"] == "calan_us" for row in payload["US"])
