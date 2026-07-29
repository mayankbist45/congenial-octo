"""Sell and rebalance triggers.

This module is the fund's sell discipline, written down before any position is
opened. It implements the six sell criteria used by professional equity managers
(down-from-cost, up-from-cost, target price, valuation level, fundamental
deterioration, opportunity cost) plus the portfolio-level reasons to trade
(drift bands, position and sector caps, risk limits).

The severity attached to each trigger decides how much discretion the decision
layer has:

- MANDATORY: the rule has fired and the trade happens. Used for hard stops and
  cap breaches, the cases where hesitation is what destroys portfolios.
- REVIEW: the situation warrants a look. The LLM weighs the news and the
  fundamentals and decides.
- OPPORTUNITY: a suggestion, acted on only if there is turnover budget left.

Nothing here proposes a trade on its own. If no trigger fires, the correct
output for the day is no trades, which is what a real fund manager does on the
overwhelming majority of days.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from src.config import Config, get_config
from src.logging_setup import get_logger
from src.models import (
    Action,
    FactorScores,
    FundamentalSnapshot,
    NewsDigest,
    PortfolioRiskProfile,
    PortfolioState,
    Position,
    Severity,
    TechnicalSnapshot,
    Trigger,
    TriggerType,
)

logger = get_logger(__name__)


class TriggerEngine:
    """Evaluates every rule against the current portfolio."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    # ------------------------------------------------------------------
    # Position-level sell triggers
    # ------------------------------------------------------------------
    def _stop_loss(self, position: Position) -> Optional[Trigger]:
        threshold = float(self._config.get("risk.stop_loss_pct", -15.0))
        if position.return_pct > threshold:
            return None
        return Trigger(
            symbol=position.symbol,
            trigger_type=TriggerType.STOP_LOSS,
            severity=Severity.MANDATORY,
            detail=(
                f"Down {position.return_pct:.1f}% from cost, past the "
                f"{threshold:.0f}% stop. Exit in full."
            ),
            suggested_action=Action.SELL,
            suggested_quantity=position.quantity,
            metrics={
                "return_pct": round(position.return_pct, 2),
                "threshold_pct": threshold,
                "avg_cost": position.avg_cost,
                "last_price": position.last_price,
            },
        )

    def _trailing_stop(self, position: Position) -> Optional[Trigger]:
        threshold = float(self._config.get("risk.trailing_stop_pct", -20.0))
        drawdown = position.drawdown_from_peak_pct
        if not position.peak_price or drawdown > threshold:
            return None
        # Only meaningful once the position has actually been in profit; a name
        # that fell straight from entry is the stop-loss case, not this one.
        if position.peak_price <= position.avg_cost:
            return None
        return Trigger(
            symbol=position.symbol,
            trigger_type=TriggerType.TRAILING_STOP,
            severity=Severity.MANDATORY,
            detail=(
                f"Down {drawdown:.1f}% from its peak of {position.peak_price:.0f}, "
                f"past the {threshold:.0f}% trailing stop. Protect the gain."
            ),
            suggested_action=Action.SELL,
            suggested_quantity=position.quantity,
            metrics={
                "drawdown_from_peak_pct": round(drawdown, 2),
                "peak_price": position.peak_price,
                "threshold_pct": threshold,
            },
        )

    def _profit_target(self, position: Position) -> Optional[Trigger]:
        threshold = float(self._config.get("risk.profit_review_pct", 30.0))
        if position.return_pct < threshold:
            return None
        # Trim half rather than exit: the fund is long-horizon, and cutting
        # winners entirely is how managers cap their own upside.
        return Trigger(
            symbol=position.symbol,
            trigger_type=TriggerType.PROFIT_TARGET,
            severity=Severity.REVIEW,
            detail=(
                f"Up {position.return_pct:.1f}% from cost, past the "
                f"{threshold:.0f}% review level. Decide whether the thesis "
                f"still supports the higher price or whether to take some off."
            ),
            suggested_action=Action.TRIM,
            suggested_quantity=max(position.quantity // 2, 1),
            metrics={"return_pct": round(position.return_pct, 2)},
        )

    def _target_price(self, position: Position) -> Optional[Trigger]:
        if not position.target_price or position.target_price <= 0:
            return None
        if position.last_price < position.target_price:
            return None
        return Trigger(
            symbol=position.symbol,
            trigger_type=TriggerType.TARGET_PRICE,
            severity=Severity.REVIEW,
            detail=(
                f"Reached the {position.target_price:.0f} target price set at "
                f"entry (now {position.last_price:.0f}). Either raise the target "
                f"with a documented reason or realise the gain."
            ),
            suggested_action=Action.SELL,
            suggested_quantity=position.quantity,
            metrics={
                "target_price": position.target_price,
                "last_price": position.last_price,
            },
        )

    def _valuation_stretch(
        self, position: Position, fundamental: Optional[FundamentalSnapshot]
    ) -> Optional[Trigger]:
        multiple = self._config.get("risk.valuation_stretch_pe_multiple")
        if multiple is None or fundamental is None or not fundamental.pe_ratio:
            return None
        if not position.entry_pe or position.entry_pe <= 0:
            return None

        limit = position.entry_pe * float(multiple)
        if fundamental.pe_ratio < limit:
            return None
        return Trigger(
            symbol=position.symbol,
            trigger_type=TriggerType.VALUATION_STRETCH,
            severity=Severity.REVIEW,
            detail=(
                f"P/E has expanded from {position.entry_pe:.1f} at entry to "
                f"{fundamental.pe_ratio:.1f}, beyond {multiple}x. The return is "
                f"coming from re-rating rather than earnings."
            ),
            suggested_action=Action.TRIM,
            suggested_quantity=max(position.quantity // 3, 1),
            metrics={
                "entry_pe": position.entry_pe,
                "current_pe": fundamental.pe_ratio,
                "limit_pe": round(limit, 1),
            },
        )

    def _fundamental_deterioration(
        self,
        position: Position,
        fundamental: Optional[FundamentalSnapshot],
        scores: Optional[FactorScores],
    ) -> Optional[Trigger]:
        """Thesis break: the business, not the price, has got worse.

        This is the one sell reason that ignores price entirely. A name that
        fails the fund's own entry gates no longer qualifies to be owned.
        """
        if fundamental is None or scores is None:
            return None
        if scores.passes_gates:
            return None

        # Liquidity and missing-data failures are data problems, not thesis
        # breaks, so they must not trigger a sale on their own.
        substantive = [
            failure
            for failure in scores.gate_failures
            if not failure.startswith(("avg daily turnover", "insufficient"))
        ]
        if not substantive:
            return None

        return Trigger(
            symbol=position.symbol,
            trigger_type=TriggerType.FUNDAMENTAL_DETERIORATION,
            severity=Severity.REVIEW,
            detail=(
                "No longer passes the fund's own entry criteria: "
                + "; ".join(substantive)
                + ". If it would not be bought today, justify continuing to hold it."
            ),
            suggested_action=Action.SELL,
            suggested_quantity=position.quantity,
            metrics={
                "gate_failures": substantive,
                "composite_score": scores.composite,
            },
        )

    def _news_event(
        self, position: Position, digest: Optional[NewsDigest]
    ) -> Optional[Trigger]:
        if digest is None or position.symbol not in digest.flagged_symbols:
            return None
        note = digest.holdings_notes.get(position.symbol, "")
        return Trigger(
            symbol=position.symbol,
            trigger_type=TriggerType.NEWS_EVENT,
            severity=Severity.REVIEW,
            detail=(
                "News flagged as potentially thesis-changing: "
                + (note[:400] if note else "see the news digest")
            ),
            suggested_action=Action.HOLD,
            metrics={"news_note": note[:400]},
        )

    def _opportunity_cost(
        self,
        position: Position,
        scores: Dict[str, FactorScores],
        best_candidate: Optional[FactorScores],
        as_of: date,
    ) -> Optional[Trigger]:
        """A clearly better use of the capital exists."""
        if best_candidate is None:
            return None
        held_score = scores.get(position.symbol)
        if held_score is None:
            return None

        gap_required = float(self._config.get("risk.opportunity_cost_score_gap", 0.75))
        gap = best_candidate.composite - held_score.composite
        if gap < gap_required:
            return None

        # A new position gets time for its thesis to work before being judged
        # against the latest shiny alternative.
        grace_days = int(self._config.get("cadence.new_position_grace_days", 60) or 60)
        holding_days = position.holding_days(as_of)
        if holding_days is not None and holding_days < grace_days:
            return None

        return Trigger(
            symbol=position.symbol,
            trigger_type=TriggerType.OPPORTUNITY_COST,
            severity=Severity.OPPORTUNITY,
            detail=(
                f"Scores {held_score.composite:.2f}, the weakest in the "
                f"portfolio, while {best_candidate.symbol} scores "
                f"{best_candidate.composite:.2f} (gap {gap:.2f}). Consider "
                f"rotating the capital."
            ),
            suggested_action=Action.SELL,
            suggested_quantity=position.quantity,
            metrics={
                "held_score": held_score.composite,
                "candidate": best_candidate.symbol,
                "candidate_score": best_candidate.composite,
                "gap": round(gap, 3),
            },
        )

    # ------------------------------------------------------------------
    # Portfolio-level triggers
    # ------------------------------------------------------------------
    def _position_cap(
        self, position: Position, weight: float, total_value: float
    ) -> Optional[Trigger]:
        cap = float(self._config.get("risk.max_position_pct", 8.0))
        if weight <= cap:
            return None
        excess_value = (weight - cap) / 100.0 * total_value
        quantity = max(int(excess_value / position.last_price), 1)
        return Trigger(
            symbol=position.symbol,
            trigger_type=TriggerType.POSITION_CAP,
            severity=Severity.MANDATORY,
            detail=(
                f"Weight {weight:.1f}% breaches the {cap:.0f}% position cap. "
                f"Trim back to the cap."
            ),
            suggested_action=Action.TRIM,
            suggested_quantity=min(quantity, position.quantity),
            metrics={"weight_pct": round(weight, 2), "cap_pct": cap},
        )

    def _drift_band(
        self, position: Position, weight: float, target_weight: float, total_value: float
    ) -> Optional[Trigger]:
        """Threshold rebalancing.

        Trading only when a weight has drifted outside its band is what keeps
        turnover low. Calendar rebalancing every day would generate constant
        small trades whose costs would swamp the benefit.
        """
        band = float(self._config.get("risk.drift_band_pct", 2.0))
        drift = weight - target_weight
        if abs(drift) <= band:
            return None

        excess_value = abs(drift) / 100.0 * total_value
        quantity = max(int(excess_value / position.last_price), 1)

        if drift > 0:
            return Trigger(
                symbol=position.symbol,
                trigger_type=TriggerType.DRIFT_BAND,
                severity=Severity.OPPORTUNITY,
                detail=(
                    f"Weight {weight:.1f}% has drifted {drift:+.1f}% above the "
                    f"{target_weight:.1f}% target, outside the {band:.0f}% band."
                ),
                suggested_action=Action.TRIM,
                suggested_quantity=min(quantity, position.quantity),
                metrics={
                    "weight_pct": round(weight, 2),
                    "target_pct": round(target_weight, 2),
                    "drift_pct": round(drift, 2),
                },
            )

        return Trigger(
            symbol=position.symbol,
            trigger_type=TriggerType.DRIFT_BAND,
            severity=Severity.OPPORTUNITY,
            detail=(
                f"Weight {weight:.1f}% has drifted {drift:+.1f}% below the "
                f"{target_weight:.1f}% target, outside the {band:.0f}% band."
            ),
            suggested_action=Action.ADD,
            suggested_quantity=quantity,
            metrics={
                "weight_pct": round(weight, 2),
                "target_pct": round(target_weight, 2),
                "drift_pct": round(drift, 2),
            },
        )

    def _sector_caps(self, portfolio: PortfolioState) -> List[Trigger]:
        cap = float(self._config.get("risk.max_sector_pct", 25.0))
        triggers: List[Trigger] = []

        for sector, weight in portfolio.sector_weights().items():
            if weight <= cap:
                continue
            # Trim the sector's weakest-performing name first.
            members = [p for p in portfolio.positions if p.sector == sector]
            if not members:
                continue
            members.sort(key=lambda p: p.return_pct)
            worst = members[0]
            excess_value = (weight - cap) / 100.0 * portfolio.total_value
            quantity = max(int(excess_value / worst.last_price), 1)

            triggers.append(
                Trigger(
                    symbol=worst.symbol,
                    trigger_type=TriggerType.SECTOR_CAP,
                    severity=Severity.MANDATORY,
                    detail=(
                        f"{sector} is {weight:.1f}% of the fund, breaching the "
                        f"{cap:.0f}% sector cap. Trimming {worst.symbol}, the "
                        f"weakest name in the sector."
                    ),
                    suggested_action=Action.TRIM,
                    suggested_quantity=min(quantity, worst.quantity),
                    metrics={
                        "sector": sector,
                        "sector_weight_pct": round(weight, 2),
                        "cap_pct": cap,
                    },
                )
            )
        return triggers

    def _risk_limits(
        self, portfolio: PortfolioState, risk_profile: Optional[PortfolioRiskProfile]
    ) -> List[Trigger]:
        if risk_profile is None:
            return []

        triggers: List[Trigger] = []
        limit = self._config.get("risk.max_single_position_risk_contribution_pct")
        if limit is not None:
            for symbol, metrics in risk_profile.per_position.items():
                contribution = metrics.risk_contribution_pct
                if contribution is None or contribution <= float(limit):
                    continue
                position = portfolio.get(symbol)
                if position is None:
                    continue
                triggers.append(
                    Trigger(
                        symbol=symbol,
                        trigger_type=TriggerType.RISK_LIMIT,
                        severity=Severity.REVIEW,
                        detail=(
                            f"Contributes {contribution:.0f}% of portfolio risk "
                            f"(limit {limit}%) on a {portfolio.weight_of(symbol):.1f}% "
                            f"weight. Its volatility is doing more damage than its "
                            f"size suggests."
                        ),
                        suggested_action=Action.TRIM,
                        suggested_quantity=max(position.quantity // 4, 1),
                        metrics={"risk_contribution_pct": contribution},
                    )
                )
        return triggers

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    def evaluate(
        self,
        portfolio: PortfolioState,
        scores: Dict[str, FactorScores],
        fundamentals: Dict[str, FundamentalSnapshot],
        technicals: Dict[str, TechnicalSnapshot],
        digest: Optional[NewsDigest] = None,
        risk_profile: Optional[PortfolioRiskProfile] = None,
        best_candidate: Optional[FactorScores] = None,
        is_deep_review: bool = False,
    ) -> List[Trigger]:
        """Return every trigger that has fired today."""
        triggers: List[Trigger] = []
        if not portfolio.positions:
            return triggers

        weights = portfolio.weights()
        total_value = portfolio.total_value
        target_positions = int(self._config.get("fund.target_positions", 25) or 25)
        # Equal-weight target, adjusted for the cash buffer the fund holds back.
        cash_buffer = float(self._config.get("fund.min_cash_buffer_pct", 5.0) or 5.0)
        target_weight = max((100.0 - cash_buffer) / max(target_positions, 1), 0.1)

        weakest_holding = min(
            (scores[p.symbol] for p in portfolio.positions if p.symbol in scores),
            key=lambda s: s.composite,
            default=None,
        )

        for position in portfolio.positions:
            symbol = position.symbol
            weight = weights.get(symbol, 0.0)
            fundamental = fundamentals.get(symbol)
            score = scores.get(symbol)

            candidates = [
                self._stop_loss(position),
                self._trailing_stop(position),
                self._profit_target(position),
                self._target_price(position),
                self._valuation_stretch(position, fundamental),
                self._fundamental_deterioration(position, fundamental, score),
                self._news_event(position, digest),
                self._position_cap(position, weight, total_value),
            ]

            # Opportunity-cost rotation is a deliberate act of portfolio
            # housekeeping, so it is only considered for the single weakest
            # holding and only during a quarterly deep review.
            if (
                is_deep_review
                and weakest_holding is not None
                and weakest_holding.symbol == symbol
            ):
                candidates.append(
                    self._opportunity_cost(
                        position, scores, best_candidate, portfolio.as_of
                    )
                )

            # Drift bands are also a deep-review activity, not a daily one.
            if is_deep_review:
                candidates.append(
                    self._drift_band(position, weight, target_weight, total_value)
                )

            triggers.extend(t for t in candidates if t is not None)

        triggers.extend(self._sector_caps(portfolio))
        triggers.extend(self._risk_limits(portfolio, risk_profile))

        # Report the most serious triggers first.
        order = {Severity.MANDATORY: 0, Severity.REVIEW: 1, Severity.OPPORTUNITY: 2}
        triggers.sort(key=lambda t: order.get(t.severity, 3))

        if triggers:
            counts: Dict[str, int] = {}
            for trigger in triggers:
                counts[trigger.severity.value] = counts.get(trigger.severity.value, 0) + 1
            logger.info(
                "Triggers fired: %s",
                ", ".join(f"{count} {name}" for name, count in counts.items()),
            )
        else:
            logger.info("No triggers fired: the portfolio needs no action today")

        return triggers
