"""Dashboard data assembly.

Kept separate from the HTTP layer so the views can be tested without a server,
and so the templates receive plain dictionaries rather than domain objects.

Everything here reads from the journal, never from the broker. The dashboard is
a window onto what the fund recorded, so it stays fast, works after hours, and
shows exactly what the manager saw when the decision was made.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.config import Config, get_config
from src.journal.performance import PerformanceCalculator
from src.journal.repository import JournalRepository
from src.learning.service import LearningService
from src.logging_setup import get_logger

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
    if value is None:
        return "-"
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude >= 1e7:
        return f"{sign}Rs {magnitude / 1e7:,.2f} cr"
    if magnitude >= 1e5:
        return f"{sign}Rs {magnitude / 1e5:,.2f} L"
    return f"{sign}Rs {magnitude:,.0f}"


def pct(value: Optional[float], places: int = 1, signed: bool = False) -> str:
    if value is None:
        return "-"
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
        return str(self._config.get("fund.benchmark_label", "Nifty 500"))

    @property
    def is_dry_run(self) -> bool:
        return bool(self._config.secrets.dry_run)

    # ------------------------------------------------------------------
    # Overview
    # ------------------------------------------------------------------
    def overview(self) -> Dict[str, Any]:
        run = self._repo.latest_run()
        history = self._repo.nav_history()
        holdings = self.holdings()
        pending = self.pending_proposals()

        latest = history[-1] if history else None
        previous = history[-2] if len(history) > 1 else None

        daily_change_pct = None
        if latest and previous and float(previous["total_value"]) > 0:
            daily_change_pct = (
                float(latest["total_value"]) / float(previous["total_value"]) - 1.0
            ) * 100.0

        total_value = float(latest["total_value"]) if latest else 0.0
        cash = float(latest["cash"]) if latest else 0.0

        return {
            "has_data": bool(run),
            "run": self._run_view(run) if run else None,
            "total_value": total_value,
            "cash": cash,
            "cash_pct": (cash / total_value * 100.0) if total_value > 0 else 0.0,
            "invested_value": float(latest["invested_value"]) if latest else 0.0,
            "position_count": len(holdings),
            "daily_change_pct": daily_change_pct,
            "realised_pnl": float(latest["realised_pnl"] or 0.0) if latest else 0.0,
            "unrealised_pnl": float(latest["unrealised_pnl"] or 0.0) if latest else 0.0,
            "pending_count": len(pending),
            "top_holdings": holdings[:5],
            "sector_weights": self.sector_weights(),
        }

    def _run_view(self, run: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": run["id"],
            "run_date": run["run_date"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "status": run["status"],
            "is_deep_review": bool(run["is_deep_review"]),
            "llm_used": bool(run["llm_used"]),
            "memo": run["memo"] or "",
            "turnover_used_pct": run["turnover_used_pct"],
            "portfolio_value": run["portfolio_value"],
        }

    # ------------------------------------------------------------------
    # Holdings
    # ------------------------------------------------------------------
    def holdings(self) -> List[Dict[str, Any]]:
        rows = self._repo.latest_position_snapshot()
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
            "User approved proposal %d: %s %s x%d",
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
            "User rejected proposal %d: %s %s x%d (%s)",
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
        from src.config import enabled_personas, get_persona_config, persona_description

        results: List[Dict[str, Any]] = []
        for pid in enabled_personas():
            pcfg = get_persona_config(pid)
            svc = DashboardService(config=pcfg)
            overview = svc.overview()
            results.append({
                "persona_id": pid,
                "description": persona_description(pid),
                "total_value": overview["total_value"],
                "position_count": overview["position_count"],
                "cash_pct": overview["cash_pct"],
                "pending_count": overview["pending_count"],
                "has_data": overview["has_data"],
            })
        return results

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
        return [self._run_view(run) for run in self._repo.recent_runs(limit=limit)]

    def run_detail(self, run_id: int) -> Optional[Dict[str, Any]]:
        for run in self._repo.recent_runs(limit=200):
            if run["id"] == run_id:
                view = self._run_view(run)
                view["proposals"] = [
                    self._proposal_view(p) for p in self._repo.proposals_for_run(run_id)
                ]
                return view
        return None

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
