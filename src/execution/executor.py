"""Order execution.

Takes proposals the human approved and turns them into broker orders. Three
concerns dominate:

- **Limit prices, not market orders.** A market order in a mid-cap can fill far
  from the last traded price. Every order carries a limit a small distance
  through the touch: enough to fill, not enough to be run over.
- **Stale prices.** A proposal was priced at the previous close. If the stock
  gapped, the trade approved is not the trade being placed, so the order is
  abandoned rather than executed at a materially different price.
- **Journalling everything.** Fills, rejections and errors are all written to the
  journal. A missing record is worse than a failed order, because the fund's
  reconstruction of its own positions depends on this history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from src.broker.base import BrokerClient, BrokerError, OrderRequest, OrderResult
from src.config import Config, get_config
from src.journal.repository import JournalRepository
from src.logging_setup import get_logger
from src.models import Action, FundamentalSnapshot, TechnicalSnapshot

logger = get_logger(__name__)

SELL_ACTIONS = {Action.SELL.value, Action.TRIM.value}


@dataclass
class ExecutionOutcome:
    """What happened to one approved proposal."""

    proposal_id: Optional[int]
    symbol: str
    action: str
    quantity: int
    placed: bool
    price: Optional[float] = None
    order_id: Optional[str] = None
    status: str = ""
    error: Optional[str] = None
    dry_run: bool = False

    @property
    def value(self) -> float:
        return self.quantity * (self.price or 0.0)


@dataclass
class ExecutionSummary:
    outcomes: List[ExecutionOutcome] = field(default_factory=list)

    @property
    def placed(self) -> List[ExecutionOutcome]:
        return [o for o in self.outcomes if o.placed]

    @property
    def failed(self) -> List[ExecutionOutcome]:
        return [o for o in self.outcomes if not o.placed and o.status != "abandoned"]

    @property
    def skipped(self) -> List[ExecutionOutcome]:
        return [o for o in self.outcomes if o.status == "abandoned"]

    def describe(self) -> str:
        if not self.outcomes:
            return "No approved trades to execute."
        parts = [f"{len(self.placed)} order(s) placed"]
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return ", ".join(parts)


class OrderExecutor:
    """Places approved orders and records the results."""

    def __init__(
        self,
        broker: BrokerClient,
        config: Optional[Config] = None,
        repository: Optional[JournalRepository] = None,
    ):
        self._config = config or get_config()
        self._broker = broker
        self._repo = repository or JournalRepository(self._config)

    # ------------------------------------------------------------------
    def execute_approved(
        self,
        technicals: Optional[Dict[str, TechnicalSnapshot]] = None,
        fundamentals: Optional[Dict[str, FundamentalSnapshot]] = None,
    ) -> ExecutionSummary:
        """Execute every proposal sitting in the approved state."""
        return self.execute(
            self._repo.approved_proposals(), technicals, fundamentals
        )

    def execute(
        self,
        proposals: List[Dict[str, Any]],
        technicals: Optional[Dict[str, TechnicalSnapshot]] = None,
        fundamentals: Optional[Dict[str, FundamentalSnapshot]] = None,
    ) -> ExecutionSummary:
        summary = ExecutionSummary()
        if not proposals:
            logger.info("No approved proposals to execute")
            return summary

        # Sells first: the cash they release funds the buys.
        ordered = sorted(
            proposals, key=lambda p: 0 if p["action"] in SELL_ACTIONS else 1
        )

        for proposal in ordered:
            outcome = self._execute_one(proposal, technicals or {}, fundamentals or {})
            summary.outcomes.append(outcome)

        logger.info("Execution complete: %s", summary.describe())
        return summary

    # ------------------------------------------------------------------
    def _execute_one(
        self,
        proposal: Dict[str, Any],
        technicals: Dict[str, TechnicalSnapshot],
        fundamentals: Dict[str, FundamentalSnapshot],
    ) -> ExecutionOutcome:
        symbol = str(proposal["symbol"]).upper()
        action = str(proposal["action"]).upper()
        quantity = int(proposal["quantity"])
        proposal_id = proposal.get("id")
        side = "SELL" if action in SELL_ACTIONS else "BUY"

        reference_price = proposal.get("reference_price")
        live_price = self._live_price(symbol, technicals)

        if live_price is None:
            return self._abandon(
                proposal_id, symbol, action, quantity, "no live price available"
            )

        gap_reason = self._gap_check(reference_price, live_price)
        if gap_reason:
            return self._abandon(proposal_id, symbol, action, quantity, gap_reason)

        limit_price = self._limit_price(side, live_price)
        request = OrderRequest(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=str(self._config.get("execution.order_type", "LIMIT")),
            price=limit_price,
            product_type=str(self._config.get("execution.product_type", "DELIVERY")),
            exchange=str(self._config.get("execution.exchange", "NSE")),
        )

        try:
            result = self._broker.place_order(request)
        except BrokerError as exc:
            return self._abandon(
                proposal_id, symbol, action, quantity, f"broker error: {exc}", status="rejected"
            )

        return self._record(proposal, result, limit_price, live_price, fundamentals)

    def _live_price(
        self, symbol: str, technicals: Dict[str, TechnicalSnapshot]
    ) -> Optional[float]:
        """Prefer the broker's live quote; fall back to the last close."""
        try:
            price = self._broker.get_last_price(symbol)
            if price and price > 0:
                return float(price)
        except BrokerError as exc:
            logger.warning("Live quote for %s unavailable: %s", symbol, exc)

        technical = technicals.get(symbol)
        if technical and technical.has_price:
            logger.info("Using last close for %s; live quote unavailable", symbol)
            return technical.last_price
        return None

    def _gap_check(
        self, reference_price: Optional[float], live_price: float
    ) -> Optional[str]:
        """Refuse to trade a stock that has moved away from the approved price.

        The human approved a trade at a price. A large gap means the decision they
        signed off on no longer applies, so the correct action is to stop and let
        the next cycle re-evaluate.
        """
        if not reference_price or reference_price <= 0:
            return None
        tolerance = float(self._config.get("execution.max_slippage_pct", 1.0) or 1.0)
        # Overnight gaps are normal; the limit is set well wider than the intraday
        # slippage tolerance so only genuine repricing stops the trade.
        gap_limit = max(tolerance * 3.0, 3.0)
        drift_pct = (live_price / reference_price - 1.0) * 100.0
        if abs(drift_pct) > gap_limit:
            return (
                f"price moved {drift_pct:+.1f}% from the approved Rs "
                f"{reference_price:,.2f} to Rs {live_price:,.2f}, beyond the "
                f"{gap_limit:.1f}% gap tolerance"
            )
        return None

    def _limit_price(self, side: str, live_price: float) -> float:
        """Set a limit a small distance through the touch, rounded to a tick."""
        buffer_pct = float(
            self._config.get("execution.limit_price_buffer_pct", 0.3) or 0.3
        )
        factor = 1.0 + buffer_pct / 100.0 if side == "BUY" else 1.0 - buffer_pct / 100.0
        # NSE equity ticks are 5 paise.
        return round(round(live_price * factor / 0.05) * 0.05, 2)

    # ------------------------------------------------------------------
    def _abandon(
        self,
        proposal_id: Optional[int],
        symbol: str,
        action: str,
        quantity: int,
        reason: str,
        status: str = "abandoned",
    ) -> ExecutionOutcome:
        logger.warning("Not placing %s %s x%d: %s", action, symbol, quantity, reason)
        self._repo.record_trade(
            symbol=symbol,
            action=action,
            quantity=quantity,
            price=0.0,
            status=status,
            proposal_id=proposal_id,
            error=reason,
        )
        if proposal_id is not None:
            self._repo.set_proposal_status(
                proposal_id,
                "failed" if status == "rejected" else "abandoned",
                decided_by="executor",
                rejection_reason=reason,
            )
        return ExecutionOutcome(
            proposal_id=proposal_id,
            symbol=symbol,
            action=action,
            quantity=quantity,
            placed=False,
            status=status,
            error=reason,
        )

    def _record(
        self,
        proposal: Dict[str, Any],
        result: OrderResult,
        limit_price: float,
        live_price: float,
        fundamentals: Dict[str, FundamentalSnapshot],
    ) -> ExecutionOutcome:
        symbol = result.symbol
        action = str(proposal["action"]).upper()
        quantity = result.quantity
        proposal_id = proposal.get("id")

        # A limit order's true fill price is unknown until it fills; the limit is
        # the honest best estimate and is what the journal records.
        fill_price = result.price or limit_price

        if not result.success:
            logger.error("Order rejected for %s: %s", symbol, result.error)
            self._repo.record_trade(
                symbol=symbol,
                action=action,
                quantity=quantity,
                price=0.0,
                status="rejected",
                proposal_id=proposal_id,
                broker_order_id=result.order_id,
                error=result.error,
                dry_run=result.dry_run,
            )
            if proposal_id is not None:
                self._repo.set_proposal_status(
                    proposal_id,
                    "failed",
                    decided_by="executor",
                    rejection_reason=result.error or "broker rejected the order",
                )
            return ExecutionOutcome(
                proposal_id=proposal_id,
                symbol=symbol,
                action=action,
                quantity=quantity,
                placed=False,
                status="rejected",
                error=result.error,
                dry_run=result.dry_run,
            )

        realised = self._realised_pnl(symbol, action, quantity, fill_price)
        status = "dry_run" if result.dry_run else "submitted"

        self._repo.record_trade(
            symbol=symbol,
            action=action,
            quantity=quantity,
            price=fill_price,
            status=status,
            proposal_id=proposal_id,
            broker_order_id=result.order_id,
            dry_run=result.dry_run,
            realised_pnl=realised,
        )
        if proposal_id is not None:
            self._repo.set_proposal_status(proposal_id, "executed", decided_by="executor")

        self._update_position_meta(proposal, action, fill_price, fundamentals)

        logger.info(
            "%s %s x%d at Rs %.2f (order %s)%s",
            action,
            symbol,
            quantity,
            fill_price,
            result.order_id or "-",
            " [DRY RUN]" if result.dry_run else "",
        )
        return ExecutionOutcome(
            proposal_id=proposal_id,
            symbol=symbol,
            action=action,
            quantity=quantity,
            placed=True,
            price=fill_price,
            order_id=result.order_id,
            status=status,
            dry_run=result.dry_run,
        )

    def _realised_pnl(
        self, symbol: str, action: str, quantity: int, price: float
    ) -> Optional[float]:
        """Realised P&L on a sale, against the average cost from the journal."""
        if action not in SELL_ACTIONS:
            return None
        for position in self._repo.reconstruct_positions():
            if position["symbol"] == symbol and position["avg_cost"] > 0:
                return round((price - position["avg_cost"]) * quantity, 2)
        return None

    def _update_position_meta(
        self,
        proposal: Dict[str, Any],
        action: str,
        fill_price: float,
        fundamentals: Dict[str, FundamentalSnapshot],
    ) -> None:
        """Capture, or retire, the context the sell triggers will need later."""
        symbol = str(proposal["symbol"]).upper()

        if action in SELL_ACTIONS:
            remaining = next(
                (
                    p["quantity"]
                    for p in self._repo.reconstruct_positions()
                    if p["symbol"] == symbol
                ),
                0,
            )
            if remaining <= 0:
                self._repo.clear_position_meta(symbol)
            return

        fundamental = fundamentals.get(symbol)
        self._repo.upsert_position_meta(
            symbol=symbol,
            entry_date=date.today(),
            entry_price=fill_price,
            entry_pe=fundamental.pe_ratio if fundamental else None,
            peak_price=fill_price,
            thesis=proposal.get("thesis") or proposal.get("rationale") or "",
            target_price=proposal.get("target_price"),
        )
