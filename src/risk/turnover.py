"""Turnover budget.

The single most effective guardrail against an over-eager decision layer. The
fund is allowed a fixed amount of trading per year; once it is spent, only
mandatory risk trades get through.

Turnover follows the standard mutual fund definition: the lesser of purchases
and sales over the period, divided by average net assets. A 75% budget implies
an average holding period of roughly 16 months, in line with what active equity
funds actually do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from src.config import Config, get_config
from src.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class TurnoverStatus:
    budget_pct: float
    used_pct: float
    purchases: float
    sales: float
    average_net_assets: float
    window_days: int

    @property
    def remaining_pct(self) -> float:
        return max(self.budget_pct - self.used_pct, 0.0)

    @property
    def is_exhausted(self) -> bool:
        return self.used_pct >= self.budget_pct

    @property
    def utilisation_pct(self) -> float:
        if self.budget_pct <= 0:
            return 0.0
        return min(self.used_pct / self.budget_pct * 100.0, 999.0)

    def remaining_value(self) -> float:
        """Rupee value of discretionary trading still available."""
        return self.remaining_pct / 100.0 * self.average_net_assets

    def describe(self) -> str:
        if self.is_exhausted:
            return (
                f"Turnover budget EXHAUSTED: {self.used_pct:.1f}% used of "
                f"{self.budget_pct:.0f}% annual allowance. Only mandatory risk "
                f"trades are permitted."
            )
        return (
            f"Turnover used {self.used_pct:.1f}% of the {self.budget_pct:.0f}% "
            f"annual budget ({self.utilisation_pct:.0f}% of allowance); "
            f"Rs {self.remaining_value():,.0f} of discretionary trading remains."
        )


class TurnoverTracker:
    """Measures trailing turnover against the annual budget."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    @property
    def budget_pct(self) -> float:
        return float(self._config.get("risk.annual_turnover_budget_pct", 75.0) or 75.0)

    def status(self, as_of: Optional[date] = None) -> TurnoverStatus:
        """Compute trailing 12-month turnover from the trade journal."""
        from src.journal.repository import JournalRepository

        as_of = as_of or date.today()
        window_start = as_of - timedelta(days=365)
        repo = JournalRepository(self._config)

        purchases, sales = repo.traded_value_between(window_start, as_of)
        average_net_assets = repo.average_net_assets_between(window_start, as_of)

        if average_net_assets <= 0:
            average_net_assets = self._config.secrets.initial_capital

        # The lesser of purchases and sales: buying with new cash is not
        # portfolio churn, and this is the SEC/AMFI convention.
        churn = min(purchases, sales)
        used_pct = (
            churn / average_net_assets * 100.0 if average_net_assets > 0 else 0.0
        )

        status = TurnoverStatus(
            budget_pct=self.budget_pct,
            used_pct=round(used_pct, 2),
            purchases=purchases,
            sales=sales,
            average_net_assets=average_net_assets,
            window_days=365,
        )
        logger.info(status.describe())
        return status

    def would_exceed(
        self, status: TurnoverStatus, trade_value: float
    ) -> bool:
        """True when adding this discretionary trade would break the budget."""
        if status.average_net_assets <= 0:
            return False
        additional_pct = trade_value / status.average_net_assets * 100.0
        return (status.used_pct + additional_pct) > status.budget_pct
