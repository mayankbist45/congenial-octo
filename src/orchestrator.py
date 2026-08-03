"""Daily cycle orchestration.

Wires the modules into the sequence a portfolio manager actually follows:

    prices -> holdings -> fundamentals -> factor scores -> risk analytics
    -> news -> triggers -> decision -> journal -> [your approval] -> execution

Each stage is defensive about the one before it. A failed news feed, a missing
benchmark series or an LLM outage degrades the run rather than aborting it,
because a fund that stops monitoring the day a data source breaks is worse than
one that monitors with less information. The one thing never degraded is the risk
engine: no trade reaches the approval queue without passing it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from src.broker.base import BrokerClient, BrokerError
from src.broker.factory import build_broker
from src.config import Config, get_config
from src.decision.engine import DecisionEngine, is_deep_review_day
from src.execution.executor import ExecutionSummary, OrderExecutor
from src.earnings.service import EarningsService
from src.fundamentals.service import FundamentalsService
from src.fundamentals.reports import QuarterlyReportsService
from src.journal.db import init_db
from src.journal.performance import PerformanceCalculator
from src.journal.portfolio import PortfolioBuilder
from src.journal.repository import JournalRepository
from src.logging_setup import get_logger
from src.market_data.liquid import LiquidFundService
from src.market_data.prices import PriceService
from src.models import (
    DailyDecision,
    EarningsDigest,
    FactorScores,
    FundamentalSnapshot,
    NewsDigest,
    PortfolioRiskProfile,
    PortfolioState,
    QuarterlyReportContext,
    TechnicalSnapshot,
    Trigger,
)
from src.news.digest import DigestBuilder
from src.news.holdings_news import HoldingsNewsFetcher
from src.news.ingest import NewsIngestor
from src.quant.scoring import ScoringEngine
from src.risk.engine import RiskEngine
from src.risk.triggers import TriggerEngine
from src.risk_analytics.analyzer import RiskAnalyzer
from src.universe import load_universe

logger = get_logger(__name__)


class AutoApprovalRefused(RuntimeError):
    """Raised when unattended approval is asked for outside paper trading."""


@dataclass
class RunResult:
    """Everything one analysis pass produced."""

    run_id: int
    as_of: date
    decision: DailyDecision
    portfolio: PortfolioState
    technicals: Dict[str, TechnicalSnapshot] = field(default_factory=dict)
    fundamentals: Dict[str, FundamentalSnapshot] = field(default_factory=dict)
    scores: Dict[str, FactorScores] = field(default_factory=dict)
    candidates: List[FactorScores] = field(default_factory=list)
    triggers: List[Trigger] = field(default_factory=list)
    risk_profile: Optional[PortfolioRiskProfile] = None
    digest: Optional[NewsDigest] = None
    quarterly_context: Optional[QuarterlyReportContext] = None
    earnings: Optional[EarningsDigest] = None

    @property
    def proposal_count(self) -> int:
        return len(self.decision.proposals)


class FundManager:
    """Runs the fund's daily cycle for a single persona."""

    def __init__(
        self,
        config: Optional[Config] = None,
        broker: Optional[BrokerClient] = None,
        force_simulated: bool = False,
    ):
        self._config = config or get_config()
        self._persona_id = self._config.persona_id
        init_db(self._config)

        self._broker = broker or build_broker(self._config, force_simulated)
        self._universe = load_universe(self._config)
        self._prices = PriceService(self._config)
        self._fundamentals = FundamentalsService(self._config)
        self._quarterly_reports = QuarterlyReportsService(self._config)
        self._scoring = ScoringEngine(self._config, self._universe)
        self._risk_analytics = RiskAnalyzer(self._config, self._prices, self._universe)
        self._triggers = TriggerEngine(self._config)
        self._risk = RiskEngine(self._config, self._universe)
        self._decision = DecisionEngine(
            self._config, risk_engine=self._risk, universe=self._universe
        )
        self._earnings = EarningsService(self._config, self._universe)
        self._portfolios = PortfolioBuilder(self._config, self._universe)
        self._repo = JournalRepository(self._config)

    @property
    def persona_id(self) -> str:
        return self._persona_id

    @property
    def broker(self) -> BrokerClient:
        return self._broker

    @property
    def repository(self) -> JournalRepository:
        return self._repo

    # ------------------------------------------------------------------
    # Pre-market analysis
    # ------------------------------------------------------------------
    def analyse(
        self,
        as_of: Optional[date] = None,
        skip_news: bool = False,
        refresh_fundamentals: bool = False,
        max_candidates: Optional[int] = None,
    ) -> RunResult:
        """Run the full analysis and journal the result."""
        started = time.time()
        self._connect()

        logger.info("=" * 66)
        logger.info("PERSONA: %s", self._persona_id)
        logger.info("STAGE 1/9  Price history and technical indicators")
        technicals = self._load_technicals()

        logger.info("STAGE 2/9  Portfolio state")
        portfolio = self._portfolios.build(self._broker, technicals, as_of=as_of)
        as_of = as_of or self._prices.latest_data_date() or date.today()
        portfolio.as_of = as_of
        deep_review = is_deep_review_day(self._config, as_of)

        logger.info("STAGE 3/9  Fundamentals")
        fundamentals = self._load_fundamentals(
            technicals, portfolio, refresh_fundamentals, max_candidates
        )

        logger.info("STAGE 4/9  Factor scoring")
        scores = self._scoring.score(fundamentals, technicals)
        candidates = self._scoring.buy_candidates(
            scores, exclude=portfolio.symbols, limit=20
        )

        logger.info("STAGE 5/9  Quarterly reports")
        quarterly_context = self._load_quarterly_reports(
            portfolio, candidates, fundamentals, as_of
        )

        logger.info("STAGE 6/9  Risk analytics")
        risk_profile = self._analyse_risk(portfolio, fundamentals, technicals)
        
        logger.info("STAGE 6b   Quarterly earnings")
        earnings = self._build_earnings(portfolio, candidates, as_of)

        logger.info("STAGE 7/9  News")
        digest = None if skip_news else self._build_digest(portfolio, candidates, as_of)

        logger.info("STAGE 8/9  Sell triggers and portfolio limits")
        triggers = self._evaluate_triggers(
            portfolio, scores, fundamentals, technicals, digest, risk_profile,
            candidates, deep_review, quarterly_context, earnings)
        

        logger.info("STAGE 9/9  Decision")
        decision = self._decision.decide(
            portfolio=portfolio,
            scores=scores,
            fundamentals=fundamentals,
            technicals=technicals,
            triggers=triggers,
            candidates=candidates,
            digest=digest,
            earnings=earnings,
            risk_profile=risk_profile,
            quarterly_context=quarterly_context,
            as_of=as_of,
        )

        run_id = self._journal(
            decision, portfolio, scores, risk_profile, digest, as_of, deep_review
        )

        logger.info("=" * 66)
        logger.info(
            "Run complete in %.0fs: %d proposal(s) awaiting approval",
            time.time() - started,
            len(decision.proposals),
        )
        self._log_summary(decision)

        return RunResult(
            run_id=run_id,
            as_of=as_of,
            decision=decision,
            portfolio=portfolio,
            technicals=technicals,
            fundamentals=fundamentals,
            scores=scores,
            candidates=candidates,
            triggers=triggers,
            risk_profile=risk_profile,
            digest=digest,
            quarterly_context=quarterly_context,
            earnings=earnings,
        )

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------
    def _connect(self) -> None:
        try:
            if not self._broker.connect():
                logger.warning(
                    "Broker %s did not connect; continuing with journalled state",
                    self._broker.name,
                )
        except BrokerError as exc:
            logger.error("Broker connection failed: %s", exc)

    def _load_technicals(self) -> Dict[str, TechnicalSnapshot]:
        symbols = self._universe.symbols
        technicals = self._prices.snapshots(symbols)
        # The benchmark is loaded here so the risk analytics and NAV recording
        # later find it already cached.
        self._prices.benchmark_series()
        return technicals

    def _load_fundamentals(
        self,
        technicals: Dict[str, TechnicalSnapshot],
        portfolio: PortfolioState,
        refresh: bool,
        max_candidates: Optional[int],
    ) -> Dict[str, FundamentalSnapshot]:
        """Fetch fundamentals for names the fund could actually trade.

        Scraping is slow, so the universe is first cut by the liquidity floor:
        a name the fund cannot exit is not worth researching. Current holdings
        are always included regardless, and always refreshed, because a stale
        fundamental on something owned is what makes a sell trigger miss.
        """
        floor = float(self._config.get("universe.min_avg_daily_turnover_inr", 0) or 0)
        liquid = [
            symbol
            for symbol, technical in technicals.items()
            if technical.has_price
            and (technical.avg_daily_turnover or 0) >= floor
        ]
        # Rank by liquidity so an optional cap keeps the most tradeable names.
        liquid.sort(
            key=lambda s: technicals[s].avg_daily_turnover or 0.0, reverse=True
        )
        if max_candidates:
            liquid = liquid[:max_candidates]

        wanted = list(dict.fromkeys(liquid + portfolio.symbols))
        logger.info(
            "Researching %d of %d priced names (liquidity floor Rs %s/day)",
            len(wanted),
            len(technicals),
            f"{floor:,.0f}",
        )

        fundamentals = self._fundamentals.get_many(wanted, force_refresh=refresh)

        if portfolio.symbols and not refresh:
            # Holdings are refreshed every run: their fundamentals drive the
            # deterioration and valuation-stretch sell triggers.
            fundamentals.update(
                self._fundamentals.refresh_symbols(portfolio.symbols)
            )
        self._fundamentals.close()
        return fundamentals

    def _load_quarterly_reports(
        self,
        portfolio: PortfolioState,
        candidates: List[FactorScores],
        fundamentals: Dict[str, FundamentalSnapshot],
        as_of: date,
    ) -> QuarterlyReportContext:
        try:
            symbols = list(
                dict.fromkeys(portfolio.symbols + [candidate.symbol for candidate in candidates])
            )
            context = self._quarterly_reports.detect_new_reports(symbols, as_of)
            for symbol, history in context.histories.items():
                if symbol in fundamentals and history:
                    fundamentals[symbol].latest_quarter_label = history[0].fiscal_period
            return context
        except Exception as exc:
            logger.error("Quarterly report stage failed: %s", exc, exc_info=True)
            return QuarterlyReportContext(errors={"__global__": str(exc)})

    def _analyse_risk(
        self,
        portfolio: PortfolioState,
        fundamentals: Dict[str, FundamentalSnapshot],
        technicals: Dict[str, TechnicalSnapshot],
    ) -> Optional[PortfolioRiskProfile]:
        try:
            return self._risk_analytics.analyse(
                portfolio,
                fundamentals,
                technicals,
                universe_symbols=list(technicals.keys()),
            )
        except Exception as exc:
            # Risk analytics are informational: their absence weakens the
            # briefing but must not stop the hard rules from running.
            logger.error("Risk analytics failed: %s", exc, exc_info=True)
            return None

    def _build_digest(
        self,
        portfolio: PortfolioState,
        candidates: List[FactorScores],
        as_of: date,
    ) -> Optional[NewsDigest]:
        try:
            articles = NewsIngestor(self._config).fetch()
        except Exception as exc:
            logger.error("News ingestion failed: %s", exc)
            articles = []

        holdings_articles: Dict[str, list] = {}
        if portfolio.symbols:
            try:
                holdings_articles = HoldingsNewsFetcher(self._config).fetch_for(
                    portfolio.symbols
                )
            except Exception as exc:
                logger.error("Targeted holdings news failed: %s", exc)

        if not articles and not holdings_articles:
            logger.warning("No news retrieved; the run proceeds without a digest")
            return None

        try:
            return DigestBuilder(self._config).build(
                articles=articles,
                holdings=portfolio.symbols,
                watchlist=[c.symbol for c in candidates],
                holdings_articles=holdings_articles,
                as_of=as_of,
            )
        except Exception as exc:
            logger.error("Digest construction failed: %s", exc)
            return None

    def _build_earnings(
        self,
        portfolio: PortfolioState,
        candidates: List[FactorScores],
        as_of: date,
    ) -> Optional[EarningsDigest]:
        try:
            return self._earnings.build(
                portfolio=portfolio,
                candidate_symbols=[c.symbol for c in candidates],
                as_of=as_of,
            )
        except Exception as exc:
            logger.error("Earnings pipeline failed: %s", exc, exc_info=True)
            return None

    def _evaluate_triggers(
        self,
        portfolio: PortfolioState,
        scores: Dict[str, FactorScores],
        fundamentals: Dict[str, FundamentalSnapshot],
        technicals: Dict[str, TechnicalSnapshot],
        digest: Optional[NewsDigest],
        risk_profile: Optional[PortfolioRiskProfile],
        candidates: List[FactorScores],
        deep_review: bool,
        quarterly_context: Optional[QuarterlyReportContext],
        earnings: Optional[EarningsDigest] = None,
    ) -> List[Trigger]:
        triggers = self._triggers.evaluate(
            portfolio=portfolio,
            scores=scores,
            fundamentals=fundamentals,
            technicals=technicals,
            digest=digest,
            risk_profile=risk_profile,
            best_candidate=candidates[0] if candidates else None,
            is_deep_review=deep_review,
            quarterly_context=quarterly_context,
        )
        triggers.extend(
            self._earnings.triggers_for(
                earnings,
                portfolio,
                [c.symbol for c in candidates],
                deep_review,
            )
        )
        triggers.extend(self._risk.cash_deployment_triggers(portfolio, candidates))
        return triggers

    # ------------------------------------------------------------------
    # Journalling
    # ------------------------------------------------------------------
    def _journal(
        self,
        decision: DailyDecision,
        portfolio: PortfolioState,
        scores: Dict[str, FactorScores],
        risk_profile: Optional[PortfolioRiskProfile],
        digest: Optional[NewsDigest],
        as_of: date,
        deep_review: bool,
    ) -> int:
        run_id = self._repo.start_run(
            run_date=as_of,
            is_deep_review=deep_review,
            portfolio_value=portfolio.total_value,
            cash=portfolio.cash,
        )
        self._repo.save_proposals(run_id, decision.proposals)
        self._repo.save_rejected(run_id, decision.rejected)
        self._repo.record_position_snapshot(as_of, portfolio, scores, risk_profile)
        self._record_nav(as_of, portfolio)

        if risk_profile is not None:
            self._repo.record_risk_profile(as_of, risk_profile)
        if digest is not None:
            self._repo.record_digest(digest)

        self._repo.finish_run(
            run_id=run_id,
            memo=decision.memo,
            llm_used=decision.llm_used,
            turnover_used_pct=decision.turnover_used_pct,
        )
        return run_id

    def _record_nav(self, as_of: date, portfolio: PortfolioState) -> None:
        """Record the day's NAV alongside the benchmark, for like-for-like returns."""
        history = self._repo.nav_history()
        previous = next(
            (row for row in reversed(history) if str(row["run_date"]) < as_of.isoformat()),
            None,
        )

        benchmark_close: Optional[float] = None
        benchmark_return: Optional[float] = None
        series = self._prices.benchmark_series()
        if series is not None and len(series) >= 2:
            benchmark_close = float(series.iloc[-1])
            if previous and previous["benchmark_close"]:
                prior = float(previous["benchmark_close"])
                if prior > 0:
                    benchmark_return = (benchmark_close / prior - 1.0) * 100.0

        daily_return: Optional[float] = None
        if previous and float(previous["total_value"]) > 0:
            daily_return = (
                portfolio.total_value / float(previous["total_value"]) - 1.0
            ) * 100.0

        self._repo.record_nav(
            run_date=as_of,
            portfolio=portfolio,
            benchmark_close=benchmark_close,
            daily_return_pct=daily_return,
            benchmark_return_pct=benchmark_return,
        )

    @staticmethod
    def _log_summary(decision: DailyDecision) -> None:
        if not decision.proposals:
            logger.info("No trades proposed today.")
            return
        for proposal in decision.proposals:
            logger.info(
                "  PROPOSED %s %d x %s (%s) - %s",
                proposal.action.value,
                proposal.quantity,
                proposal.symbol,
                f"Rs {proposal.estimated_value:,.0f}"
                if proposal.estimated_value
                else "value unknown",
                proposal.rationale[:110],
            )

    # ------------------------------------------------------------------
    # Approval and execution
    # ------------------------------------------------------------------
    def pending_count(self) -> int:
        return len(self._repo.pending_proposals())

    @property
    def auto_approval_enabled(self) -> bool:
        return bool(self._config.get("execution.auto_approve", False))

    def auto_approve(self) -> int:
        """Approve every pending proposal without a human, for unattended runs.

        Paper trading only. The guard is enforced here rather than at the command
        line so that no caller can reach live order placement without a person
        having looked at the trade. Approvals are journalled as ``auto`` so a
        later review can tell them apart from anything a human agreed to.
        """
        if not self._config.secrets.dry_run:
            raise AutoApprovalRefused(
                "auto-approval is limited to paper trading, and DRY_RUN is false"
            )

        pending = self._repo.pending_proposals()
        if not pending:
            logger.info("Nothing awaiting approval")
            return 0

        for proposal in pending:
            self._repo.set_proposal_status(
                proposal["id"], "approved", decided_by="auto"
            )

        logger.warning(
            "AUTO MODE: approved %d proposal(s) with no human review "
            "(simulated orders only)",
            len(pending),
        )
        return len(pending)

    def wait_for_approval(
        self, timeout_minutes: int = 120, poll_seconds: int = 15
    ) -> bool:
        """Block until every pending proposal has been decided, or time out.

        Returns True when the queue was cleared by a human. A timeout is not an
        error: undecided proposals simply expire, which is the safe default. The
        fund never trades on silence.
        """
        pending = self.pending_count()
        if pending == 0:
            logger.info("Nothing awaiting approval")
            return True

        host = self._config.get("dashboard.host", "127.0.0.1")
        port = self._config.get("dashboard.port", 8000)
        logger.info(
            "%d proposal(s) awaiting your approval at http://%s:%s/proposals "
            "(waiting up to %d minutes)",
            pending,
            host,
            port,
            timeout_minutes,
        )

        deadline = time.time() + timeout_minutes * 60
        last_reported = pending
        while time.time() < deadline:
            time.sleep(poll_seconds)
            remaining = self.pending_count()
            if remaining == 0:
                logger.info("All proposals decided")
                return True
            if remaining != last_reported:
                logger.info("%d proposal(s) still awaiting a decision", remaining)
                last_reported = remaining

        logger.warning(
            "Approval window closed with %d proposal(s) undecided; they expire "
            "unactioned and will be reconsidered on the next run",
            self.pending_count(),
        )
        self._expire_pending()
        return False

    def _expire_pending(self) -> None:
        for proposal in self._repo.pending_proposals():
            self._repo.set_proposal_status(
                proposal["id"],
                "expired",
                decided_by="timeout",
                rejection_reason="approval window closed without a decision",
            )

    def execute_approved(
        self,
        technicals: Optional[Dict[str, TechnicalSnapshot]] = None,
        fundamentals: Optional[Dict[str, FundamentalSnapshot]] = None,
    ) -> ExecutionSummary:
        """Place every approved order."""
        approved = self._repo.approved_proposals()
        if not approved:
            logger.info("No approved proposals to execute")
            return ExecutionSummary()

        self._connect()
        symbols = [p["symbol"] for p in approved]
        if technicals is None:
            technicals = self._prices.snapshots(symbols)
        if fundamentals is None:
            # Entry P/E is captured at execution time, so buys need fundamentals.
            buys = [
                p["symbol"] for p in approved if str(p["action"]).upper() in ("BUY", "ADD")
            ]
            fundamentals = self._fundamentals.get_many(buys) if buys else {}

        executor = OrderExecutor(self._broker, self._config, self._repo)
        summary = executor.execute(approved, technicals, fundamentals)

        for outcome in summary.outcomes:
            logger.info(
                "  %s %s %d x %s%s",
                "PLACED" if outcome.placed else "NOT PLACED",
                outcome.action,
                outcome.quantity,
                outcome.symbol,
                f" - {outcome.error}" if outcome.error else "",
            )

        # Dashboard overview/holdings read position_snapshots and nav_history.
        # Refresh them here so fills show up immediately, not only at close.
        if summary.placed:
            try:
                self._refresh_book(
                    as_of=self._prices.latest_data_date() or date.today(),
                    technicals=technicals,
                )
            except Exception as exc:
                logger.error("Post-execution book refresh failed: %s", exc, exc_info=True)

        return summary

    def _refresh_book(
        self,
        as_of: Optional[date] = None,
        technicals: Optional[Dict[str, TechnicalSnapshot]] = None,
    ) -> PortfolioState:
        """Rebuild the portfolio from the broker/journal and journal the marks."""
        holdings = [p["symbol"] for p in self._repo.reconstruct_positions()]
        if technicals is None or any(symbol not in (technicals or {}) for symbol in holdings):
            technicals = self._prices.snapshots(holdings) if holdings else {}
        portfolio = self._portfolios.build(self._broker, technicals, as_of=as_of)
        as_of = as_of or self._prices.latest_data_date() or date.today()
        portfolio.as_of = as_of
        self._repo.record_position_snapshot(as_of, portfolio)
        self._record_nav(as_of, portfolio)
        logger.info(
            "Book refreshed for %s: %d position(s), total Rs %s",
            as_of.isoformat(),
            len(portfolio.positions),
            f"{portfolio.total_value:,.0f}",
        )
        return portfolio

    # ------------------------------------------------------------------
    # Post-market
    # ------------------------------------------------------------------
    def close_of_day(self, as_of: Optional[date] = None) -> PortfolioState:
        """Re-mark the portfolio at the closing prices and journal the NAV.

        Run after the market closes. The pre-market NAV was struck on the
        previous close and on holdings that had not yet traded; this is the
        figure the performance record should keep.
        """
        self._connect()
        self._prices.clear_cache()

        as_of = as_of or self._prices.latest_data_date() or date.today()
        portfolio = self._refresh_book(as_of=as_of)

        self._record_liquid_position(as_of, portfolio)

        report = PerformanceCalculator(self._config).report()
        logger.info(
            "Close of %s: fund Rs %s (%s vs %s benchmark), drawdown %s",
            as_of.isoformat(),
            f"{portfolio.total_value:,.0f}",
            f"{report.total_return_pct:+.2f}%"
            if report.total_return_pct is not None
            else "n/a",
            f"{report.benchmark_return_pct:+.2f}%"
            if report.benchmark_return_pct is not None
            else "n/a",
            f"{report.current_drawdown_pct:.2f}%"
            if report.current_drawdown_pct is not None
            else "n/a",
        )
        return portfolio

    def _record_liquid_position(
        self, as_of: date, portfolio: PortfolioState
    ) -> None:
        """If liquid fund parking is enabled, record the parked-cash position."""
        if not self._config.get("liquid_fund.enabled", False):
            return

        try:
            liquid_svc = LiquidFundService(self._config)
            nav_info = liquid_svc.current_nav()
            if nav_info is None:
                logger.warning("Could not fetch liquid fund NAV; skipping")
                return

            min_pct = float(
                self._config.get("liquid_fund.min_cash_for_parking_pct", 10.0)
            )
            total_value = portfolio.total_value
            if total_value <= 0:
                return

            min_cash = total_value * min_pct / 100.0
            parkable = max(0.0, portfolio.cash - min_cash)
            if parkable <= 0:
                logger.info("Cash below parking threshold; no liquid fund allocation")
                return

            units = parkable / nav_info.nav if nav_info.nav > 0 else 0.0
            market_value = units * nav_info.nav

            self._repo.record_liquid_position(
                run_date=as_of,
                scheme_code=nav_info.scheme_code,
                scheme_name=nav_info.scheme_name,
                units=units,
                nav=nav_info.nav,
                market_value=market_value,
                daily_return_pct=nav_info.daily_return_pct,
            )
            logger.info(
                "Liquid fund: Rs %s parked in %s (NAV %.4f, daily %.4f%%)",
                f"{market_value:,.0f}",
                nav_info.scheme_name or nav_info.scheme_code,
                nav_info.nav,
                nav_info.daily_return_pct,
            )
        except Exception as exc:
            logger.warning("Liquid fund recording failed: %s", exc)

    def disconnect(self) -> None:
        try:
            self._earnings.close()
        except Exception as exc:
            logger.debug("Earnings disconnect: %s", exc)
        try:
            self._broker.disconnect()
        except Exception as exc:
            logger.debug("Broker disconnect: %s", exc)
