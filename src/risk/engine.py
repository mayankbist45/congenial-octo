"""Risk engine: the final gate before anything reaches the approval queue.

Every proposal, whether it came from the LLM or from a mandatory trigger, is
re-validated here. The engine's job is to be the part of the system that cannot
be talked out of a rule. It checks, in order:

1. the trade is well formed and the symbol is investable
2. the fund would not exceed a position, sector or count limit
3. the cash buffer survives
4. the position remains liquid enough to exit
5. discretionary trades still fit inside the turnover budget

Failing a hard check rejects the proposal outright. Softer concerns are attached
as warnings so they surface in the dashboard for the human approver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.config import Config, get_config
from src.logging_setup import get_logger
from src.models import (
    Action,
    FactorScores,
    PortfolioState,
    TechnicalSnapshot,
    TradeProposal,
    Trigger,
    TriggerType,
    Severity,
)
from src.risk.sizing import PositionSizer
from src.risk.turnover import TurnoverStatus, TurnoverTracker
from src.universe import Universe, load_universe

logger = get_logger(__name__)

# Triggers whose trades must always be allowed through, even with no turnover
# budget left. Risk control is not discretionary.
MANDATORY_TRIGGERS = {
    TriggerType.STOP_LOSS.value,
    TriggerType.TRAILING_STOP.value,
    TriggerType.POSITION_CAP.value,
    TriggerType.SECTOR_CAP.value,
}


@dataclass
class ValidationResult:
    approved: List[TradeProposal] = field(default_factory=list)
    rejected: List[Tuple[TradeProposal, str]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return f"{len(self.approved)} approved, {len(self.rejected)} rejected"


class RiskEngine:
    """Validates and, where needed, generates trades."""

    def __init__(
        self,
        config: Optional[Config] = None,
        universe: Optional[Universe] = None,
    ):
        self._config = config or get_config()
        self._universe = universe or load_universe(self._config)
        self._sizer = PositionSizer(self._config)
        self._turnover = TurnoverTracker(self._config)

    # ------------------------------------------------------------------
    # Mandatory trades
    # ------------------------------------------------------------------
    def mandatory_proposals(
        self,
        triggers: List[Trigger],
        portfolio: PortfolioState,
        technicals: Dict[str, TechnicalSnapshot],
    ) -> List[TradeProposal]:
        """Turn MANDATORY triggers into trades directly.

        These bypass the decision layer entirely. A stop-loss is not a matter of
        opinion, and waiting for a language model to agree with it would defeat
        the purpose of having a stop.
        """
        proposals: List[TradeProposal] = []
        for trigger in triggers:
            if trigger.severity != Severity.MANDATORY:
                continue
            position = portfolio.get(trigger.symbol)
            if position is None:
                continue

            quantity = trigger.suggested_quantity or position.quantity
            quantity = min(max(int(quantity), 1), position.quantity)
            price = position.last_price

            proposals.append(
                TradeProposal(
                    symbol=trigger.symbol,
                    action=trigger.suggested_action or Action.SELL,
                    quantity=quantity,
                    rationale=f"[Rule enforced] {trigger.detail}",
                    trigger_types=[trigger.trigger_type.value],
                    confidence=1.0,
                    reference_price=price,
                    estimated_value=quantity * price,
                    company_name=self._universe.company_name_of(trigger.symbol),
                    sector=self._universe.sector_of(trigger.symbol),
                    source="risk_engine",
                )
            )

        if proposals:
            logger.info(
                "Generated %d mandatory risk trade(s): %s",
                len(proposals),
                ", ".join(f"{p.action.value} {p.symbol}" for p in proposals),
            )
        return proposals

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate(
        self,
        proposals: List[TradeProposal],
        portfolio: PortfolioState,
        technicals: Dict[str, TechnicalSnapshot],
        scores: Optional[Dict[str, FactorScores]] = None,
        turnover: Optional[TurnoverStatus] = None,
    ) -> ValidationResult:
        """Check every proposal against the fund's hard rules."""
        result = ValidationResult()
        scores = scores or {}
        turnover = turnover or self._turnover.status(portfolio.as_of)

        # Simulate the portfolio as trades are accepted, so a batch of proposals
        # cannot collectively breach a limit that each one respects alone.
        projected_cash = portfolio.cash
        projected_values: Dict[str, float] = {
            p.symbol: p.market_value for p in portfolio.positions
        }
        total_value = portfolio.total_value
        discretionary_value = 0.0

        # Sells first: they release cash and headroom that buys then use.
        ordered = sorted(proposals, key=lambda p: 0 if not p.is_buy_side else 1)

        for proposal in ordered:
            failure = self._validate_one(
                proposal,
                portfolio,
                technicals,
                scores,
                projected_cash,
                projected_values,
                total_value,
                turnover,
                discretionary_value,
            )
            if failure is not None:
                result.rejected.append((proposal, failure))
                logger.info(
                    "REJECTED %s %s x%d: %s",
                    proposal.action.value,
                    proposal.symbol,
                    proposal.quantity,
                    failure,
                )
                continue

            value = proposal.quantity * (proposal.reference_price or 0.0)
            if proposal.is_buy_side:
                projected_cash -= value
                projected_values[proposal.symbol] = (
                    projected_values.get(proposal.symbol, 0.0) + value
                )
            else:
                projected_cash += value
                projected_values[proposal.symbol] = max(
                    projected_values.get(proposal.symbol, 0.0) - value, 0.0
                )

            if not self._is_mandatory(proposal):
                discretionary_value += value

            result.approved.append(proposal)

        logger.info("Risk validation: %s", result.summary)
        return result

    @staticmethod
    def _is_mandatory(proposal: TradeProposal) -> bool:
        return any(t in MANDATORY_TRIGGERS for t in proposal.trigger_types)

    def _validate_one(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioState,
        technicals: Dict[str, TechnicalSnapshot],
        scores: Dict[str, FactorScores],
        projected_cash: float,
        projected_values: Dict[str, float],
        total_value: float,
        turnover: TurnoverStatus,
        discretionary_value: float,
    ) -> Optional[str]:
        """Return a rejection reason, or None when the trade is acceptable."""
        symbol = proposal.symbol.upper()

        # --- 1. Well-formedness ---
        if proposal.quantity <= 0:
            return "quantity must be positive"

        if not self._universe.contains(symbol):
            return f"{symbol} is not in the Nifty 500 investable universe"

        technical = technicals.get(symbol)
        price = proposal.reference_price or (technical.last_price if technical else None)
        if not price or price <= 0:
            return f"no reliable price for {symbol}"
        proposal.reference_price = price
        proposal.estimated_value = proposal.quantity * price

        position = portfolio.get(symbol)

        # --- 2. Sell-side checks ---
        if not proposal.is_buy_side:
            if position is None:
                return f"cannot sell {symbol}: not held"
            if proposal.quantity > position.quantity:
                return (
                    f"cannot sell {proposal.quantity} of {symbol}: only "
                    f"{position.quantity} held"
                )
            return None

        # --- 3. Buy-side checks ---
        if proposal.action == Action.BUY and position is not None:
            return f"{symbol} already held; use ADD to increase the position"

        if proposal.action == Action.ADD and position is None:
            return f"cannot add to {symbol}: not held"

        # Gate failures block new purchases. An existing holding may be topped
        # up while under review, but a fresh position must clear every gate.
        score = scores.get(symbol)
        if proposal.action == Action.BUY and score is not None and not score.passes_gates:
            return (
                f"{symbol} fails fundamental gates: "
                + "; ".join(score.gate_failures[:2])
            )

        value = proposal.quantity * price

        # --- Cash buffer ---
        buffer_pct = float(self._config.get("fund.min_cash_buffer_pct", 5.0) or 5.0)
        minimum_cash = buffer_pct / 100.0 * total_value
        if projected_cash - value < minimum_cash:
            return (
                f"would leave cash at Rs {projected_cash - value:,.0f}, below the "
                f"{buffer_pct:.0f}% buffer of Rs {minimum_cash:,.0f}"
            )

        # --- Position cap ---
        cap = float(self._config.get("risk.max_position_pct", 8.0))
        projected_weight = (
            (projected_values.get(symbol, 0.0) + value) / total_value * 100.0
        )
        if projected_weight > cap:
            return (
                f"would take {symbol} to {projected_weight:.1f}%, past the "
                f"{cap:.0f}% position cap"
            )

        # --- Sector cap ---
        sector = self._universe.sector_of(symbol)
        sector_cap = float(self._config.get("risk.max_sector_pct", 25.0))
        sector_value = value + sum(
            market_value
            for held_symbol, market_value in projected_values.items()
            if self._universe.sector_of(held_symbol) == sector
        )
        projected_sector_weight = sector_value / total_value * 100.0
        if projected_sector_weight > sector_cap:
            return (
                f"would take {sector} to {projected_sector_weight:.1f}%, past the "
                f"{sector_cap:.0f}% sector cap"
            )

        # --- Position count ---
        if proposal.action == Action.BUY:
            projected_count = sum(1 for v in projected_values.values() if v > 0) + 1
            maximum = int(self._config.get("fund.max_positions", 30) or 30)
            if projected_count > maximum:
                return (
                    f"would hold {projected_count} positions, past the maximum of "
                    f"{maximum}"
                )

        # --- Liquidity ---
        max_adv_pct = self._config.get("universe.max_position_pct_of_adv")
        if (
            max_adv_pct is not None
            and technical
            and technical.avg_daily_turnover
            and technical.avg_daily_turnover > 0
        ):
            order_pct_of_adv = value / technical.avg_daily_turnover * 100.0
            if order_pct_of_adv > float(max_adv_pct):
                return (
                    f"order is {order_pct_of_adv:.1f}% of {symbol}'s average daily "
                    f"turnover, above the {max_adv_pct}% limit"
                )

        # --- Turnover budget ---
        if not self._is_mandatory(proposal):
            if turnover.is_exhausted:
                return (
                    f"turnover budget exhausted ({turnover.used_pct:.1f}% of "
                    f"{turnover.budget_pct:.0f}%); only mandatory risk trades allowed"
                )
            if self._turnover.would_exceed(turnover, discretionary_value + value):
                return (
                    f"would breach the {turnover.budget_pct:.0f}% annual turnover "
                    f"budget (already {turnover.used_pct:.1f}% used)"
                )

        # --- Soft warnings for the human approver ---
        if technical:
            if technical.rsi_14 is not None and technical.rsi_14 > 75:
                proposal.warnings.append(
                    f"RSI {technical.rsi_14:.0f} is overbought; entry may be poorly timed"
                )
            if technical.above_sma_200 is False:
                proposal.warnings.append(
                    "trading below its 200-day moving average (downtrend)"
                )
        if score is not None and score.percentile is not None and score.percentile < 50:
            proposal.warnings.append(
                f"ranks in the {score.percentile:.0f}th percentile of the universe"
            )

        return None

    # ------------------------------------------------------------------
    # Deployment of idle cash
    # ------------------------------------------------------------------
    def cash_deployment_triggers(
        self, portfolio: PortfolioState, candidates: List[FactorScores]
    ) -> List[Trigger]:
        """Flag that the fund is holding more cash than its mandate intends.

        An equity fund sitting in cash is making an unintended market-timing
        bet, so persistent excess cash is itself a trigger to act.
        """
        if not candidates:
            return []

        buffer_pct = float(self._config.get("fund.min_cash_buffer_pct", 5.0) or 5.0)
        # Allow meaningful headroom before acting, so ordinary cash movements do
        # not cause daily nibbling.
        threshold = buffer_pct * 2.0
        if portfolio.cash_pct <= threshold:
            return []

        target_positions = int(self._config.get("fund.target_positions", 25) or 25)
        held = len(portfolio.positions)
        if held >= int(self._config.get("fund.max_positions", 30) or 30):
            return []

        trigger_type = (
            TriggerType.INITIAL_BUILD if held == 0 else TriggerType.UNDERWEIGHT_DEPLOY
        )
        detail = (
            f"Cash is {portfolio.cash_pct:.1f}% of the fund against a "
            f"{buffer_pct:.0f}% target buffer, and the fund holds {held} of "
            f"{target_positions} intended positions. Idle cash in an equity fund "
            f"is an unintended market-timing bet."
        )

        return [
            Trigger(
                symbol=candidates[0].symbol,
                trigger_type=trigger_type,
                severity=Severity.OPPORTUNITY,
                detail=detail,
                suggested_action=Action.BUY,
                metrics={
                    "cash_pct": round(portfolio.cash_pct, 2),
                    "positions_held": held,
                    "target_positions": target_positions,
                    "top_candidates": [c.symbol for c in candidates[:10]],
                },
            )
        ]

    def turnover_status(self, as_of=None) -> TurnoverStatus:
        return self._turnover.status(as_of)

    @property
    def sizer(self) -> PositionSizer:
        return self._sizer
