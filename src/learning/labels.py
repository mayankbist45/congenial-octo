"""Conservative labels for reviewed investment decisions."""

from __future__ import annotations

from typing import Any, Dict, Optional

GOOD_EXCESS_PCT = 3.0
BAD_EXCESS_PCT = -3.0

LABEL_RECOMMENDATIONS = {
    "good_decision_good_outcome": "Keep this pattern under observation; outcome supported the original process.",
    "good_decision_bad_outcome": "Do not overreact; this looks like a reasonable process with an adverse result.",
    "bad_decision_good_outcome": "Review the process despite the gain; luck may have rewarded a weak decision.",
    "bad_decision_bad_outcome": "Research this pattern before repeating it.",
    "overpaid_for_growth": "Test stricter valuation checks for growth names before buying.",
    "bought_weak_quality": "Test stronger quality and balance-sheet gates before buying.",
    "bought_without_momentum": "Test requiring confirmed trend strength for new buys.",
    "sold_too_early": "Review sell discipline and whether winners are being cut before thesis break.",
    "held_loser_too_long": "Review sell discipline for thesis breaks and underperforming holds.",
    "risk_engine_saved_us": "Keep the risk rule under observation; it avoided later underperformance.",
    "risk_engine_too_strict": "Research whether this risk rule is blocking attractive opportunities.",
    "insufficient_evidence": "Collect more price and benchmark history before learning from this decision.",
}


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(needle in lower for needle in needles)


def recommendation_for(label: str) -> str:
    return LABEL_RECOMMENDATIONS.get(label, "Review this decision pattern.")


def classify_review(
    proposal: Dict[str, Any],
    stock_return_pct: Optional[float],
    benchmark_return_pct: Optional[float],
    excess_return_pct: Optional[float],
) -> str:
    """Assign an intentionally cautious mistake/success label."""
    if stock_return_pct is None or benchmark_return_pct is None or excess_return_pct is None:
        return "insufficient_evidence"

    action = str(proposal.get("action") or "").upper()
    status = str(proposal.get("status") or "").lower()
    decided_by = str(proposal.get("decided_by") or "").lower()
    source = str(proposal.get("source") or "").lower()
    text = " ".join(
        str(proposal.get(key) or "")
        for key in ("rationale", "thesis", "warnings", "trigger_types", "rejection_reason")
    )

    is_buy = action in ("BUY", "ADD")
    is_sell = action in ("SELL", "TRIM")
    was_rejected = status in ("rejected", "expired", "failed")
    risk_blocked = was_rejected and (decided_by == "risk_engine" or source == "risk_engine")

    if risk_blocked and is_buy:
        return "risk_engine_too_strict" if excess_return_pct >= GOOD_EXCESS_PCT else "risk_engine_saved_us"

    if risk_blocked and is_sell:
        return "risk_engine_saved_us" if excess_return_pct >= GOOD_EXCESS_PCT else "risk_engine_too_strict"

    if was_rejected and is_sell and excess_return_pct <= BAD_EXCESS_PCT:
        return "held_loser_too_long"

    if is_sell:
        if excess_return_pct >= GOOD_EXCESS_PCT:
            return "sold_too_early"
        if excess_return_pct <= BAD_EXCESS_PCT:
            return "good_decision_good_outcome"
        return "good_decision_bad_outcome"

    if is_buy:
        if excess_return_pct >= GOOD_EXCESS_PCT:
            return "good_decision_good_outcome"
        if excess_return_pct <= BAD_EXCESS_PCT:
            if _contains_any(text, ("valuation", "p/e", "pe ", "multiple", "expensive")):
                return "overpaid_for_growth"
            if _contains_any(text, ("debt", "roe", "roce", "margin", "quality", "balance sheet")):
                return "bought_weak_quality"
            if _contains_any(text, ("momentum", "trend", "sma", "200-dma", "rsi")):
                return "bought_without_momentum"
            return "bad_decision_bad_outcome"
        return "good_decision_bad_outcome"

    return "insufficient_evidence"
