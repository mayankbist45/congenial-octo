"""Subscription tier catalog — limits aligned to docs/pricing/PRICING_MODEL.md."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class TierLimits:
    tier: str
    label: str
    price_inr: int
    price_display: str
    seat_limit: int
    live_trading: bool
    max_active_funds: Optional[int]
    max_books: int
    max_lines_per_book: int
    agent_runs_per_month: int
    agent_ic_memo_per_month: int
    holding_intel_per_day: int
    manual_refresh_per_month: int
    long_cycle_candidates: Optional[int]  # None = full universe
    concurrent_agents: int
    dashboard_reads_per_hour: int
    razorpay_plan_env: str

    @property
    def paper_only(self) -> bool:
        return not self.live_trading


TIERS: Dict[str, TierLimits] = {
    "sandbox": TierLimits(
        tier="sandbox",
        label="Sandbox",
        price_inr=24999,
        price_display="₹24,999",
        seat_limit=1,
        live_trading=False,
        max_active_funds=1,
        max_books=2,
        max_lines_per_book=15,
        agent_runs_per_month=5,
        agent_ic_memo_per_month=1,
        holding_intel_per_day=10,
        manual_refresh_per_month=0,
        long_cycle_candidates=150,
        concurrent_agents=1,
        dashboard_reads_per_hour=300,
        razorpay_plan_env="RAZORPAY_PLAN_SANDBOX",
    ),
    "desk": TierLimits(
        tier="desk",
        label="Desk",
        price_inr=74999,
        price_display="₹74,999",
        seat_limit=1,
        live_trading=True,
        max_active_funds=1,
        max_books=5,
        max_lines_per_book=25,
        agent_runs_per_month=20,
        agent_ic_memo_per_month=5,
        holding_intel_per_day=30,
        manual_refresh_per_month=2,
        long_cycle_candidates=150,
        concurrent_agents=1,
        dashboard_reads_per_hour=600,
        razorpay_plan_env="RAZORPAY_PLAN_DESK",
    ),
    "fund": TierLimits(
        tier="fund",
        label="Fund",
        price_inr=149999,
        price_display="₹1,49,999",
        seat_limit=1,
        live_trading=True,
        max_active_funds=3,
        max_books=5,
        max_lines_per_book=25,
        agent_runs_per_month=80,
        agent_ic_memo_per_month=15,
        holding_intel_per_day=100,
        manual_refresh_per_month=5,
        long_cycle_candidates=None,
        concurrent_agents=2,
        dashboard_reads_per_hour=1200,
        razorpay_plan_env="RAZORPAY_PLAN_FUND",
    ),
    "enterprise": TierLimits(
        tier="enterprise",
        label="Enterprise",
        price_inr=299999,
        price_display="from ₹2,99,999",
        seat_limit=1,
        live_trading=True,
        max_active_funds=None,
        max_books=5,
        max_lines_per_book=25,
        agent_runs_per_month=999,
        agent_ic_memo_per_month=999,
        holding_intel_per_day=999,
        manual_refresh_per_month=999,
        long_cycle_candidates=None,
        concurrent_agents=4,
        dashboard_reads_per_hour=5000,
        razorpay_plan_env="RAZORPAY_PLAN_ENTERPRISE",
    ),
}

DEFAULT_TIER = "sandbox"
SELF_SERVE_TIERS = ("sandbox", "desk", "fund", "enterprise")
UNPAID_MAX_BOOKS = 2


def get_tier(tier: Optional[str]) -> TierLimits:
    key = (tier or DEFAULT_TIER).strip().lower()
    return TIERS.get(key, TIERS[DEFAULT_TIER])


def plan_id_for_tier(tier: str) -> str:
    limits = get_tier(tier)
    return os.getenv(limits.razorpay_plan_env, "").strip() or os.getenv("RAZORPAY_PLAN_ID", "").strip()


def tier_for_plan_id(plan_id: str) -> Optional[str]:
    plan_id = (plan_id or "").strip()
    if not plan_id:
        return None
    for name, limits in TIERS.items():
        if plan_id_for_tier(name) == plan_id:
            return name
    legacy = os.getenv("RAZORPAY_PLAN_ID", "").strip()
    if legacy and legacy == plan_id:
        return "desk"
    return None


def plans_payload() -> list[dict]:
    rows = []
    for name in SELF_SERVE_TIERS:
        tier = TIERS[name]
        rows.append(
            {
                "tier": tier.tier,
                "label": tier.label,
                "price_inr": tier.price_inr,
                "price_display": tier.price_display,
                "live_trading": tier.live_trading,
                "max_active_funds": tier.max_active_funds,
                "max_books": None if name != "sandbox" else tier.max_books,
                "agent_runs_per_month": tier.agent_runs_per_month,
            }
        )
    return rows
