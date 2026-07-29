"""Position sizing.

Size is a risk decision, not an afterthought. A new position starts at the
fund's target weight, then gets scaled down for volatility and for liquidity,
so a thin or wild stock never enters at the same size as a large steady one.
Every size is finally clipped by the position cap and by available cash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.config import Config, get_config
from src.logging_setup import get_logger
from src.models import PortfolioState, TechnicalSnapshot

logger = get_logger(__name__)

# Volatility the target weight is calibrated for. A name at twice this vol is
# sized at roughly half weight.
REFERENCE_VOLATILITY_PCT = 25.0
MIN_SIZE_FACTOR = 0.4
MAX_SIZE_FACTOR = 1.0


@dataclass
class SizingDecision:
    quantity: int
    target_weight_pct: float
    value: float
    reasons: list

    @property
    def is_actionable(self) -> bool:
        return self.quantity > 0


class PositionSizer:
    """Computes how many shares to buy."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    def target_weight(self) -> float:
        """Equal weight across the target number of positions, net of cash."""
        positions = int(self._config.get("fund.target_positions", 25) or 25)
        cash_buffer = float(self._config.get("fund.min_cash_buffer_pct", 5.0) or 5.0)
        return max((100.0 - cash_buffer) / max(positions, 1), 0.1)

    def size_new_position(
        self,
        portfolio: PortfolioState,
        symbol: str,
        price: float,
        technical: Optional[TechnicalSnapshot] = None,
    ) -> SizingDecision:
        reasons: list = []
        if price <= 0:
            return SizingDecision(0, 0.0, 0.0, ["no valid price"])

        total_value = portfolio.total_value
        if total_value <= 0:
            return SizingDecision(0, 0.0, 0.0, ["portfolio has no value"])

        weight = self.target_weight()
        reasons.append(f"base target weight {weight:.2f}%")

        # --- Volatility scaling ---
        if technical and technical.volatility_annualised:
            volatility = technical.volatility_annualised
            factor = max(
                min(REFERENCE_VOLATILITY_PCT / volatility, MAX_SIZE_FACTOR),
                MIN_SIZE_FACTOR,
            )
            if factor < 1.0:
                weight *= factor
                reasons.append(
                    f"scaled to {factor:.2f}x for {volatility:.0f}% volatility"
                )

        # --- Position cap ---
        cap = float(self._config.get("risk.max_position_pct", 8.0))
        if weight > cap:
            weight = cap
            reasons.append(f"capped at {cap:.1f}% position limit")

        value = weight / 100.0 * total_value

        # --- Cash constraint: never breach the cash buffer ---
        buffer_pct = float(self._config.get("fund.min_cash_buffer_pct", 5.0) or 5.0)
        deployable = portfolio.cash - (buffer_pct / 100.0 * total_value)
        if deployable <= 0:
            return SizingDecision(
                0,
                weight,
                0.0,
                reasons + [f"no cash above the {buffer_pct:.0f}% buffer"],
            )
        if value > deployable:
            value = deployable
            reasons.append(f"limited to Rs {deployable:,.0f} of deployable cash")

        # --- Liquidity constraint: stay a small share of daily volume ---
        max_adv_pct = self._config.get("universe.max_position_pct_of_adv")
        if (
            max_adv_pct is not None
            and technical
            and technical.avg_daily_turnover
            and technical.avg_daily_turnover > 0
        ):
            liquidity_cap = float(max_adv_pct) / 100.0 * technical.avg_daily_turnover
            if value > liquidity_cap:
                value = liquidity_cap
                reasons.append(
                    f"limited to {max_adv_pct}% of Rs "
                    f"{technical.avg_daily_turnover / 1e7:.1f}cr daily turnover"
                )

        quantity = int(value // price)
        if quantity <= 0:
            return SizingDecision(
                0, weight, 0.0, reasons + [f"one share costs more than Rs {value:,.0f}"]
            )

        actual_value = quantity * price
        return SizingDecision(
            quantity=quantity,
            target_weight_pct=round(actual_value / total_value * 100.0, 2),
            value=actual_value,
            reasons=reasons,
        )

    def size_add_to_position(
        self,
        portfolio: PortfolioState,
        symbol: str,
        price: float,
        technical: Optional[TechnicalSnapshot] = None,
    ) -> SizingDecision:
        """Top up an existing holding back toward its target weight."""
        position = portfolio.get(symbol)
        if position is None:
            return self.size_new_position(portfolio, symbol, price, technical)

        current_weight = portfolio.weight_of(symbol)
        target = min(
            self.target_weight(), float(self._config.get("risk.max_position_pct", 8.0))
        )
        shortfall = target - current_weight
        if shortfall <= 0:
            return SizingDecision(
                0, target, 0.0, [f"already at {current_weight:.2f}% of the fund"]
            )

        value = shortfall / 100.0 * portfolio.total_value
        buffer_pct = float(self._config.get("fund.min_cash_buffer_pct", 5.0) or 5.0)
        deployable = portfolio.cash - (buffer_pct / 100.0 * portfolio.total_value)
        reasons = [f"topping up from {current_weight:.2f}% toward {target:.2f}%"]

        if deployable <= 0:
            return SizingDecision(
                0, target, 0.0, reasons + [f"no cash above the {buffer_pct:.0f}% buffer"]
            )
        if value > deployable:
            value = deployable
            reasons.append(f"limited to Rs {deployable:,.0f} of deployable cash")

        quantity = int(value // price)
        if quantity <= 0:
            return SizingDecision(0, target, 0.0, reasons + ["shortfall below one share"])

        return SizingDecision(
            quantity=quantity,
            target_weight_pct=round(
                (position.market_value + quantity * price)
                / portfolio.total_value
                * 100.0,
                2,
            ),
            value=quantity * price,
            reasons=reasons,
        )
