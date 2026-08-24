"""Dashboard data assembly.

Kept separate from the HTTP layer so the views can be tested without a server,
and so the templates receive plain dictionaries rather than domain objects.

Everything here reads from the journal, never from the broker. The dashboard is
a window onto what the fund recorded, so it stays fast, works after hours, and
shows exactly what the manager saw when the decision was made.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional

from src.config import Config, get_config
from src.journal.performance import PerformanceCalculator
from src.journal.repository import JournalRepository
from src.learning.service import LearningService
from src.logging_setup import get_logger
from src.operations import load_events

logger = get_logger(__name__)


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def money(value: Optional[float]) -> str:
    """Indian-convention rupee formatting: crore and lakh, not millions."""
    return format_money(value, "INR")


def format_money(value: Optional[float], currency: str = "INR") -> str:
    """Format a monetary amount in INR or USD."""
    if value is None:
        return "-"
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    code = (currency or "INR").upper()
    if code == "USD":
        if magnitude >= 1e6:
            return f"{sign}${magnitude / 1e6:,.2f}M"
        if magnitude >= 1e3:
            return f"{sign}${magnitude / 1e3:,.1f}K"
        return f"{sign}${magnitude:,.2f}"
    if magnitude >= 1e7:
        return f"{sign}Rs {magnitude / 1e7:,.2f} cr"
    if magnitude >= 1e5:
        return f"{sign}Rs {magnitude / 1e5:,.2f} L"
    return f"{sign}Rs {magnitude:,.0f}"


def region_label(region: str) -> str:
    from src.market.registry import LEGACY_REGION_TO_MARKET, get_market

    key = (region or "IN").upper()
    market_id = LEGACY_REGION_TO_MARKET.get(key)
    if market_id:
        return get_market(market_id).label
    mapping = {"IN": "India", "US": "United States"}
    return mapping.get(key, region)


def broker_label(provider: str) -> str:
    mapping = {
        "angelone": "Angel One",
        "alpaca": "Alpaca",
        "alpaca_crypto": "Alpaca Crypto",
    }
    return mapping.get((provider or "angelone").lower(), provider)


def persona_currency(pcfg: Config, region: str) -> str:
    """USD for US personas even when global fund.base_currency is INR."""
    region = (region or "IN").upper()
    explicit = str(pcfg.get("fund.base_currency") or "").upper()
    if region == "US":
        return "USD" if not explicit or explicit == "INR" else explicit
    return explicit or "INR"


def pct(value: Optional[float], places: int = 1, signed: bool = False) -> str:
    if value is None:
        return "-"
    if signed and value == 0:
        return f"{value:.{places}f}%"
    return f"{value:+.{places}f}%" if signed else f"{value:.{places}f}%"


def number(value: Optional[float], places: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:,.{places}f}"


def sign_class(value: Optional[float]) -> str:
    if value is None:
        return "flat"
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


class DashboardService:
    """Read-side queries backing every page."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._repo = JournalRepository(self._config)
        self._performance = PerformanceCalculator(self._config)
        self._learning = LearningService(self._config)
        self._ops_now = datetime.now

    @property
    def repository(self) -> JournalRepository:
        return self._repo

    @property
    def persona_id(self) -> str:
        return self._config.persona_id

    @property
    def fund_name(self) -> str:
        return str(self._config.get("fund.name", "AI Managed Equity Fund"))

    @property
    def benchmark_label(self) -> str:
        fund_label = self._config.get("fund.benchmark_label")
        if fund_label:
            return str(fund_label)
        mandate_label = self._config.mandate.get("benchmark_label")
        if mandate_label:
            return str(mandate_label)
        market_fund = getattr(self._config.market, "fund", None) or {}
        market_label = market_fund.get("benchmark_label")
        if market_label:
            return str(market_label)
        return "Benchmark"

    @property
    def is_dry_run(self) -> bool:
        return bool(self._config.secrets.dry_run)

    def mandate_summary(self) -> Dict[str, Any]:
        mandate = self._config.mandate
        liquidity = mandate.get("liquidity", {}) or {}
        cash_bounds = mandate.get("cash_bounds_pct", {}) or {}
        benchmark = (
            self._config.get("fund.benchmark_label")
            or mandate.get("benchmark_label")
            or self.benchmark_label
        )
        return {
            "benchmark": benchmark,
            "horizon_years": mandate.get("investment_horizon_years"),
            "target_excess_return_pct": mandate.get("target_excess_return_pct"),
            "max_stock_weight_pct": mandate.get(
                "max_stock_weight_pct", self._config.get("risk.max_position_pct")
            ),
            "max_sector_weight_pct": mandate.get(
                "max_sector_weight_pct", self._config.get("risk.max_sector_pct")
            ),
            "max_tracking_error_pct": mandate.get("max_tracking_error_pct"),
            "max_drawdown_pct": mandate.get("max_drawdown_pct"),
            "turnover_budget_pct": mandate.get(
                "turnover_budget_pct",
                self._config.get("risk.annual_turnover_budget_pct"),
            ),
            "cash_min_pct": cash_bounds.get(
                "min", self._config.get("fund.min_cash_buffer_pct")
            ),
            "cash_max_pct": cash_bounds.get("max"),
            "market_caps": mandate.get("market_cap_limits", {}).get("allowed", []),
            "liquidity_floor_inr": liquidity.get("min_avg_daily_turnover_inr"),
            "adv_limit_pct": liquidity.get("max_position_pct_of_adv"),
            "derivatives_allowed": bool(mandate.get("derivatives_allowed", False)),
            "leverage_allowed": bool(mandate.get("leverage_allowed", False)),
        }

    def analytics_summary(self) -> Dict[str, Any]:
        analytics = self._config.analytics
        return {
            "enabled": bool(analytics.get("enabled", True)),
            "sector_models": analytics.get("sector_models", []) or [],
            "preferred_sector_models": analytics.get("preferred_sector_models", []) or [],
            "metric_weights": analytics.get("metric_weights", {}) or {},
        }

    def _empty_overview(self) -> Dict[str, Any]:
        return {
            "has_data": False,
            "total_value": 0.0,
            "position_count": 0,
            "cash_pct": 100.0,
            "pending_count": 0,
            "cash": 0.0,
            "invested_value": 0.0,
            "unrealised_pnl": 0.0,
            "run": None,
            "daily_change_pct": None,
            "warnings": [],
            "candidate_pack": None,
            "latest_nav_date": None,
            "latest_analysis_date": None,
        }

    def _safe_overview(self) -> Dict[str, Any]:
        try:
            return self.overview()
        except Exception as exc:
            logger.debug("Overview unavailable for %s: %s", self.persona_id, exc)
            return self._empty_overview()

    # ------------------------------------------------------------------
    # Overview
    # ------------------------------------------------------------------
    def _reconstructed_book_totals(self, holdings: List[Dict[str, Any]]) -> Dict[str, float]:
        capital = float(self._config.secrets.initial_capital)
        net_deployed = self._repo.net_cash_deployed()
        realised = self._repo.total_realised_pnl()
        invested_value = sum(float(h["market_value"] or 0.0) for h in holdings)
        unrealised_pnl = sum(float(h["unrealised_pnl"] or 0.0) for h in holdings)
        cash = max(capital - net_deployed + realised, 0.0)
        return {
            "cash": cash,
            "invested_value": invested_value,
            "unrealised_pnl": unrealised_pnl,
            "realised_pnl": realised,
            "total_value": cash + invested_value,
        }

    def overview(self) -> Dict[str, Any]:
        run = self._repo.latest_run()
        history = self._repo.nav_history()
        holdings = self.holdings()
        pending = self.pending_proposals()
        candidate_pack = self._repo.latest_candidate_pack()

        latest = history[-1] if history else None
        previous = history[-2] if len(history) > 1 else None

        daily_change_pct = None
        if latest and previous and float(previous["total_value"]) > 0:
            daily_change_pct = (
                float(latest["total_value"]) / float(previous["total_value"]) - 1.0
            ) * 100.0

        capital = float(self._config.secrets.initial_capital)
        # No NAV yet: still seed cash/total from mandate capital so Overview
        # can show the same placeholder shell as a cash-only book.
        total_value = float(latest["total_value"]) if latest else capital
        cash = float(latest["cash"]) if latest else capital
        invested_value = float(latest["invested_value"]) if latest else 0.0
        unrealised_pnl = float(latest["unrealised_pnl"] or 0.0) if latest else 0.0

        # If NAV still reflects a cash-only book but fills exist, prefer the
        # reconstructed or fresher holdings mark so Overview matches Holdings.
        holdings_invested = sum(float(h["market_value"] or 0.0) for h in holdings)
        reconstructed = False
        holdings_unrealised = sum(float(h["unrealised_pnl"] or 0.0) for h in holdings)
        if holdings and (
            invested_value <= 0
            or abs(holdings_invested - invested_value) > 1.0
            or abs(holdings_unrealised - unrealised_pnl) > 1.0
        ):
            reconciled = self._reconstructed_book_totals(holdings)
            cash = reconciled["cash"]
            invested_value = reconciled["invested_value"]
            unrealised_pnl = reconciled["unrealised_pnl"]
            total_value = reconciled["total_value"]
            daily_change_pct = None
            reconstructed = True

        return_since_inception_pct = (
            (total_value / capital - 1.0) * 100.0
            if capital > 0 and total_value > 0
            else 0.0 if capital > 0 else None
        )
        perf = self.performance()
        total_return_pct = perf.get("total_return_pct")
        observations = perf.get("observations") or 0
        if return_since_inception_pct is not None:
            if total_return_pct is None or observations < 2 or reconstructed:
                total_return_pct = return_since_inception_pct
            elif (
                total_return_pct == 0
                and abs(return_since_inception_pct) > 0.005
            ):
                total_return_pct = return_since_inception_pct
        if total_return_pct is None and not latest and not holdings:
            total_return_pct = 0.0

        warnings: List[str] = []
        latest_nav_date = str(latest["run_date"]) if latest else None
        latest_analysis_date = str(run["run_date"]) if run else None
        if latest_nav_date and latest_analysis_date and latest_nav_date > latest_analysis_date:
            warnings.append(
                f"Holdings/NAV are fresher than the latest analysis run ({latest_analysis_date})."
            )
        if run and not (run.get("memo") or "").strip():
            warnings.append("Latest run did not persist a manager memo.")
        if candidate_pack and run and run.get("command") == "daily_monitor":
            pack_date = str(candidate_pack.get("research_date") or "")
            if pack_date and run.get("candidate_pack_date") and pack_date != str(run["candidate_pack_date"]):
                warnings.append("Latest daily monitor did not use the latest available candidate pack.")

        return {
            "has_data": bool(run) or bool(holdings),
            "run": self._run_view(run) if run else None,
            "total_value": total_value,
            "cash": cash,
            "cash_pct": (cash / total_value * 100.0) if total_value > 0 else 0.0,
            "invested_value": invested_value,
            "position_count": len(holdings),
            "daily_change_pct": daily_change_pct,
            "realised_pnl": float(latest["realised_pnl"] or 0.0) if latest else 0.0,
            "unrealised_pnl": unrealised_pnl,
            "pending_count": len(pending),
            "top_holdings": holdings[:5],
            "sector_weights": self.sector_weights(),
            "market_regime": (self.risk_profile() or {}).get("market_regime"),
            "total_return_pct": total_return_pct,
            "excess_return_pct": perf.get("excess_return_pct"),
            "warnings": warnings,
            "candidate_pack": self._candidate_pack_view(candidate_pack),
            "latest_nav_date": latest_nav_date,
            "latest_analysis_date": latest_analysis_date,
        }

    def _run_view(self, run: Dict[str, Any]) -> Dict[str, Any]:
        stage_status = _loads(run.get("stage_status"), {})
        command = run.get("command") or "analyse"
        universe_mode = stage_status.get("universe_mode") or (
            "candidate_pack" if command == "daily_monitor" else "full_universe"
        )
        return {
            "id": run["id"],
            "run_date": run["run_date"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "status": run["status"],
            "is_deep_review": bool(run["is_deep_review"]),
            "llm_used": bool(run["llm_used"]),
            "memo": run["memo"] or "",
            "memo_present": bool(run["memo"]),
            "memo_length": run.get("memo_length"),
            "turnover_used_pct": run["turnover_used_pct"],
            "portfolio_value": run["portfolio_value"],
            "command": command,
            "run_mode": command,
            "universe_mode": universe_mode,
            "trigger_source": run.get("trigger_source") or "manual",
            "candidate_pack_date": run.get("candidate_pack_date"),
            "candidate_pack_age_days": run.get("candidate_pack_age_days"),
            "market_regime": run.get("market_regime"),
            "market_regime_score": run.get("market_regime_score"),
            "stage_status": stage_status,
            "outcome_summary": stage_status.get("outcome_summary") or self._legacy_outcome_summary(run),
        }

    @staticmethod
    def _legacy_outcome_summary(run: Dict[str, Any]) -> str:
        return "Run completed; audit details were not recorded for this historical run."

    @staticmethod
    def _normalise_stage_event(row: Dict[str, Any]) -> Dict[str, Any]:
        details = _loads(row.get("details"), {})
        return {
            "stage_name": row["stage_name"], "status": row["status"],
            "duration_ms": row.get("duration_ms"), "details": details,
            "error": row.get("error"),
            "summary": details.get("summary") or str(row["stage_name"]).replace("_", " ").title(),
            "input_count": details.get("input_count", 0),
            "output_count": details.get("output_count", 0),
            "top_reasons": details.get("top_reasons") or [],
        }

    @staticmethod
    def _stage_lookup(events: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
        return next((event for event in events if event["stage_name"] == name), None)

    def _candidate_pack_view(self, pack: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not pack:
            return None
        return {
            "id": pack["id"],
            "research_date": pack["research_date"],
            "candidate_count": pack.get("candidate_count"),
        }

    # ------------------------------------------------------------------
    # Holdings
    # ------------------------------------------------------------------
    def holdings(self) -> List[Dict[str, Any]]:
        rows = self._repo.latest_position_snapshot()
        if rows:
            return [
                {
                    "symbol": row["symbol"],
                    "company_name": row["company_name"] or row["symbol"],
                    "sector": row["sector"] or "Unknown",
                    "quantity": row["quantity"],
                    "avg_cost": row["avg_cost"],
                    "last_price": row["last_price"],
                    "market_value": row["market_value"],
                    "weight_pct": row["weight_pct"],
                    "return_pct": row["return_pct"],
                    "unrealised_pnl": row["unrealised_pnl"],
                    "holding_days": row["holding_days"],
                    "composite_score": row["composite_score"],
                    "beta": row["beta"],
                    "idiosyncratic_vol": row["idiosyncratic_vol"],
                    "risk_contribution_pct": row["risk_contribution_pct"],
                    "thesis": row["thesis"] or "",
                }
                for row in rows
            ]

        # Fills can land before the next snapshot refresh. Reconstruct from the
        # trade journal so Overview/Holdings are not stuck on an empty cash book.
        lots = self._repo.reconstruct_positions()
        if not lots:
            return []

        invested = sum(float(lot["quantity"]) * float(lot["avg_cost"]) for lot in lots)
        views: List[Dict[str, Any]] = []
        for lot in lots:
            qty = float(lot["quantity"])
            avg_cost = float(lot["avg_cost"])
            market_value = qty * avg_cost
            views.append(
                {
                    "symbol": lot["symbol"],
                    "company_name": lot["symbol"],
                    "sector": "Unknown",
                    "quantity": qty,
                    "avg_cost": avg_cost,
                    "last_price": avg_cost,
                    "market_value": market_value,
                    "weight_pct": (market_value / invested * 100.0) if invested else 0.0,
                    "return_pct": 0.0,
                    "unrealised_pnl": 0.0,
                    "holding_days": None,
                    "composite_score": None,
                    "beta": None,
                    "idiosyncratic_vol": None,
                    "risk_contribution_pct": None,
                    "thesis": "",
                }
            )
        views.sort(key=lambda row: row["market_value"], reverse=True)
        return views

    def sector_weights(self) -> List[Dict[str, Any]]:
        buckets: Dict[str, float] = {}
        for holding in self.holdings():
            weight = holding["weight_pct"] or 0.0
            buckets[holding["sector"]] = buckets.get(holding["sector"], 0.0) + weight
        rows = [
            {"sector": sector, "weight_pct": weight}
            for sector, weight in buckets.items()
        ]
        rows.sort(key=lambda r: r["weight_pct"], reverse=True)
        return rows

    # ------------------------------------------------------------------
    # Proposals
    # ------------------------------------------------------------------
    def pending_proposals(self) -> List[Dict[str, Any]]:
        return [self._proposal_view(p) for p in self._repo.pending_proposals()]

    def approved_proposals(self) -> List[Dict[str, Any]]:
        return [self._proposal_view(p) for p in self._repo.approved_proposals()]

    def recent_decisions(self, limit: int = 40) -> List[Dict[str, Any]]:
        """Proposals already acted on, newest first."""
        runs = self._repo.recent_runs(limit=10)
        rows: List[Dict[str, Any]] = []
        for run in runs:
            for proposal in self._repo.proposals_for_run(run["id"]):
                if proposal["status"] == "pending":
                    continue
                view = self._proposal_view(proposal)
                view["run_date"] = run["run_date"]
                rows.append(view)
        rows.sort(key=lambda r: r["id"], reverse=True)
        return rows[:limit]

    def _proposal_view(self, proposal: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": proposal["id"],
            "run_id": proposal["run_id"],
            "run_date": proposal.get("run_date"),
            "symbol": proposal["symbol"],
            "company_name": proposal["company_name"] or proposal["symbol"],
            "sector": proposal["sector"] or "Unknown",
            "action": proposal["action"],
            "quantity": proposal["quantity"],
            "reference_price": proposal["reference_price"],
            "estimated_value": proposal["estimated_value"],
            "rationale": proposal["rationale"] or "",
            "thesis": proposal["thesis"] or "",
            "target_price": proposal["target_price"],
            "trigger_types": _loads(proposal["trigger_types"], []),
            "confidence": proposal["confidence"],
            "warnings": _loads(proposal["warnings"], []),
            "source": proposal["source"] or "llm",
            "status": proposal["status"],
            "decided_at": proposal["decided_at"],
            "decided_by": proposal["decided_by"],
            "rejection_reason": proposal["rejection_reason"],
            "is_sell": str(proposal["action"]).upper() in ("SELL", "TRIM"),
            "is_mandatory": (proposal["source"] or "") == "risk_engine",
        }

    def approve(self, proposal_id: int) -> bool:
        proposal = self._repo.get_proposal(proposal_id)
        if not proposal or proposal["status"] != "pending":
            return False
        logger.info(
            "User approved proposal %d: %s %s x%s",
            proposal_id,
            proposal["action"],
            proposal["symbol"],
            proposal["quantity"],
        )
        return self._repo.set_proposal_status(proposal_id, "approved")

    def reject(self, proposal_id: int, reason: str = "") -> bool:
        proposal = self._repo.get_proposal(proposal_id)
        if not proposal or proposal["status"] != "pending":
            return False
        logger.info(
            "User rejected proposal %d: %s %s x%s (%s)",
            proposal_id,
            proposal["action"],
            proposal["symbol"],
            proposal["quantity"],
            reason or "no reason given",
        )
        return self._repo.set_proposal_status(
            proposal_id, "rejected", rejection_reason=reason or "rejected by user"
        )

    def approve_all(self) -> int:
        count = 0
        for proposal in self._repo.pending_proposals():
            if self.approve(proposal["id"]):
                count += 1
        return count

    def reject_all(self) -> int:
        count = 0
        for proposal in self._repo.pending_proposals():
            if self.reject(proposal["id"], reason="rejected in bulk by user"):
                count += 1
        return count

    # ------------------------------------------------------------------
    # News
    # ------------------------------------------------------------------
    def digest(self) -> Optional[Dict[str, Any]]:
        row = self._repo.latest_digest()
        if not row:
            return None
        return {
            "run_date": row["run_date"],
            "macro_summary": row["macro_summary"] or "",
            "market_summary": row["market_summary"] or "",
            "holdings_notes": _loads(row["holdings_notes"], {}),
            "watchlist_notes": _loads(row["watchlist_notes"], {}),
            "flagged_symbols": _loads(row["flagged_symbols"], []),
            "article_count": row["article_count"] or 0,
            "high_impact_count": row["high_impact_count"] or 0,
            "generated_by_llm": bool(row["generated_by_llm"]),
        }

    # ------------------------------------------------------------------
    # Performance and risk
    # ------------------------------------------------------------------
    def performance(self) -> Dict[str, Any]:
        report = self._performance.report()
        history = self._repo.nav_history()
        latest = history[-1] if history else None

        # If the latest recorded NAV is stale versus the latest holdings mark,
        # prefer the reconstructed book so Overview and Holdings stay consistent.
        holdings = self.holdings()
        if latest and holdings:
            latest_invested = float(latest.get("invested_value") or 0.0)
            latest_unrealised = float(latest.get("unrealised_pnl") or 0.0)
            reconciled = self._reconstructed_book_totals(holdings)
            if (
                latest_invested <= 0
                or abs(reconciled["invested_value"] - latest_invested) > 1.0
                or abs(reconciled["unrealised_pnl"] - latest_unrealised) > 1.0
            ):
                report.current_value = reconciled["total_value"]
                report.realised_pnl = reconciled["realised_pnl"]
                report.unrealised_pnl = reconciled["unrealised_pnl"]
            if report.starting_value > 0:
                report.total_return_pct = (
                    report.current_value / report.starting_value - 1.0
                ) * 100.0
                if report.benchmark_return_pct is not None:
                    report.excess_return_pct = (
                        report.total_return_pct - report.benchmark_return_pct
                    )

        return {
            "start_date": report.start_date.isoformat() if report.start_date else None,
            "end_date": report.end_date.isoformat() if report.end_date else None,
            "days": report.days,
            "observations": report.observations,
            "starting_value": report.starting_value,
            "current_value": report.current_value,
            "total_return_pct": report.total_return_pct,
            "annualised_return_pct": report.annualised_return_pct,
            "benchmark_return_pct": report.benchmark_return_pct,
            "excess_return_pct": report.excess_return_pct,
            "volatility_pct": report.volatility_pct,
            "sharpe_ratio": report.sharpe_ratio,
            "sortino_ratio": report.sortino_ratio,
            "information_ratio": report.information_ratio,
            "max_drawdown_pct": report.max_drawdown_pct,
            "max_drawdown_date": report.max_drawdown_date,
            "current_drawdown_pct": report.current_drawdown_pct,
            "win_rate_pct": report.win_rate_pct,
            "best_day_pct": report.best_day_pct,
            "worst_day_pct": report.worst_day_pct,
            "realised_pnl": report.realised_pnl,
            "unrealised_pnl": report.unrealised_pnl,
            "notes": report.notes,
            "is_beating_benchmark": report.is_beating_benchmark,
        }

    def nav_series(self) -> Dict[str, List[Any]]:
        """Fund and benchmark rebased to 100 for a like-for-like chart."""
        history = self._repo.nav_history()
        dates = [row["run_date"] for row in history]
        values = [float(row["total_value"]) for row in history]

        fund = self._rebase(values)
        benchmark_raw = [row["benchmark_close"] for row in history]
        benchmark = (
            self._rebase([float(v) for v in benchmark_raw])
            if all(v for v in benchmark_raw) and len(benchmark_raw) >= 2
            else []
        )
        return {"dates": dates, "fund": fund, "benchmark": benchmark, "values": values}

    @staticmethod
    def _rebase(values: List[float]) -> List[float]:
        if not values or values[0] <= 0:
            return []
        return [round(v / values[0] * 100.0, 3) for v in values]

    def risk_profile(self) -> Optional[Dict[str, Any]]:
        row = self._repo.latest_risk_profile()
        if not row:
            return None
        payload = _loads(row["payload"], {})
        return {
            "run_date": row["run_date"],
            "portfolio_beta": row["portfolio_beta"],
            "portfolio_vol": row["portfolio_vol"],
            "tracking_error_pct": row["tracking_error_pct"],
            "active_share_pct": row["active_share_pct"],
            "concentration_top5_pct": row["concentration_top5_pct"],
            "avg_pairwise_correlation": row["avg_pairwise_correlation"],
            "active_selection_share_pct": payload.get("active_selection_share_pct"),
            "active_factor_share_pct": payload.get("active_factor_share_pct"),
            "effective_positions": payload.get("effective_positions"),
            "max_pairwise_correlation": payload.get("max_pairwise_correlation"),
            "style_tilts": payload.get("style_tilts") or {},
            "warnings": payload.get("warnings") or [],
            "market_regime": payload.get("market_regime"),
        }

    # ------------------------------------------------------------------
    # Learning
    # ------------------------------------------------------------------
    def learning_summary(self) -> Dict[str, Any]:
        return self._learning.summary()

    def learning_lessons(self) -> List[Dict[str, Any]]:
        return self._learning.lessons()

    def decision_reviews(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self._learning.reviews(limit=limit)

    def learning_payload(self) -> Dict[str, Any]:
        return self._learning.payload()

    # ------------------------------------------------------------------
    # Intraday paper/backtest summaries
    # ------------------------------------------------------------------
    def intraday_summary(self) -> Optional[Dict[str, Any]]:
        path = self._config.root / "data" / "intraday" / f"{self.persona_id}_latest.json"
        if not path.exists():
            return None
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Could not read intraday summary %s: %s", path, exc)
            return None
        # Live watch writes completed_trades; backtests use trades.
        if "trades" not in summary and summary.get("completed_trades"):
            summary = dict(summary)
            summary["trades"] = summary["completed_trades"]
        return summary

    @property
    def is_intraday(self) -> bool:
        return bool(self._config.get("intraday.enabled", False))

    def intraday_performance(
        self, summary: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Performance-page payload for live intraday personas."""
        capital = float(
            summary.get("starting_capital") or self._config.secrets.initial_capital
        )
        ending = float(summary.get("ending_capital") or capital)
        trades = summary.get("trades") or summary.get("completed_trades") or []
        return {
            "is_intraday": True,
            "start_date": summary.get("start_date"),
            "end_date": summary.get("end_date"),
            "days": 1,
            "observations": 1,
            "starting_value": capital,
            "current_value": ending,
            "total_return_pct": float(summary.get("total_return_pct") or 0.0),
            "annualised_return_pct": None,
            "benchmark_return_pct": None,
            "excess_return_pct": None,
            "volatility_pct": None,
            "sharpe_ratio": None,
            "sortino_ratio": None,
            "information_ratio": None,
            "max_drawdown_pct": None,
            "max_drawdown_date": None,
            "current_drawdown_pct": None,
            "win_rate_pct": float(summary.get("win_rate_pct") or 0.0),
            "best_day_pct": None,
            "worst_day_pct": None,
            "realised_pnl": float(
                summary.get("total_net_pnl") or summary.get("daily_net_pnl") or 0.0
            ),
            "unrealised_pnl": 0.0,
            "notes": [],
            "is_beating_benchmark": None,
            "total_trades": int(summary.get("total_trades") or len(trades)),
            "expectancy_r": float(summary.get("expectancy_r") or 0.0),
            "strategy_pnl": summary.get("strategy_pnl") or {},
        }

    def intraday_news_context(
        self, summary: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """News-page substitute for intraday: watchlist catalysts and scan notes."""
        if not summary:
            return {"has_data": False}
        watchlist = summary.get("watchlist") or []
        candidates = summary.get("top_candidates") or []
        skipped = summary.get("skipped_signals") or []
        return {
            "has_data": bool(watchlist or candidates or skipped),
            "run_date": summary.get("end_date") or summary.get("start_date"),
            "active": bool(summary.get("active")),
            "last_scan_at": summary.get("last_scan_at"),
            "watchlist": watchlist,
            "top_candidates": candidates,
            "skipped_signals": skipped,
        }

    def intraday_dashboard_overview(
        self, summary: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Overview-page payload for live intraday personas (not the swing journal)."""
        region = self._config.market_region
        currency = persona_currency(self._config, region)
        capital = float(
            summary.get("starting_capital") or self._config.secrets.initial_capital
        )
        trades = summary.get("trades") or summary.get("completed_trades") or []
        return {
            "has_data": True,
            "is_intraday": True,
            "currency": currency,
            "region": region,
            "region_label": region_label(region),
            "broker_label": broker_label(self._config.broker_provider),
            "starting_capital": capital,
            "total_value": float(summary.get("ending_capital") or capital),
            "total_net_pnl": float(
                summary.get("total_net_pnl") or summary.get("daily_net_pnl") or 0.0
            ),
            "total_return_pct": float(summary.get("total_return_pct") or 0.0),
            "total_trades": int(summary.get("total_trades") or len(trades)),
            "win_rate_pct": float(summary.get("win_rate_pct") or 0.0),
            "expectancy_r": float(summary.get("expectancy_r") or 0.0),
            "active": bool(summary.get("active")),
            "last_scan_at": summary.get("last_scan_at"),
            "start_date": summary.get("start_date"),
            "end_date": summary.get("end_date"),
            "watchlist": summary.get("watchlist") or [],
            "open_positions": summary.get("open_positions") or [],
            "trades": trades,
            "skipped_signals": summary.get("skipped_signals") or [],
            "top_candidates": summary.get("top_candidates") or [],
            "lockout_reason": summary.get("lockout_reason"),
        }

    def attribution(self) -> List[Dict[str, Any]]:
        """Contribution to return by position, from the latest snapshot."""
        holdings = self.holdings()
        total = sum(h["market_value"] or 0.0 for h in holdings)
        cash = self.overview()["cash"]
        fund_value = total + cash
        if fund_value <= 0:
            return []

        rows = [
            {
                "symbol": h["symbol"],
                "sector": h["sector"],
                "weight_pct": h["weight_pct"],
                "return_pct": h["return_pct"],
                "pnl": h["unrealised_pnl"],
                "contribution_pct": (h["unrealised_pnl"] or 0.0) / fund_value * 100.0,
            }
            for h in holdings
        ]
        rows.sort(key=lambda r: r["contribution_pct"] or 0.0, reverse=True)
        return rows

    # ------------------------------------------------------------------
    # Multi-persona overview
    # ------------------------------------------------------------------
    def all_personas_overview(self) -> List[Dict[str, Any]]:
        from src.config import Config, _deep_merge, _sync_mandate_from_fund

        results: List[Dict[str, Any]] = []
        personas = self._config.raw.get("personas", {})
        if not isinstance(personas, dict):
            personas = {}
        for pid, pdef in personas.items():
            if not isinstance(pdef, dict) or not pdef.get("enabled", True):
                continue
            meta_keys = {
                "enabled",
                "capital",
                "capital_inr",
                "capital_usd",
                "description",
                "strategy_description",
                "market",
                "id",
            }
            overrides = {k: v for k, v in pdef.items() if k not in meta_keys}
            base_raw = {k: v for k, v in self._config.raw.items() if k != "personas"}
            merged = _deep_merge(base_raw, overrides)
            merged["personas"] = personas
            _sync_mandate_from_fund(merged)
            secrets = self._config.secrets
            capital = pdef.get("capital")
            if capital is None:
                capital = pdef.get("capital_inr")
            if capital is None:
                capital = pdef.get("capital_usd")
            if capital is not None:
                secrets = secrets.with_capital(float(capital))
            pcfg = Config(
                raw=merged,
                secrets=secrets,
                root=self._config.root,
                persona_id=pid,
            )
            svc = DashboardService(config=pcfg)
            is_intraday = bool(pcfg.get("intraday.enabled", False))
            overview = (
                self._empty_overview()
                if is_intraday
                else svc._safe_overview()
            )
            intraday_summary = svc.intraday_summary()
            region = pcfg.market_region
            market = pcfg.market
            currency = persona_currency(pcfg, region)
            results.append({
                "persona_id": pid,
                "description": str(pdef.get("description", pid)),
                "strategy_description": str(pdef.get("strategy_description", "")),
                "market_id": market.id,
                "market_label": market.label,
                "region": region,
                "region_label": region_label(region),
                "currency": currency,
                "broker": pcfg.broker_provider,
                "broker_label": market.display.broker_label or broker_label(pcfg.broker_provider),
                "fund_name": str(pcfg.get("fund.name", pcfg.get("fund.benchmark_label", pid))),
                "benchmark_label": str(
                    pcfg.get("fund.benchmark_label")
                    or pcfg.mandate.get("benchmark_label")
                    or ""
                ),
                "capital": float(capital) if capital is not None else secrets.initial_capital,
                "total_value": overview["total_value"],
                "position_count": overview["position_count"],
                "cash_pct": overview["cash_pct"],
                "pending_count": overview["pending_count"],
                "has_data": overview["has_data"],
                "intraday": bool(pcfg.get("intraday.enabled", False)),
                "intraday_summary": intraday_summary,
                "intraday_has_results": intraday_summary is not None,
            })
        return results

    def personas_by_region(self) -> Dict[str, List[Dict[str, Any]]]:
        """Group enabled personas by market region for the dashboard."""
        return self.personas_by_market_legacy()

    def personas_by_market(self) -> Dict[str, List[Dict[str, Any]]]:
        """Group enabled personas by market id."""
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in self.all_personas_overview():
            market_id = row.get("market_id") or "india_equity"
            grouped.setdefault(market_id, []).append(row)
        for rows in grouped.values():
            # Swing first, then intraday; stable by persona id.
            rows.sort(key=lambda r: (bool(r.get("intraday")), r.get("persona_id", "")))
        return grouped

    def personas_by_market_legacy(self) -> Dict[str, List[Dict[str, Any]]]:
        """Group personas by legacy IN/US region code."""
        grouped: Dict[str, List[Dict[str, Any]]] = {"IN": [], "US": []}
        for row in self.all_personas_overview():
            region = row.get("region", "IN")
            grouped.setdefault(region, []).append(row)
        for rows in grouped.values():
            rows.sort(key=lambda r: (bool(r.get("intraday")), r.get("persona_id", "")))
        return grouped

    def intraday_personas(
        self,
        region: Optional[str] = None,
        market_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Intraday personas, optionally filtered to one market."""
        rows = [
            row for row in self.all_personas_overview()
            if row.get("intraday")
        ]
        if market_id:
            rows = [row for row in rows if row.get("market_id") == market_id]
        elif region:
            rows = [row for row in rows if row.get("region") == region.upper()]
        return rows

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------
    def trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        return [
            {
                "id": row["id"],
                "executed_at": row["executed_at"],
                "symbol": row["symbol"],
                "action": row["action"],
                "quantity": row["quantity"],
                "price": row["price"],
                "value": row["value"],
                "status": row["status"],
                "broker_order_id": row["broker_order_id"],
                "error": row["error"],
                "dry_run": bool(row["dry_run"]),
                "realised_pnl": row["realised_pnl"],
                "is_sell": str(row["action"]).upper() in ("SELL", "TRIM"),
            }
            for row in self._repo.trades(limit=limit)
        ]

    def runs(self, limit: int = 30) -> List[Dict[str, Any]]:
        rows = []
        for run in self._repo.recent_runs(limit=limit):
            view = self._run_view(run)
            view["stage_events"] = [self._normalise_stage_event(row) for row in self._repo.run_stage_events(run["id"])]
            news = self._stage_lookup(view["stage_events"], "news")
            risk = self._stage_lookup(view["stage_events"], "risk_validation") or self._stage_lookup(view["stage_events"], "risk_analytics")
            view["news_status"] = news["status"] if news else "not recorded"
            view["risk_status"] = risk["status"] if risk else "not recorded"
            rows.append(view)
        return rows

    def run_detail(self, run_id: int) -> Optional[Dict[str, Any]]:
        for run in self._repo.recent_runs(limit=200):
            if run["id"] == run_id:
                view = self._run_view(run)
                view["proposals"] = [
                    self._proposal_view(p) for p in self._repo.proposals_for_run(run_id)
                ]
                view["quarterly_updates"] = self.quarterly_updates_for_run(run["run_date"])
                view["stage_events"] = [self._normalise_stage_event(row) for row in self._repo.run_stage_events(run_id)]
                pack = self._stage_lookup(view["stage_events"], "candidate_pack_load")
                risk = self._stage_lookup(view["stage_events"], "risk_validation")
                decision = self._stage_lookup(view["stage_events"], "decision")
                context = dict(pack["details"]) if pack else {"mode": view["universe_mode"]}
                context["stage_summary"] = context.get("summary")
                context["summary"] = (
                    "This run reused the weekly candidate pack; it did not perform a full-universe discovery pass."
                    if view["command"] == "daily_monitor" else
                    "This run explored the screenable universe and refreshed the candidate pack."
                    if view["command"] == "weekly_research" else
                    "This run explored the full screenable universe."
                )
                view["candidate_pack_context"] = context
                view["risk_rejections_summary"] = (risk or {}).get("details", {}).get("rejection_reasons", {})
                view["suppressed_candidates_summary"] = (decision or {}).get("details", {}).get("suppressed_candidates", [])
                return view
        return None

    def quarterly_updates_for_run(self, run_date: str) -> List[Dict[str, Any]]:
        try:
            parsed = datetime.fromisoformat(run_date).date()
        except (TypeError, ValueError):
            return []
        rows = self._repo.quarterly_report_analyses_for_run(parsed)
        updates: List[Dict[str, Any]] = []
        for row in rows:
            updates.append(
                {
                    "symbol": row["symbol"],
                    "fiscal_period": row["fiscal_period"],
                    "summary": row["summary"] or "",
                    "thesis_impact": row["thesis_impact"] or "unclear",
                    "flags": _loads(row["flags"], []),
                    "highlights": _loads(row["highlights"], {}),
                }
            )
        return updates

    def quarterly_report_cards(self) -> List[Dict[str, Any]]:
        cards: List[Dict[str, Any]] = []
        for holding in self.holdings():
            history = self._repo.quarterly_history(holding["symbol"], limit=8)
            if not history:
                continue
            summary = self._quarterly_summary(holding["symbol"])
            cards.append(
                {
                    "symbol": holding["symbol"],
                    "company_name": holding["company_name"],
                    "latest_quarter": summary.get("latest_quarter"),
                    "quarterly_summary": summary.get("quarterly_summary"),
                    "quarterly_thesis_impact": summary.get("quarterly_thesis_impact"),
                    "revenue_series": self._align_series(history, "revenue"),
                    "profit_series": self._align_series(history, "net_profit"),
                    "opm_series": self._align_series(history, "opm_pct"),
                }
            )
        return cards

    @staticmethod
    def _align_series(history: List[Dict[str, Any]], field: str) -> Dict[str, List[Any]]:
        ordered = list(reversed(history))
        values = [row.get(field) for row in ordered]
        return {
            "dates": [row["fiscal_period"] for row in ordered],
            "values": [float(v) if v is not None else 0.0 for v in values],
            "has_values": any(v is not None for v in values),
        }

    def _quarterly_summary(self, symbol: str) -> Dict[str, Any]:
        latest = self._repo.latest_quarterly_report(symbol)
        analysis = self._repo.latest_quarterly_report_analysis(symbol)
        return {
            "latest_quarter": latest["fiscal_period"] if latest else None,
            "quarterly_summary": analysis["summary"] if analysis else "",
            "quarterly_thesis_impact": analysis["thesis_impact"] if analysis else "",
            "quarterly_flags": _loads(analysis["flags"], []) if analysis else [],
        }

    def briefing(self, run_date: str) -> Optional[str]:
        """The exact briefing text the decision layer was given that day."""
        try:
            parsed = datetime.fromisoformat(run_date).date()
        except (TypeError, ValueError):
            return None
        path = self._config.briefing_dir / f"briefing_{parsed.isoformat()}.txt"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------
    def operations_overview(self) -> Dict[str, Any]:
        now = self._ops_now()
        events = load_events(self._config.root / "data")
        runs = self._repo.recent_runs(limit=60)

        cards = [
            self._scheduled_job_card(
                "long_cycle",
                label="Morning long-horizon cycle",
                schedule=self._market_schedule(self._config.market_id, "swing_cycle"),
                grace_minutes=int(self._config.get("observability.daily_monitor_grace_minutes", 45) or 45),
                now=now,
                events=events,
            ),
            self._scheduled_job_card(
                "close",
                label="Close run",
                schedule=self._market_schedule(self._config.market_id, "swing_close"),
                grace_minutes=int(self._config.get("observability.close_grace_minutes", 45) or 45),
                now=now,
                events=events,
            ),
            self._whatsapp_digest_card(now, events),
            self._backup_card(now, events),
        ]

        for market_id in ("india_equity", "us_equity", "crypto_spot_us"):
            cards.append(self._intraday_market_card(market_id, now, events))
        cards.append(self._dashboard_service_card(now, events))

        return {
            "cards": cards,
            "incidents": self._operations_incidents(events, runs),
            "freshness": self._freshness_summary(cards),
            "generated_at": now.isoformat(timespec="seconds"),
            "log_hint": "Search Cloud Logging for OPS_EVENT and one of the event names: scheduled_run, backup_run, backup_upload, intraday_launcher, intraday_watch, intraday_heartbeat, dashboard_service.",
        }

    def _whatsapp_digest_card(self, now: datetime, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        digest_events = [event for event in events if event.get("event") == "whatsapp_digest"]
        starts = [event for event in digest_events if event.get("status") == "start"]
        successes = [event for event in digest_events if event.get("status") == "success"]
        failures = [event for event in digest_events if event.get("status") == "failure"]
        last_start = starts[-1] if starts else None
        last_success = successes[-1] if successes else None
        last_failure = failures[-1] if failures else None
        target = self._today_at("18:00", now)
        current_state = "unknown"
        if self._is_weekday(now):
            if last_failure and self._event_time(last_failure) >= self._event_time(last_success):
                current_state = "failed"
            elif last_success and self._event_time(last_success) >= target:
                current_state = "healthy"
            elif now > target + timedelta(minutes=60):
                current_state = "late"
            else:
                current_state = "healthy"
        return {
            "key": "whatsapp_digest",
            "label": "India WhatsApp digest",
            "kind": "notification",
            "expected_schedule": f"Weekdays after close {self._config.market.tz_abbrev}",
            "last_start_time": self._event_ts(last_start),
            "last_success_time": self._event_ts(last_success),
            "last_failure_time": self._event_ts(last_failure),
            "current_state": current_state,
            "latest_error": (last_failure or {}).get("error"),
            "latest_note": "Sent only after a successful close and dashboard publication.",
        }

    def _market_schedule(self, market_id: str, field: str) -> str:
        from src.market.registry import get_market

        market = get_market(market_id)
        return str(getattr(market.scheduling, field))

    def _scheduled_job_card(
        self,
        job: str,
        *,
        label: str,
        schedule: str,
        grace_minutes: int,
        now: datetime,
        events: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        job_events = [e for e in events if e.get("event") == "scheduled_run" and e.get("job") == job]
        starts = [e for e in job_events if e.get("status") == "start"]
        successes = [e for e in job_events if e.get("status") == "success"]
        failures = [e for e in job_events if e.get("status") == "failure"]
        last_start = starts[-1] if starts else None
        last_success = successes[-1] if successes else None
        last_failure = failures[-1] if failures else None

        target = self._today_at(schedule, now)
        healthy_today = last_success and self._event_time(last_success) >= target
        latest_error = (last_failure or {}).get("error")
        current_state = "healthy"
        if self._is_weekday(now):
            if last_failure and self._event_time(last_failure) >= self._event_time(last_success):
                current_state = "failed"
            elif healthy_today:
                current_state = "healthy"
            elif now > target + timedelta(minutes=grace_minutes):
                current_state = "late"
            else:
                current_state = "healthy"
        elif last_failure and self._event_time(last_failure) >= self._event_time(last_success):
            current_state = "failed"

        return {
            "key": job,
            "label": label,
            "kind": "scheduled",
            "expected_schedule": f"Weekdays {schedule} {self._config.market.tz_abbrev}",
            "last_start_time": self._event_ts(last_start),
            "last_success_time": self._event_ts(last_success),
            "last_failure_time": self._event_ts(last_failure),
            "current_state": current_state if (last_success or last_failure or self._is_weekday(now)) else "unknown",
            "latest_error": latest_error,
            "latest_note": f"Expected by {schedule} with {grace_minutes} min grace",
        }

    def _backup_card(self, now: datetime, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        backups = [e for e in events if e.get("event") == "backup_run"]
        uploads = [e for e in events if e.get("event") == "backup_upload"]
        starts = [e for e in backups if e.get("status") == "start"]
        successes = [e for e in backups if e.get("status") == "success"]
        failures = [e for e in backups if e.get("status") == "failure"]
        upload_success = [e for e in uploads if e.get("status") == "success"]
        upload_failure = [e for e in uploads if e.get("status") == "failure"]

        last_start = starts[-1] if starts else None
        last_success = successes[-1] if successes else None
        last_failure = failures[-1] if failures else None
        last_upload_success = upload_success[-1] if upload_success else None
        last_upload_failure = upload_failure[-1] if upload_failure else None
        schedule = "19:30"
        target = self._today_at(schedule, now)
        grace_minutes = int(self._config.get("observability.backup_grace_minutes", 90) or 90)

        state = "healthy"
        if last_upload_failure and self._event_time(last_upload_failure) >= self._event_time(last_upload_success):
            state = "failed"
        elif last_failure and self._event_time(last_failure) >= self._event_time(last_success):
            state = "failed"
        elif self._is_weekday(now) and now > target + timedelta(minutes=grace_minutes):
            if not last_success or self._event_time(last_success) < target:
                state = "late"

        latest_error = (
            (last_upload_failure or {}).get("error")
            or (last_failure or {}).get("error")
        )
        note = "Local dump plus GCS upload"
        if last_upload_success and last_upload_success.get("bucket"):
            note = f"GCS bucket {last_upload_success['bucket']}"

        return {
            "key": "backup",
            "label": "Backup run",
            "kind": "backup",
            "expected_schedule": f"Weekdays {schedule} {self._config.market.tz_abbrev}",
            "last_start_time": self._event_ts(last_start),
            "last_success_time": self._event_ts(last_upload_success or last_success),
            "last_failure_time": self._event_ts(last_upload_failure or last_failure),
            "current_state": state if (last_success or last_failure or last_upload_failure or self._is_weekday(now)) else "unknown",
            "latest_error": latest_error,
            "latest_note": note,
        }

    def _intraday_market_card(
        self, market_id: str, now: datetime, events: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        from src.market.registry import get_market

        market = get_market(market_id)
        heartbeats = [e for e in events if e.get("event") == "intraday_heartbeat" and e.get("market_id") == market_id]
        launchers = [e for e in events if e.get("event") == "intraday_launcher" and e.get("market_id") == market_id]
        watch_failures = [e for e in events if e.get("event") == "intraday_watch" and e.get("market_id") == market_id and e.get("status") == "failure"]
        last_heartbeat = heartbeats[-1] if heartbeats else None
        last_launcher_success = self._latest_event(launchers, "success")
        last_launcher_failure = self._latest_event(launchers, "failure")
        last_watch_failure = watch_failures[-1] if watch_failures else None

        in_session = self._is_market_session_open(market_id, now)
        heartbeat_interval = int(self._config.get("observability.heartbeat_interval_seconds", 300) or 300)
        heartbeat_deadline = now - timedelta(seconds=heartbeat_interval * 2)

        state = "healthy"
        if in_session:
            if not last_heartbeat or self._event_time(last_heartbeat) < heartbeat_deadline:
                state = "late"
            else:
                state = "healthy"
        elif last_watch_failure and self._event_time(last_watch_failure) >= self._event_time(last_heartbeat):
            state = "failed"

        latest_error = (last_watch_failure or {}).get("error")
        if not latest_error and last_launcher_failure and self._event_time(last_launcher_failure) >= self._event_time(last_launcher_success):
            latest_error = last_launcher_failure.get("error")
            state = "failed"

        session_label = f"{market.session.open}-{market.session.close} {market.tz_abbrev}"
        trading_days = (
            "Daily"
            if getattr(market.session, "trading_days", "weekdays") == "all_days"
            else "Weekdays"
        )
        return {
            "key": f"intraday-{market_id}",
            "label": f"{market.label} intraday watch",
            "kind": "intraday",
            "expected_schedule": f"{trading_days} {session_label}",
            "last_start_time": self._event_ts(last_launcher_success or self._latest_event(launchers, "start")),
            "last_success_time": self._event_ts(last_heartbeat),
            "last_failure_time": self._event_ts(last_watch_failure or last_launcher_failure),
            "current_state": state if (last_heartbeat or launchers or market.scheduling.intraday_watch) else "unknown",
            "latest_error": latest_error,
            "latest_note": (
                f"Latest heartbeat state: {(last_heartbeat or {}).get('session_state', 'none')}"
                if last_heartbeat
                else "No heartbeat yet"
            ),
        }

    def _dashboard_service_card(self, now: datetime, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        starts = [e for e in events if e.get("event") == "dashboard_service" and e.get("status") == "start"]
        last_start = starts[-1] if starts else None
        freshness_minutes = int(self._config.get("observability.dashboard_freshness_minutes", 30) or 30)
        state = "healthy"
        if last_start and self._event_time(last_start) < now - timedelta(minutes=freshness_minutes):
            state = "healthy"
        return {
            "key": "dashboard-service",
            "label": "Dashboard service",
            "kind": "service",
            "expected_schedule": "Always on",
            "last_start_time": self._event_ts(last_start),
            "last_success_time": now.isoformat(timespec="seconds"),
            "last_failure_time": None,
            "current_state": state,
            "latest_error": None,
            "latest_note": "This page is responding now",
        }

    def _operations_incidents(
        self, events: List[Dict[str, Any]], runs: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        incidents: List[Dict[str, Any]] = []
        for event in events:
            if event.get("status") not in {"failure", "warning"}:
                continue
            incidents.append(
                {
                    "time": event.get("ts"),
                    "source": event.get("event"),
                    "subject": event.get("job") or event.get("market_id") or event.get("persona_id") or event.get("bucket") or "-",
                    "message": event.get("error") or event.get("state") or event.get("status"),
                }
            )
        for run in runs[:20]:
            for row in self._repo.run_stage_events(run["id"]):
                if str(row["status"]).lower() not in {"failed", "failure", "error"}:
                    continue
                incidents.append(
                    {
                        "time": row["created_at"],
                        "source": f"run_stage:{row['stage_name']}",
                        "subject": run.get("command") or run.get("run_date") or f"run {run['id']}",
                        "message": row["error"] or row["details"] or "stage failed",
                    }
                )
        incidents.sort(key=lambda row: str(row.get("time") or ""), reverse=True)
        return incidents[:20]

    def _freshness_summary(self, cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        lookup = {card["key"]: card for card in cards}
        return [
            {
                "label": "Morning run",
                "value": lookup["long_cycle"]["current_state"],
            },
            {
                "label": "Close run",
                "value": lookup["close"]["current_state"],
            },
            {
                "label": "Backup",
                "value": lookup["backup"]["current_state"],
            },
            {
                "label": "India intraday",
                "value": lookup["intraday-india_equity"]["current_state"],
            },
            {
                "label": "US intraday",
                "value": lookup["intraday-us_equity"]["current_state"],
            },
        ]

    @staticmethod
    def _event_time(event: Optional[Dict[str, Any]]) -> datetime:
        if not event or not event.get("ts"):
            return datetime.min
        try:
            return datetime.fromisoformat(str(event["ts"]).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return datetime.min

    def _event_ts(self, event: Optional[Dict[str, Any]]) -> Optional[str]:
        if not event:
            return None
        return str(event.get("ts") or "")

    @staticmethod
    def _latest_event(events: List[Dict[str, Any]], status: str) -> Optional[Dict[str, Any]]:
        matches = [event for event in events if event.get("status") == status]
        return matches[-1] if matches else None

    @staticmethod
    def _is_weekday(now: datetime) -> bool:
        return now.weekday() < 5

    @staticmethod
    def _parse_clock(value: str) -> time:
        hour, minute = str(value).split(":", 1)
        return time(hour=int(hour), minute=int(minute))

    def _today_at(self, hhmm: str, now: datetime) -> datetime:
        clock = self._parse_clock(hhmm)
        return now.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)

    def _is_market_session_open(self, market_id: str, now: datetime) -> bool:
        from zoneinfo import ZoneInfo
        from src.market.registry import get_market

        market = get_market(market_id)
        local_now = now.astimezone(ZoneInfo(market.timezone)) if now.tzinfo else now
        if getattr(market.session, "trading_days", "weekdays") != "all_days" and local_now.weekday() >= 5:
            return False
        open_at = self._parse_clock(market.session.open)
        close_at = self._parse_clock(market.session.close)
        current = local_now.time()
        return open_at <= current <= close_at
