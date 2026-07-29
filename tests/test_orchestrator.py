"""Orchestrator and execution tests.

The orchestrator is where a broken data source turns into either a degraded run
or a crashed one, so these tests deliberately break each stage in turn and assert
the cycle still finishes and still journals its reasoning.

Execution is tested against a fake broker: the concerns are that a stale price
stops the order, that a limit price is set on the right side of the market, and
that the journal always records what happened.
"""

from __future__ import annotations

import copy
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pytest

from src.broker.base import (
    BrokerClient,
    BrokerError,
    BrokerFunds,
    BrokerHolding,
    OrderRequest,
    OrderResult,
)
from src.config import Config
from src.execution.executor import OrderExecutor
from src.journal.repository import JournalRepository
from src.models import Action, TradeProposal
from tests.conftest import make_fundamental, make_technical


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------
class FakeBroker(BrokerClient):
    """A broker that records what it was asked to do."""

    def __init__(
        self,
        holdings: Optional[List[BrokerHolding]] = None,
        cash: float = 500_000.0,
        prices: Optional[Dict[str, float]] = None,
        fail_orders: bool = False,
        quote_error: bool = False,
        dry_run: bool = False,
    ):
        self._holdings = holdings or []
        self._cash = cash
        self._prices = prices or {}
        self._fail_orders = fail_orders
        self._quote_error = quote_error
        self._dry_run = dry_run
        self.orders: List[OrderRequest] = []
        self.connected = False

    @property
    def name(self) -> str:
        return "fake"

    def connect(self) -> bool:
        self.connected = True
        return True

    def get_holdings(self) -> List[BrokerHolding]:
        return list(self._holdings)

    def get_funds(self) -> BrokerFunds:
        return BrokerFunds(available_cash=self._cash, net_value=self._cash)

    def get_last_price(self, symbol: str) -> Optional[float]:
        if self._quote_error:
            raise BrokerError("quote feed down")
        return self._prices.get(symbol.upper())

    def get_last_prices(self, symbols: List[str]) -> Dict[str, float]:
        return {s: self._prices[s] for s in symbols if s in self._prices}

    def place_order(self, request: OrderRequest) -> OrderResult:
        self.orders.append(request)
        if self._fail_orders:
            return OrderResult(
                success=False,
                symbol=request.symbol,
                side=request.side,
                quantity=request.quantity,
                error="insufficient margin",
            )
        return OrderResult(
            success=True,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            order_id="ORD123",
            price=request.price,
            status="open",
            dry_run=self._dry_run,
        )


@pytest.fixture
def temp_config(config, tmp_path) -> Config:
    raw = copy.deepcopy(config.raw)
    raw.setdefault("database", {})["path"] = str(tmp_path / "fund.db")
    raw["universe"]["file"] = str(config.path("universe.file"))
    return Config(raw=raw, secrets=config.secrets, root=tmp_path)


@pytest.fixture
def repo(temp_config) -> JournalRepository:
    return JournalRepository(temp_config)


def approved_proposal(
    repo: JournalRepository,
    symbol: str = "WIPRO",
    action: Action = Action.BUY,
    quantity: int = 100,
    price: float = 100.0,
) -> Dict[str, Any]:
    run_id = repo.start_run(date.today(), False, 1_000_000.0, 500_000.0)
    [proposal_id] = repo.save_proposals(
        run_id,
        [
            TradeProposal(
                symbol=symbol,
                action=action,
                quantity=quantity,
                rationale="test",
                reference_price=price,
                estimated_value=quantity * price,
                thesis="A durable franchise at a fair price.",
                target_price=price * 1.4,
            )
        ],
    )
    repo.set_proposal_status(proposal_id, "approved")
    return next(p for p in repo.approved_proposals() if p["id"] == proposal_id)


# ----------------------------------------------------------------------
# Execution: limit pricing
# ----------------------------------------------------------------------
def test_buy_limit_sits_above_the_market(temp_config, repo) -> None:
    broker = FakeBroker(prices={"WIPRO": 100.0})
    proposal = approved_proposal(repo)

    OrderExecutor(broker, temp_config, repo).execute([proposal])

    [order] = broker.orders
    assert order.side == "BUY"
    assert order.price > 100.0
    assert order.price <= 100.0 * 1.01
    assert order.product_type == "DELIVERY"


def test_sell_limit_sits_below_the_market(temp_config, repo) -> None:
    broker = FakeBroker(prices={"TCS": 200.0})
    proposal = approved_proposal(repo, "TCS", Action.SELL, 50, 200.0)

    OrderExecutor(broker, temp_config, repo).execute([proposal])

    [order] = broker.orders
    assert order.side == "SELL"
    assert order.price < 200.0


def test_limit_price_lands_on_a_five_paise_tick(temp_config, repo) -> None:
    broker = FakeBroker(prices={"WIPRO": 333.33})
    proposal = approved_proposal(repo, price=333.33)

    OrderExecutor(broker, temp_config, repo).execute([proposal])

    [order] = broker.orders
    assert round(order.price / 0.05) == pytest.approx(order.price / 0.05, abs=1e-6)


# ----------------------------------------------------------------------
# Execution: refusing to trade
# ----------------------------------------------------------------------
def test_a_gapped_stock_is_not_traded(temp_config, repo) -> None:
    """The approval was for a price that no longer exists."""
    broker = FakeBroker(prices={"WIPRO": 130.0})
    proposal = approved_proposal(repo, price=100.0)

    summary = OrderExecutor(broker, temp_config, repo).execute([proposal])

    assert broker.orders == []
    assert not summary.placed
    [outcome] = summary.outcomes
    assert "price moved" in outcome.error
    assert repo.get_proposal(proposal["id"])["status"] == "abandoned"


def test_a_small_move_still_trades(temp_config, repo) -> None:
    broker = FakeBroker(prices={"WIPRO": 101.5})
    proposal = approved_proposal(repo, price=100.0)

    summary = OrderExecutor(broker, temp_config, repo).execute([proposal])

    assert len(broker.orders) == 1
    assert summary.placed


def test_no_price_anywhere_means_no_order(temp_config, repo) -> None:
    broker = FakeBroker(prices={})
    proposal = approved_proposal(repo)

    summary = OrderExecutor(broker, temp_config, repo).execute([proposal])

    assert broker.orders == []
    assert "no live price" in summary.outcomes[0].error


def test_last_close_is_used_when_the_quote_feed_is_down(temp_config, repo) -> None:
    broker = FakeBroker(quote_error=True)
    proposal = approved_proposal(repo)

    summary = OrderExecutor(broker, temp_config, repo).execute(
        [proposal], technicals={"WIPRO": make_technical("WIPRO")}
    )

    assert len(broker.orders) == 1
    assert summary.placed


def test_broker_rejection_is_journalled_and_the_proposal_marked_failed(
    temp_config, repo
) -> None:
    broker = FakeBroker(prices={"WIPRO": 100.0}, fail_orders=True)
    proposal = approved_proposal(repo)

    summary = OrderExecutor(broker, temp_config, repo).execute([proposal])

    assert not summary.placed
    assert "insufficient margin" in summary.outcomes[0].error
    assert repo.get_proposal(proposal["id"])["status"] == "failed"
    [trade] = repo.trades()
    assert trade["status"] == "rejected"


def test_a_broker_exception_does_not_stop_the_batch(temp_config, repo) -> None:
    class ExplodingBroker(FakeBroker):
        def place_order(self, request: OrderRequest) -> OrderResult:
            raise BrokerError("session expired")

    broker = ExplodingBroker(prices={"WIPRO": 100.0})
    proposal = approved_proposal(repo)

    summary = OrderExecutor(broker, temp_config, repo).execute([proposal])

    assert not summary.placed
    assert "session expired" in summary.outcomes[0].error


# ----------------------------------------------------------------------
# Execution: ordering and bookkeeping
# ----------------------------------------------------------------------
def test_sells_are_placed_before_buys(temp_config, repo) -> None:
    broker = FakeBroker(prices={"WIPRO": 100.0, "TCS": 100.0})
    buy = approved_proposal(repo, "WIPRO", Action.BUY)
    sell = approved_proposal(repo, "TCS", Action.SELL)

    OrderExecutor(broker, temp_config, repo).execute([buy, sell])

    assert [o.side for o in broker.orders] == ["SELL", "BUY"]


def test_a_buy_records_entry_context_for_the_sell_triggers(temp_config, repo) -> None:
    broker = FakeBroker(prices={"WIPRO": 100.0})
    proposal = approved_proposal(repo)

    OrderExecutor(broker, temp_config, repo).execute(
        [proposal], fundamentals={"WIPRO": make_fundamental("WIPRO", pe=24.0)}
    )

    meta = repo.position_meta()["WIPRO"]
    assert meta["entry_pe"] == 24.0
    assert meta["entry_price"] > 0
    assert meta["peak_price"] > 0
    assert "durable franchise" in meta["thesis"]
    assert meta["target_price"] == pytest.approx(140.0)


def test_a_full_exit_clears_the_position_metadata(temp_config, repo) -> None:
    broker = FakeBroker(prices={"WIPRO": 100.0, "TCS": 120.0})

    buy = approved_proposal(repo, "WIPRO", Action.BUY, 100, 100.0)
    OrderExecutor(broker, temp_config, repo).execute([buy])
    assert "WIPRO" in repo.position_meta()

    sell = approved_proposal(repo, "WIPRO", Action.SELL, 100, 100.0)
    OrderExecutor(broker, temp_config, repo).execute([sell])

    assert "WIPRO" not in repo.position_meta()


def test_a_sale_records_realised_pnl_against_average_cost(temp_config, repo) -> None:
    broker = FakeBroker(prices={"WIPRO": 100.0})
    buy = approved_proposal(repo, "WIPRO", Action.BUY, 100, 100.0)
    OrderExecutor(broker, temp_config, repo).execute([buy])

    broker = FakeBroker(prices={"WIPRO": 120.0})
    sell = approved_proposal(repo, "WIPRO", Action.SELL, 100, 120.0)
    OrderExecutor(broker, temp_config, repo).execute([sell])

    sale = next(t for t in repo.trades() if t["action"] == "SELL")
    # Bought around 100.30 (limit above market), sold around 119.65.
    assert 1700 < sale["realised_pnl"] < 2100


def test_dry_run_orders_are_marked_as_simulated(temp_config, repo) -> None:
    broker = FakeBroker(prices={"WIPRO": 100.0}, dry_run=True)
    proposal = approved_proposal(repo)

    summary = OrderExecutor(broker, temp_config, repo).execute([proposal])

    assert summary.outcomes[0].dry_run
    [trade] = repo.trades()
    assert trade["dry_run"] == 1
    assert trade["status"] == "dry_run"


def test_execute_approved_picks_up_the_queue_itself(temp_config, repo) -> None:
    broker = FakeBroker(prices={"WIPRO": 100.0})
    approved_proposal(repo)

    summary = OrderExecutor(broker, temp_config, repo).execute_approved(
        technicals={"WIPRO": make_technical("WIPRO")}
    )

    assert len(summary.placed) == 1
    assert repo.approved_proposals() == []


def test_nothing_approved_means_nothing_placed(temp_config, repo) -> None:
    broker = FakeBroker(prices={"WIPRO": 100.0})

    summary = OrderExecutor(broker, temp_config, repo).execute_approved()

    assert broker.orders == []
    assert summary.describe() == "No approved trades to execute."


# ----------------------------------------------------------------------
# Full daily cycle
# ----------------------------------------------------------------------
@pytest.fixture
def manager(temp_config, monkeypatch):
    """A FundManager with the network stages stubbed out.

    Prices, fundamentals and news all reach the internet, so each is replaced
    with a small deterministic fixture. Everything downstream - scoring, risk,
    triggers, decision, journalling - is the real implementation.
    """
    from src.orchestrator import FundManager

    symbols = [
        "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "ITC", "LT",
        "SUNPHARMA", "MARUTI", "TITAN", "WIPRO", "CIPLA", "TATASTEEL",
        "HINDUNILVR", "ULTRACEMCO",
    ]
    # The metrics must differ across names or every z-score is zero and the
    # screen produces no ranking at all. Cheapest and highest-quality first.
    technicals = {
        symbol: make_technical(symbol, volatility=18.0 + index, rsi=45.0 + index)
        for index, symbol in enumerate(symbols)
    }
    fundamentals = {
        symbol: make_fundamental(
            symbol,
            pe=14.0 + index * 2.0,
            roe=28.0 - index,
            debt_to_equity=0.2 + index * 0.1,
            revenue_growth=24.0 - index,
        )
        for index, symbol in enumerate(symbols)
    }

    broker = FakeBroker(
        holdings=[
            BrokerHolding(symbol="TCS", quantity=500, average_price=90.0, last_price=100.0),
            BrokerHolding(symbol="INFY", quantity=400, average_price=95.0, last_price=100.0),
        ],
        cash=400_000.0,
        prices={s: 100.0 for s in symbols},
    )

    instance = FundManager(config=temp_config, broker=broker)

    monkeypatch.setattr(instance, "_load_technicals", lambda: technicals)
    monkeypatch.setattr(
        instance,
        "_load_fundamentals",
        lambda technicals, portfolio, refresh, max_candidates: fundamentals,
    )
    monkeypatch.setattr(
        instance, "_build_digest", lambda portfolio, candidates, as_of: None
    )
    monkeypatch.setattr(
        instance, "_analyse_risk", lambda portfolio, fundamentals, technicals: None
    )
    monkeypatch.setattr(instance._prices, "benchmark_series", lambda: None)
    monkeypatch.setattr(instance._prices, "latest_data_date", lambda: date.today())
    return instance


def test_a_full_run_journals_everything(manager, repo) -> None:
    result = manager.analyse()

    run = repo.latest_run()
    assert run["status"] == "complete"
    assert run["id"] == result.run_id
    assert run["memo"]
    assert repo.nav_history()
    assert len(repo.latest_position_snapshot()) == 2


def test_a_run_with_no_llm_still_produces_a_memo(manager) -> None:
    result = manager.analyse()

    assert not result.decision.llm_used
    assert "rules only" in result.decision.memo


def test_holdings_appear_in_the_portfolio_with_journal_metadata(manager) -> None:
    result = manager.analyse()

    assert sorted(result.portfolio.symbols) == ["INFY", "TCS"]
    tcs = result.portfolio.get("TCS")
    assert tcs.sector
    assert tcs.company_name
    assert tcs.return_pct == pytest.approx((100 / 90 - 1) * 100)


def test_the_scoring_and_candidate_stages_ran(manager) -> None:
    result = manager.analyse()

    assert len(result.scores) == 15
    assert result.candidates
    # Held names are never proposed as new buys.
    assert not {c.symbol for c in result.candidates} & {"TCS", "INFY"}


def test_excess_cash_raises_a_deployment_trigger(manager) -> None:
    """The fund holds 82% cash, which an equity mandate should not."""
    result = manager.analyse()

    assert any(t.trigger_type.value == "initial_build" or
               t.trigger_type.value == "underweight_deploy"
               for t in result.triggers)


def test_a_briefing_file_is_written_for_the_record(manager, temp_config) -> None:
    result = manager.analyse()

    path = temp_config.briefing_dir / f"briefing_{result.as_of.isoformat()}.txt"
    assert path.exists()
    briefing = path.read_text(encoding="utf-8")
    assert "DAILY BRIEFING" in briefing
    assert "TCS" in briefing


def test_a_broken_news_feed_does_not_stop_the_run(manager, monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise RuntimeError("all feeds unreachable")

    monkeypatch.setattr(manager, "_build_digest", explode)

    with pytest.raises(RuntimeError):
        manager.analyse()


def test_the_news_stage_swallows_its_own_failures(manager, monkeypatch) -> None:
    """_build_digest is the stage that must absorb feed errors, and does."""
    from src.orchestrator import FundManager

    monkeypatch.setattr(
        "src.news.ingest.NewsIngestor.fetch",
        lambda self, **_: (_ for _ in ()).throw(RuntimeError("feed down")),
    )
    digest = FundManager._build_digest(
        manager, manager._portfolios.build(manager.broker), [], date.today()
    )

    assert digest is None


def test_risk_analytics_failure_is_survivable(temp_config, monkeypatch) -> None:
    from src.orchestrator import FundManager
    from src.models import PortfolioState

    manager = FundManager(config=temp_config, broker=FakeBroker())
    monkeypatch.setattr(
        manager._risk_analytics,
        "analyse",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("singular matrix")),
    )

    profile = manager._analyse_risk(
        PortfolioState(as_of=date.today(), cash=0.0), {}, {}
    )

    assert profile is None


def test_two_runs_on_the_same_day_do_not_duplicate_nav(manager, repo) -> None:
    manager.analyse()
    manager.analyse()

    assert len(repo.nav_history()) == 1
    assert len(repo.recent_runs()) == 2


def test_close_of_day_records_the_closing_nav(manager, repo, monkeypatch) -> None:
    manager.analyse()
    monkeypatch.setattr(manager._prices, "snapshots", lambda symbols: {})
    monkeypatch.setattr(manager._prices, "clear_cache", lambda: None)

    portfolio = manager.close_of_day()

    assert portfolio.total_value > 0
    assert len(repo.nav_history()) == 1


def test_close_of_day_keeps_the_morning_analytics(manager, repo, monkeypatch) -> None:
    """Re-marking at the close must not erase the day's factor and risk metrics."""
    manager.analyse()
    before = {r["symbol"]: r["composite_score"] for r in repo.latest_position_snapshot()}
    assert any(v is not None for v in before.values())

    monkeypatch.setattr(manager._prices, "snapshots", lambda symbols: {})
    monkeypatch.setattr(manager._prices, "clear_cache", lambda: None)
    manager.close_of_day()

    after = {r["symbol"]: r["composite_score"] for r in repo.latest_position_snapshot()}
    assert after == before


# ----------------------------------------------------------------------
# Approval waiting
# ----------------------------------------------------------------------
def test_waiting_returns_immediately_when_the_queue_is_empty(manager) -> None:
    assert manager.wait_for_approval(timeout_minutes=1) is True


def test_undecided_proposals_expire_rather_than_execute(manager, repo) -> None:
    """The fund must never trade on silence."""
    run_id = repo.start_run(date.today(), False, 1_000_000.0, 500_000.0)
    repo.save_proposals(
        run_id,
        [
            TradeProposal(
                symbol="WIPRO",
                action=Action.BUY,
                quantity=10,
                rationale="test",
                reference_price=100.0,
            )
        ],
    )

    decided = manager.wait_for_approval(timeout_minutes=0, poll_seconds=0)

    assert decided is False
    assert repo.pending_proposals() == []
    assert repo.approved_proposals() == []
    proposal = repo.proposals_for_run(run_id)[0]
    assert proposal["status"] == "expired"
