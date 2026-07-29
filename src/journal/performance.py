"""Performance measurement.

Reports the fund against its benchmark the way a factsheet would: absolute and
relative return, risk-adjusted return, and drawdown. Relative numbers matter
most - an equity fund that made 12% in a year the index made 20% has lost, and
the arithmetic here says so plainly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from src.config import Config, get_config
from src.journal.repository import JournalRepository
from src.logging_setup import get_logger

logger = get_logger(__name__)

TRADING_DAYS = 252
# Indian risk-free proxy: roughly the 10-year government bond yield.
DEFAULT_RISK_FREE_PCT = 7.0


@dataclass
class PerformanceReport:
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    days: int = 0
    starting_value: float = 0.0
    current_value: float = 0.0
    total_return_pct: Optional[float] = None
    annualised_return_pct: Optional[float] = None
    benchmark_return_pct: Optional[float] = None
    benchmark_annualised_pct: Optional[float] = None
    excess_return_pct: Optional[float] = None
    volatility_pct: Optional[float] = None
    sharpe_ratio: Optional[float] = None
    sortino_ratio: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    max_drawdown_date: Optional[str] = None
    current_drawdown_pct: Optional[float] = None
    win_rate_pct: Optional[float] = None
    best_day_pct: Optional[float] = None
    worst_day_pct: Optional[float] = None
    information_ratio: Optional[float] = None
    realised_pnl: float = 0.0
    unrealised_pnl: float = 0.0
    observations: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def is_beating_benchmark(self) -> Optional[bool]:
        if self.excess_return_pct is None:
            return None
        return self.excess_return_pct > 0


def _annualise(total_return_pct: float, days: int) -> Optional[float]:
    """Convert a cumulative return into a compound annual rate."""
    if days <= 0:
        return None
    years = days / 365.25
    growth = 1.0 + total_return_pct / 100.0
    if growth <= 0:
        return -100.0
    # Below a year, annualising is misleading rather than informative.
    if years < 1.0:
        return None
    return (growth ** (1.0 / years) - 1.0) * 100.0


def _max_drawdown(values: List[float], labels: List[str]) -> tuple:
    """Largest peak-to-trough decline, and when the trough occurred."""
    if not values:
        return None, None
    peak = values[0]
    worst = 0.0
    worst_label: Optional[str] = None
    for value, label in zip(values, labels):
        peak = max(peak, value)
        if peak <= 0:
            continue
        drawdown = (value / peak - 1.0) * 100.0
        if drawdown < worst:
            worst = drawdown
            worst_label = label
    return worst, worst_label


class PerformanceCalculator:
    """Computes the fund's performance report from NAV history."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._repo = JournalRepository(self._config)

    def report(self) -> PerformanceReport:
        history = self._repo.nav_history()
        report = PerformanceReport()

        if not history:
            report.notes.append("No NAV history yet; run the daily cycle first.")
            return report

        report.observations = len(history)
        report.starting_value = float(history[0]["total_value"])
        report.current_value = float(history[-1]["total_value"])
        report.realised_pnl = float(history[-1].get("realised_pnl") or 0.0)
        report.unrealised_pnl = float(history[-1].get("unrealised_pnl") or 0.0)

        try:
            report.start_date = datetime.fromisoformat(history[0]["run_date"]).date()
            report.end_date = datetime.fromisoformat(history[-1]["run_date"]).date()
            report.days = (report.end_date - report.start_date).days
        except (TypeError, ValueError):
            pass

        if report.starting_value > 0:
            report.total_return_pct = (
                report.current_value / report.starting_value - 1.0
            ) * 100.0
            report.annualised_return_pct = _annualise(
                report.total_return_pct, report.days
            )

        # --- Benchmark comparison ---
        benchmark_values = [
            float(row["benchmark_close"])
            for row in history
            if row.get("benchmark_close")
        ]
        if len(benchmark_values) >= 2 and benchmark_values[0] > 0:
            report.benchmark_return_pct = (
                benchmark_values[-1] / benchmark_values[0] - 1.0
            ) * 100.0
            report.benchmark_annualised_pct = _annualise(
                report.benchmark_return_pct, report.days
            )
            if report.total_return_pct is not None:
                report.excess_return_pct = (
                    report.total_return_pct - report.benchmark_return_pct
                )
        else:
            report.notes.append("Benchmark history incomplete; relative return omitted.")

        # --- Daily return series ---
        values = [float(row["total_value"]) for row in history]
        labels = [str(row["run_date"]) for row in history]
        returns: List[float] = []
        for previous, current in zip(values, values[1:]):
            if previous > 0:
                returns.append((current / previous - 1.0) * 100.0)

        if len(returns) >= 2:
            mean = sum(returns) / len(returns)
            variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
            daily_vol = math.sqrt(variance)
            report.volatility_pct = daily_vol * math.sqrt(TRADING_DAYS)
            report.best_day_pct = max(returns)
            report.worst_day_pct = min(returns)
            report.win_rate_pct = (
                sum(1 for r in returns if r > 0) / len(returns) * 100.0
            )

            risk_free = float(
                self._config.get("fund.risk_free_rate_pct", DEFAULT_RISK_FREE_PCT)
                or DEFAULT_RISK_FREE_PCT
            )
            annualised_return = mean * TRADING_DAYS
            if report.volatility_pct and report.volatility_pct > 0:
                report.sharpe_ratio = (
                    annualised_return - risk_free
                ) / report.volatility_pct

            # Sortino penalises only downside deviation, which is what an
            # investor actually experiences as risk.
            downside = [r for r in returns if r < 0]
            if downside:
                downside_variance = sum(r**2 for r in downside) / len(downside)
                downside_vol = math.sqrt(downside_variance) * math.sqrt(TRADING_DAYS)
                if downside_vol > 0:
                    report.sortino_ratio = (
                        annualised_return - risk_free
                    ) / downside_vol

            # Information ratio: excess return per unit of tracking error.
            benchmark_returns = [
                float(row["benchmark_return_pct"])
                for row in history
                if row.get("benchmark_return_pct") is not None
            ]
            if len(benchmark_returns) == len(returns) and len(returns) >= 20:
                active = [p - b for p, b in zip(returns, benchmark_returns)]
                active_mean = sum(active) / len(active)
                active_variance = sum((a - active_mean) ** 2 for a in active) / (
                    len(active) - 1
                )
                tracking_error = math.sqrt(active_variance) * math.sqrt(TRADING_DAYS)
                if tracking_error > 0:
                    report.information_ratio = (
                        active_mean * TRADING_DAYS
                    ) / tracking_error
        else:
            report.notes.append(
                "Fewer than three observations; risk statistics need more history."
            )

        drawdown, drawdown_date = _max_drawdown(values, labels)
        report.max_drawdown_pct = drawdown
        report.max_drawdown_date = drawdown_date
        peak = max(values) if values else 0.0
        if peak > 0:
            report.current_drawdown_pct = (report.current_value / peak - 1.0) * 100.0

        return report

    def attribution(self, portfolio) -> List[Dict[str, Any]]:
        """Per-position contribution to the fund's return.

        Contribution is the position's unrealised P&L as a share of total fund
        value, which is what tells the manager where performance came from
        rather than merely which stock rose most.
        """
        total_value = portfolio.total_value
        if total_value <= 0:
            return []

        rows: List[Dict[str, Any]] = []
        for position in portfolio.positions:
            rows.append(
                {
                    "symbol": position.symbol,
                    "sector": position.sector,
                    "weight_pct": round(position.market_value / total_value * 100.0, 2),
                    "return_pct": round(position.return_pct, 2),
                    "pnl": round(position.unrealised_pnl, 2),
                    "contribution_pct": round(
                        position.unrealised_pnl / total_value * 100.0, 3
                    ),
                }
            )
        rows.sort(key=lambda r: r["contribution_pct"], reverse=True)
        return rows

    def sector_attribution(self, portfolio) -> List[Dict[str, Any]]:
        total_value = portfolio.total_value
        if total_value <= 0:
            return []

        buckets: Dict[str, Dict[str, float]] = {}
        for position in portfolio.positions:
            bucket = buckets.setdefault(
                position.sector, {"weight": 0.0, "pnl": 0.0, "cost": 0.0}
            )
            bucket["weight"] += position.market_value / total_value * 100.0
            bucket["pnl"] += position.unrealised_pnl
            bucket["cost"] += position.cost_basis

        rows = [
            {
                "sector": sector,
                "weight_pct": round(data["weight"], 2),
                "pnl": round(data["pnl"], 2),
                "return_pct": round(
                    data["pnl"] / data["cost"] * 100.0 if data["cost"] > 0 else 0.0, 2
                ),
                "contribution_pct": round(data["pnl"] / total_value * 100.0, 3),
            }
            for sector, data in buckets.items()
        ]
        rows.sort(key=lambda r: r["contribution_pct"], reverse=True)
        return rows
