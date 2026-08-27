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

import json
import time
from dataclasses import dataclass, field
from datetime import date
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.broker.base import BrokerClient, BrokerError
from src.connectors.registry import build_broker
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
from src.logging_setup import bind_log_context, get_logger, make_run_label
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
        bind_log_context(
            persona_id=self._persona_id,
            run_label=make_run_label("analyse", self._persona_id),
            command="analyse",
            market_id=self._config.market_id,
        )
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
        audit: List[Dict[str, Any]] = []
        self._connect()

        logger.info("=" * 66)
        logger.info("PERSONA: %s", self._persona_id)
        logger.info("STAGE 1/9  Price history and technical indicators")
        technicals = self._audit_call(audit, "prices", "full_universe", self._load_technicals)

        logger.info("STAGE 2/9  Portfolio state")
        portfolio = self._audit_call(
            audit, "portfolio", "full_universe",
            lambda: self._portfolios.build(self._broker, technicals, as_of=as_of),
        )
        as_of = as_of or self._prices.latest_data_date() or date.today()
        portfolio.as_of = as_of
        deep_review = is_deep_review_day(self._config, as_of)

        logger.info("STAGE 3/9  Fundamentals")
        fundamentals = self._audit_call(
            audit, "fundamentals", "full_universe",
            lambda: self._load_fundamentals(
                technicals, portfolio, refresh_fundamentals, max_candidates
            ),
            lambda value: self._fundamentals_details(value, len(technicals)),
        )

        logger.info("STAGE 4/9  Factor scoring")
        scores, candidates = self._audit_call(
            audit, "scoring", "full_universe",
            lambda: self._score_candidates(fundamentals, technicals, portfolio),
            self._scoring_details,
        )

        logger.info("STAGE 5/9  Quarterly reports")
        quarterly_context = self._audit_call(
            audit, "quarterly_reports", "full_universe",
            lambda: self._load_quarterly_reports(portfolio, candidates, fundamentals, as_of),
        )

        logger.info("STAGE 6/9  Risk analytics")
        risk_profile = self._audit_call(
            audit, "risk_analytics", "full_universe",
            lambda: self._analyse_risk(portfolio, fundamentals, technicals),
            lambda value: self._base_details("full_universe", len(portfolio.symbols), len(portfolio.symbols), portfolio.symbols,
                "Portfolio risk analytics completed." if value else "Risk analytics was unavailable."),
        )
        
        logger.info("STAGE 6b   Quarterly earnings")
        earnings = self._audit_call(
            audit, "earnings", "full_universe",
            lambda: self._build_earnings(portfolio, candidates, as_of),
        )

        logger.info("STAGE 7/9  News")
        digest = self._audit_call(
            audit, "news", "full_universe",
            (lambda: None) if skip_news else lambda: self._build_digest(portfolio, candidates, as_of),
            lambda value: self._news_details(value, enabled=not skip_news),
            status="skipped" if skip_news else "completed",
        )

        logger.info("STAGE 8/9  Sell triggers and portfolio limits")
        triggers = self._audit_call(
            audit, "triggers", "full_universe",
            lambda: self._evaluate_triggers(
                portfolio, scores, fundamentals, technicals, digest, risk_profile,
                candidates, deep_review, quarterly_context, earnings),
            self._trigger_details,
        )
        

        logger.info("STAGE 9/9  Decision")
        decision = self._audit_call(
            audit, "decision", "full_universe",
            lambda: self._decision.decide(
                portfolio=portfolio, scores=scores, fundamentals=fundamentals,
                technicals=technicals, triggers=triggers, candidates=candidates,
                digest=digest, earnings=earnings, risk_profile=risk_profile,
                quarterly_context=quarterly_context, as_of=as_of,
            ),
            lambda value: self._decision_details(value, candidates, triggers),
        )

        run_id = self._journal(
            decision, portfolio, scores, risk_profile, digest, as_of, deep_review,
            stage_status=self._outcome_status(
                decision, candidates, digest, pack_stale=False, mode="full_universe"
            ),
            audit_events=audit,
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

    def weekly_research(
        self,
        as_of: Optional[date] = None,
        refresh_fundamentals: bool = False,
        max_candidates: Optional[int] = None,
        trigger_source: str = "manual",
    ) -> RunResult:
        """Refresh the screened universe and persist a reusable candidate pack."""
        audit: List[Dict[str, Any]] = []
        self._connect()
        technicals = self._audit_call(audit, "prices", "full_universe", self._load_technicals)
        portfolio = self._audit_call(
            audit, "portfolio", "full_universe",
            lambda: self._portfolios.build(self._broker, technicals, as_of=as_of),
        )
        as_of = as_of or self._prices.latest_data_date() or date.today()
        portfolio.as_of = as_of
        deep_review = is_deep_review_day(self._config, as_of)

        fundamentals = self._audit_call(
            audit, "fundamentals", "full_universe",
            lambda: self._load_fundamentals(technicals, portfolio, refresh_fundamentals, max_candidates),
            lambda value: self._fundamentals_details(value, len(technicals)),
        )
        scores, candidates = self._audit_call(
            audit, "scoring", "full_universe",
            lambda: self._score_candidates(fundamentals, technicals, portfolio), self._scoring_details,
        )
        risk_profile = self._audit_call(
            audit, "risk_analytics", "full_universe",
            lambda: self._analyse_risk(portfolio, fundamentals, technicals),
        )
        market_regime = (
            risk_profile.market_regime.regime
            if risk_profile and risk_profile.market_regime
            else None
        )
        candidates = self._apply_regime_policy(candidates, market_regime)
        pack_id = self._audit_call(
            audit, "candidate_pack_refresh", "full_universe",
            lambda: self._repo.save_candidate_pack(as_of, candidates),
            lambda value: {**self._base_details("full_universe", len(scores), len(candidates), [c.symbol for c in candidates],
                f"Refreshed weekly candidate pack #{value} with {len(candidates)} candidates."),
                "pack_candidate_count": len(candidates)},
        )

        decision = DailyDecision(
            as_of=as_of,
            is_deep_review=deep_review,
            memo=(
                f"Weekly research refreshed {len(candidates)} screened candidate(s) "
                f"into pack #{pack_id} for {as_of.isoformat()}."
            ),
            llm_used=False,
            risk_profile=risk_profile,
            regime_applied=self._regime_summary(market_regime),
            suppressed_candidates=self._suppressed_symbols(candidates),
        )
        run_id = self._journal(
            decision,
            portfolio,
            scores,
            risk_profile,
            digest=None,
            as_of=as_of,
            deep_review=deep_review,
            command="weekly_research",
            trigger_source=trigger_source,
            candidate_pack_date=as_of,
            stage_status={
                "candidate_pack_id": pack_id,
                "candidate_count": len(candidates),
                "market_regime": market_regime,
                "universe_mode": "full_universe",
                "outcome": "research_refreshed",
                "outcome_summary": f"Weekly research refreshed {len(candidates)} candidate(s).",
            },
            audit_events=audit,
        )
        return RunResult(
            run_id=run_id,
            as_of=as_of,
            decision=decision,
            portfolio=portfolio,
            technicals=technicals,
            fundamentals=fundamentals,
            scores=scores,
            candidates=candidates,
            risk_profile=risk_profile,
        )

    def daily_monitor(
        self,
        as_of: Optional[date] = None,
        skip_news: bool = False,
        refresh_fundamentals: bool = False,
        trigger_source: str = "manual",
    ) -> RunResult:
        """Run the holdings-first daily monitor using the latest weekly pack."""
        started = time.time()
        audit: List[Dict[str, Any]] = []
        self._connect()

        logger.info("=" * 66)
        logger.info("PERSONA: %s", self._persona_id)
        logger.info("DAILY MONITOR using latest weekly candidate pack")

        technicals = self._audit_call(audit, "prices", "candidate_pack", self._load_technicals)
        portfolio = self._audit_call(
            audit, "portfolio", "candidate_pack",
            lambda: self._portfolios.build(self._broker, technicals, as_of=as_of),
        )
        as_of = as_of or self._prices.latest_data_date() or date.today()
        portfolio.as_of = as_of
        deep_review = is_deep_review_day(self._config, as_of)

        pack = self._repo.latest_candidate_pack()
        pack_date = (
            date.fromisoformat(str(pack["research_date"])) if pack and pack.get("research_date") else None
        )
        pack_age_days = (as_of - pack_date).days if pack_date else None
        pack_max_age = int(
            self._config.get("analytics.regime_policy.pack_max_age_days", 10) or 10
        )
        pack_stale = pack_age_days is None or pack_age_days > pack_max_age

        self._audit_event(
            audit, "candidate_pack_load", "completed", 0, {
                **self._base_details("candidate_pack", 0, int(pack.get("candidate_count") or 0) if pack else 0, [],
                    "Loaded the latest weekly candidate pack." if pack else "No weekly candidate pack was available."),
                "pack_date": pack_date.isoformat() if pack_date else None,
                "pack_age_days": pack_age_days,
                "stale": pack_stale,
                "pack_candidate_count": int(pack.get("candidate_count") or 0) if pack else 0,
            },
        )

        candidate_scores = self._candidate_pack_scores(pack) if pack and not pack_stale else []
        candidate_symbols = [score.symbol for score in candidate_scores]
        fundamentals = self._audit_call(
            audit, "fundamentals", "candidate_pack",
            lambda: self._load_named_fundamentals(
                technicals, portfolio, refresh_fundamentals, candidate_symbols,
            ),
            lambda value: self._fundamentals_details(value, len(set(portfolio.symbols + candidate_symbols))),
        )
        live_scores = self._audit_call(
            audit, "candidate_refresh", "candidate_pack",
            lambda: self._refresh_candidate_scores(candidate_scores, fundamentals, technicals),
            lambda value: self._candidate_refresh_details(value, candidate_scores),
        )
        risk_profile = self._audit_call(
            audit, "risk_analytics", "candidate_pack",
            lambda: self._analyse_risk(portfolio, fundamentals, technicals),
            lambda value: self._base_details("candidate_pack", len(portfolio.symbols), len(portfolio.symbols), portfolio.symbols,
                "Portfolio risk analytics completed." if value else "Risk analytics was unavailable."),
        )
        market_regime = (
            risk_profile.market_regime.regime
            if risk_profile and risk_profile.market_regime
            else None
        )
        candidates = self._apply_regime_policy(live_scores, market_regime)

        quarterly_context = self._audit_call(
            audit, "quarterly_reports", "candidate_pack",
            lambda: self._load_quarterly_reports(portfolio, candidates, fundamentals, as_of),
        )
        earnings = self._audit_call(
            audit, "earnings", "candidate_pack",
            lambda: self._build_earnings(portfolio, candidates, as_of),
        )
        digest = self._audit_call(
            audit, "news", "candidate_pack",
            (lambda: None) if skip_news else lambda: self._build_digest(portfolio, candidates, as_of),
            lambda value: self._news_details(value, enabled=not skip_news),
            status="skipped" if skip_news else "completed",
        )
        scores = self._audit_call(
            audit, "full_book_scoring", "candidate_pack",
            lambda: self._scoring.score(fundamentals, technicals),
            lambda value: self._base_details("candidate_pack", len(fundamentals), len(value), list(value),
                "Scored holdings and the candidate-pack symbols for trigger coverage."),
        )
        triggers = self._audit_call(
            audit, "triggers", "candidate_pack",
            lambda: self._evaluate_triggers(
                portfolio, scores, fundamentals, technicals, digest, risk_profile,
                candidates if not pack_stale else [], deep_review, quarterly_context, earnings,
            ),
            self._trigger_details,
        )
        decision_candidates = [] if pack_stale else candidates
        decision = self._audit_call(
            audit, "decision", "candidate_pack",
            lambda: self._decision.decide(
                portfolio=portfolio, scores=scores, fundamentals=fundamentals,
                technicals=technicals, triggers=triggers, candidates=decision_candidates,
                digest=digest, earnings=earnings, risk_profile=risk_profile,
                quarterly_context=quarterly_context, as_of=as_of,
            ),
            lambda value: self._decision_details(value, decision_candidates, triggers),
        )
        if pack_stale:
            note = "No usable weekly candidate pack was available, so new discretionary buys were suppressed."
            decision.memo = f"{decision.memo}\n\n{note}".strip()
            decision.regime_applied = self._regime_summary(market_regime)
        elif decision.regime_applied:
            decision.memo = f"{decision.memo}\n\n{decision.regime_applied}".strip()

        run_id = self._journal(
            decision,
            portfolio,
            scores,
            risk_profile,
            digest,
            as_of,
            deep_review,
            command="daily_monitor",
            trigger_source=trigger_source,
            candidate_pack_date=pack_date,
            candidate_pack_age_days=pack_age_days,
            stage_status={
                "candidate_pack_stale": pack_stale,
                "candidate_count": len(candidates),
                "candidate_pack_age_days": pack_age_days,
                "market_regime": market_regime,
                "elapsed_seconds": round(time.time() - started, 2),
                **self._outcome_status(decision, candidates, digest, pack_stale=pack_stale, mode="candidate_pack"),
            },
            audit_events=audit,
        )
        logger.info(
            "Daily monitor complete in %.0fs: %d proposal(s) awaiting approval",
            time.time() - started,
            len(decision.proposals),
        )
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
    def _audit_call(
        self,
        events: List[Dict[str, Any]],
        stage_name: str,
        mode: str,
        operation: Any,
        details_builder: Optional[Any] = None,
        status: str = "completed",
    ) -> Any:
        """Time a pipeline stage and retain product-safe facts for the run audit."""
        started = time.perf_counter()
        try:
            value = operation()
        except Exception as exc:
            self._audit_event(
                events, stage_name, "failed", (time.perf_counter() - started) * 1000,
                self._base_details(mode, 0, 0, [], f"{stage_name.replace('_', ' ')} failed."),
                str(exc),
            )
            raise
        details = details_builder(value) if details_builder else self._base_details(
            mode, 0, 0, [], f"{stage_name.replace('_', ' ')} completed."
        )
        # Builders describe stage-specific facts; the caller owns the run mode.
        details["mode"] = mode
        self._audit_event(
            events, stage_name, status, (time.perf_counter() - started) * 1000, details
        )
        return value

    @staticmethod
    def _audit_event(
        events: List[Dict[str, Any]], stage_name: str, status: str,
        duration_ms: float, details: Dict[str, Any], error: Optional[str] = None,
    ) -> None:
        events.append({
            "stage_name": stage_name, "status": status,
            "duration_ms": round(duration_ms, 2), "details": details, "error": error,
        })

    @staticmethod
    def _base_details(mode: str, input_count: int, output_count: int,
                      symbols: List[str], summary: str) -> Dict[str, Any]:
        return {
            "mode": mode, "input_count": input_count, "output_count": output_count,
            "symbols_sample": list(symbols)[:10], "summary": summary,
            "top_reasons": [],
        }

    def _fundamentals_details(self, fundamentals: Dict[str, FundamentalSnapshot], requested: int) -> Dict[str, Any]:
        return {
            **self._base_details("full_universe", requested, len(fundamentals), list(fundamentals),
                f"Loaded fundamentals for {len(fundamentals)} of {requested} requested symbols."),
            "requested_count": requested, "loaded_count": len(fundamentals),
            "missing_count": max(0, requested - len(fundamentals)),
        }

    def _score_candidates(self, fundamentals: Dict[str, FundamentalSnapshot], technicals: Dict[str, TechnicalSnapshot], portfolio: PortfolioState) -> Any:
        scores = self._scoring.score(fundamentals, technicals)
        return scores, self._scoring.buy_candidates(scores, exclude=portfolio.symbols, limit=20)

    def _scoring_details(self, result: Any) -> Dict[str, Any]:
        scores, candidates = result
        suppressed = [score for score in scores.values() if getattr(score, "suppressed_reasons", [])]
        return {
            **self._base_details("full_universe", len(scores), len(candidates), [c.symbol for c in candidates],
                f"Scored {len(scores)} symbols and found {len(candidates)} buy candidates."),
            "scored_count": len(scores),
            "passed_gates_count": sum(1 for score in scores.values() if score.passes_gates),
            "buy_candidate_count": len(candidates), "suppressed_count": len(suppressed),
        }

    def _candidate_refresh_details(self, scores: List[FactorScores], cached: List[FactorScores]) -> Dict[str, Any]:
        suppressed = [score for score in scores if getattr(score, "suppressed_reasons", [])]
        return {
            **self._base_details("candidate_pack", len(cached), len(scores), [s.symbol for s in scores],
                f"Refreshed {len(scores)} of {len(cached)} candidate-pack symbols."),
            "scored_count": len(scores), "passed_gates_count": sum(1 for s in scores if s.passes_gates),
            "buy_candidate_count": len([s for s in scores if s.passes_gates]),
            "suppressed_count": len(suppressed),
        }

    def _news_details(self, digest: Optional[NewsDigest], enabled: bool) -> Dict[str, Any]:
        if not enabled:
            return {**self._base_details("candidate_pack", 0, 0, [], "News was skipped for this run."), "enabled": False,
                    "article_count": 0, "flagged_symbol_count": 0, "high_impact_count": 0, "generated_digest": False}
        return {**self._base_details("candidate_pack", 0, int(digest.article_count or 0) if digest else 0,
                    list(digest.flagged_symbols) if digest else [],
                    "News digest generated." if digest else "News was unavailable; the run continued without a digest."),
                "enabled": True, "article_count": int(digest.article_count or 0) if digest else 0,
                "flagged_symbol_count": len(digest.flagged_symbols) if digest else 0,
                "high_impact_count": int(digest.high_impact_count or 0) if digest else 0,
                "generated_digest": bool(digest)}

    def _trigger_details(self, triggers: List[Trigger]) -> Dict[str, Any]:
        types: Dict[str, int] = {}
        severities: Dict[str, int] = {}
        for trigger in triggers:
            types[trigger.trigger_type.value] = types.get(trigger.trigger_type.value, 0) + 1
            severities[trigger.severity.value] = severities.get(trigger.severity.value, 0) + 1
        return {**self._base_details("candidate_pack", 0, len(triggers), [t.symbol for t in triggers],
                    f"Evaluated triggers; {len(triggers)} fired."), "total_trigger_count": len(triggers),
                "trigger_type_counts": types, "severity_counts": severities}

    def _decision_details(self, decision: DailyDecision, candidates: List[FactorScores], triggers: List[Trigger]) -> Dict[str, Any]:
        mandatory = sum(1 for trigger in triggers if trigger.severity.value == "mandatory")
        return {**self._base_details("candidate_pack", len(candidates), len(decision.proposals),
                    [p.symbol for p in decision.proposals], f"Decision produced {len(decision.proposals)} proposal(s)."),
                "mandatory_proposal_count": mandatory, "llm_used": decision.llm_used,
                "discretionary_proposal_count": max(0, len(decision.proposals) - mandatory),
                "rules_only_fallback_used": "Deterministic cash deployment was used" in decision.memo,
                "suppressed_candidates": list(decision.suppressed_candidates),
            }

    def _outcome_status(self, decision: DailyDecision, candidates: List[FactorScores], digest: Optional[NewsDigest], *, pack_stale: bool, mode: str) -> Dict[str, Any]:
        if decision.proposals:
            return {"outcome": "proposals", "outcome_summary": f"{len(decision.proposals)} proposal(s) generated.", "universe_mode": mode}
        reasons: List[Dict[str, str]] = []
        if pack_stale:
            reasons.append({"code": "candidate_pack_stale", "label": "Stale or unavailable candidate pack", "detail": "New discretionary buys were suppressed because weekly research was unavailable or too old."})
        elif decision.rejected:
            reasons.append({"code": "all_proposals_failed_risk", "label": "Risk validation rejected all proposals", "detail": "Every proposed trade failed hard portfolio risk checks."})
        elif not candidates:
            reasons.append({"code": "no_candidates_after_scoring", "label": "No candidates survived scoring", "detail": "No eligible buy candidates were available after scoring and regime policy."})
        elif not decision.llm_used:
            reasons.append({"code": "rules_only_no_actionable_buy", "label": "Rules found no actionable buy", "detail": "The rules-only decision path did not identify an actionable trade."})
        else:
            reasons.append({"code": "llm_declined_all", "label": "Decision layer declined discretionary trades", "detail": "The decision layer found no trade justified by the available evidence."})
        if digest is None:
            reasons.append({"code": "news_unavailable", "label": "News unavailable or skipped", "detail": "The run continued without a news digest."})
        primary = reasons[0]
        return {"outcome": "no_proposals", "universe_mode": mode,
                "primary_reason_code": primary["code"], "primary_reason_label": primary["label"],
                "primary_reason_detail": primary["detail"], "contributing_reasons": reasons,
                "outcome_summary": f"No picks: {primary['label'].lower()}."}

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

    def _load_named_fundamentals(
        self,
        technicals: Dict[str, TechnicalSnapshot],
        portfolio: PortfolioState,
        refresh: bool,
        symbols: List[str],
    ) -> Dict[str, FundamentalSnapshot]:
        wanted = list(dict.fromkeys(list(portfolio.symbols) + list(symbols)))
        if not wanted:
            return {}
        fundamentals = self._fundamentals.get_many(wanted, force_refresh=refresh)
        if portfolio.symbols and not refresh:
            fundamentals.update(self._fundamentals.refresh_symbols(portfolio.symbols))
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
        regime = risk_profile.market_regime.regime if risk_profile and risk_profile.market_regime else None
        triggers.extend(self._risk.cash_deployment_triggers(portfolio, candidates, regime))
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
        command: str = "analyse",
        trigger_source: str = "manual",
        candidate_pack_date: Optional[date] = None,
        candidate_pack_age_days: Optional[float] = None,
        stage_status: Optional[Dict[str, Any]] = None,
        audit_events: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        market_regime = risk_profile.market_regime if risk_profile else None
        run_id = self._repo.start_run(
            run_date=as_of,
            is_deep_review=deep_review,
            portfolio_value=portfolio.total_value,
            cash=portfolio.cash,
            command=command,
            trigger_source=trigger_source,
            candidate_pack_date=candidate_pack_date,
            candidate_pack_age_days=candidate_pack_age_days,
            market_regime=market_regime.regime if market_regime else None,
            market_regime_score=market_regime.score if market_regime else None,
        )
        bind_log_context(run_id=run_id)
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
            stage_status=stage_status,
        )
        for event in audit_events or []:
            self._repo.record_run_stage(run_id, **event)
        if command != "weekly_research":
            rejection_counts: Dict[str, int] = {}
            for _, reason in decision.rejected:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            self._repo.record_run_stage(
                run_id,
                "risk_validation",
                "completed",
                details={
                    **self._base_details(
                        "candidate_pack" if command == "daily_monitor" else "full_universe",
                        len(decision.proposals) + len(decision.rejected), len(decision.proposals),
                        [proposal.symbol for proposal in decision.proposals],
                        "Hard portfolio risk validation completed.",
                    ),
                    "approved_count": len(decision.proposals),
                    "rejected_count": len(decision.rejected),
                    "rejection_reasons": rejection_counts,
                },
            )
        journal_started = time.perf_counter()
        self._repo.record_run_stage(
            run_id,
            "journal",
            "completed",
            duration_ms=round((time.perf_counter() - journal_started) * 1000, 2),
            details={
                **self._base_details("candidate_pack" if command == "daily_monitor" else "full_universe", 0, len(decision.proposals),
                    [proposal.symbol for proposal in decision.proposals], "Run results were saved to the journal."),
                "proposal_count": len(decision.proposals),
                "rejected_count": len(decision.rejected),
                "briefing_saved": (self._config.briefing_dir / f"briefing_{as_of.isoformat()}.txt").exists(),
            },
        )
        return run_id

    def _candidate_pack_scores(self, pack: Optional[Dict[str, Any]]) -> List[FactorScores]:
        if not pack:
            return []
        payload = json.loads(str(pack.get("payload") or "[]"))
        out: List[FactorScores] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            out.append(
                FactorScores(
                    symbol=str(item.get("symbol") or "").upper(),
                    rank=item.get("rank"),
                    composite=float(item.get("composite") or 0.0),
                    percentile=item.get("percentile"),
                    passes_gates=bool(item.get("passes_gates", False)),
                    gate_failures=list(item.get("gate_failures") or []),
                    sector_model=item.get("sector_model"),
                    supported_metrics=list(item.get("supported_metrics") or []),
                    unsupported_metrics=list(item.get("unsupported_metrics") or []),
                    metric_coverage_pct=item.get("metric_coverage_pct"),
                    valuation_summary=dict(item.get("valuation_summary") or {}),
                    macro_exposures=dict(item.get("macro_exposures") or {}),
                    suppressed_reasons=list(item.get("suppressed_reasons") or []),
                )
            )
        return out

    def _refresh_candidate_scores(
        self,
        cached_scores: List[FactorScores],
        fundamentals: Dict[str, FundamentalSnapshot],
        technicals: Dict[str, TechnicalSnapshot],
    ) -> List[FactorScores]:
        if not cached_scores:
            return []
        rescored = self._scoring.score(fundamentals, technicals)
        ordered: List[FactorScores] = []
        for cached in cached_scores:
            score = rescored.get(cached.symbol)
            if score is None:
                continue
            ordered.append(score)
        return ordered

    def _apply_regime_policy(
        self,
        candidates: List[FactorScores],
        market_regime: Optional[str],
    ) -> List[FactorScores]:
        if not candidates:
            return []
        minimum = self._risk.regime_min_buy_score(market_regime)
        limit = self._risk.regime_new_buy_limit(market_regime)
        allowed: List[FactorScores] = []
        for candidate in candidates:
            if candidate.composite < minimum:
                candidate.suppressed_reasons.append(
                    f"below {market_regime or 'unknown'} regime score floor {minimum:.2f}"
                )
                continue
            allowed.append(candidate)
        return allowed[:limit] if limit > 0 else []

    @staticmethod
    def _suppressed_symbols(candidates: List[FactorScores]) -> List[str]:
        return [c.symbol for c in candidates if getattr(c, "suppressed_reasons", [])]

    def _regime_summary(self, market_regime: Optional[str]) -> str:
        regime = str(market_regime or "unknown").lower()
        floor = self._risk.regime_min_buy_score(regime)
        limit = self._risk.regime_new_buy_limit(regime)
        return (
            f"Decision under regime policy: {regime.title()} backdrop applied a "
            f"minimum composite buy score of {floor:.2f} and capped new buys at {limit}."
        )

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
                from src.workspace.dashboard_snapshot import DashboardSnapshotService

                DashboardSnapshotService(self._config).refresh_portfolio()
            except Exception as exc:
                logger.error("Post-execution workspace refresh failed: %s", exc, exc_info=True)

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
