"""Tier quotas and subscription state for the workspace."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from src.config import Config, get_config
from src.journal.db import connect, session
from src.workspace.tiers import DEFAULT_TIER, UNPAID_MAX_BOOKS, TierLimits, get_tier
from src.workspace.usage import UsageCounter


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(float(value), timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


class QuotaExceeded(Exception):
    def __init__(
        self,
        quota: str,
        *,
        tier: str,
        limit: int,
        used: int,
        resets_at: Optional[str] = None,
    ):
        self.quota = quota
        self.tier = tier
        self.limit = limit
        self.used = used
        self.resets_at = resets_at
        super().__init__(f"Quota exceeded for {quota}")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": "quota_exceeded",
            "quota": self.quota,
            "tier": self.tier,
            "limit": self.limit,
            "used": self.used,
            "resets_at": self.resets_at,
        }


@dataclass
class SubscriptionRow:
    id: str
    owner_user_id: int
    tier: str
    seat_limit: int
    live_trading_enabled: bool
    billing_subscription_id: Optional[str]
    billing_subscription_status: Optional[str]
    billing_paid_through_at: Optional[str]
    trial_ends_at: Optional[str]
    razorpay_plan_id: Optional[str]

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "SubscriptionRow":
        return cls(
            id=str(row["id"]),
            owner_user_id=int(row["owner_user_id"]),
            tier=str(row.get("tier") or DEFAULT_TIER),
            seat_limit=int(row.get("seat_limit") or 1),
            live_trading_enabled=bool(int(row.get("live_trading_enabled") or 0)),
            billing_subscription_id=row.get("billing_subscription_id"),
            billing_subscription_status=row.get("billing_subscription_status"),
            billing_paid_through_at=row.get("billing_paid_through_at"),
            trial_ends_at=row.get("trial_ends_at"),
            razorpay_plan_id=row.get("razorpay_plan_id"),
        )


class EntitlementsService:
    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._usage = UsageCounter(self._config)

    def ensure_subscription(self, user_id: int, *, trial_days: int = 14) -> SubscriptionRow:
        conn = connect(self._config)
        try:
            row = conn.execute(
                "SELECT * FROM workspace_subscriptions WHERE owner_user_id = ?",
                (user_id,),
            ).fetchone()
            if row:
                return SubscriptionRow.from_row(dict(row))
            now = _now().isoformat()
            trial_ends = (_now() + timedelta(days=trial_days)).isoformat()
            sub_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO workspace_subscriptions (
                    id, owner_user_id, org_id, tier, seat_limit,
                    billing_subscription_id, billing_subscription_status,
                    billing_paid_through_at, trial_ends_at, live_trading_enabled,
                    razorpay_plan_id, created_at, updated_at
                ) VALUES (?, ?, NULL, ?, 1, NULL, NULL, NULL, ?, 0, NULL, ?, ?)
                """,
                (sub_id, user_id, DEFAULT_TIER, trial_ends, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM workspace_subscriptions WHERE id = ?",
                (sub_id,),
            ).fetchone()
            return SubscriptionRow.from_row(dict(row))
        finally:
            conn.close()

    def subscription_for_user(self, user_id: int) -> SubscriptionRow:
        return self.ensure_subscription(user_id)

    def limits(self, sub: SubscriptionRow) -> TierLimits:
        return get_tier(sub.tier)

    def watchlist_book_limit(self, sub: SubscriptionRow) -> Optional[int]:
        """Unpaid users (trial/expired) get a small cap; any paid subscription is unlimited."""
        if self.is_paid_active(sub):
            return None
        return UNPAID_MAX_BOOKS

    def active_fund_limit(self, sub: SubscriptionRow) -> Optional[int]:
        return self.limits(sub).max_active_funds

    def is_paid_active(self, sub: SubscriptionRow) -> bool:
        paid_through = _parse_ts(sub.billing_paid_through_at)
        status = (sub.billing_subscription_status or "").lower()
        if status in {"active", "cancelled"} and paid_through and paid_through > _now():
            return True
        return False

    def can_access(self, sub: SubscriptionRow) -> bool:
        if self.is_paid_active(sub):
            return True
        trial_ends = _parse_ts(sub.trial_ends_at)
        return bool(trial_ends and trial_ends > _now())

    def access_state(self, sub: SubscriptionRow) -> str:
        if self.is_paid_active(sub):
            return "active"
        trial_ends = _parse_ts(sub.trial_ends_at)
        if trial_ends and trial_ends > _now():
            return "trial"
        return "expired"

    def count_active_funds(self, user_id: int) -> int:
        conn = connect(self._config)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM user_book_personas WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()

    def count_active_books(self, user_id: int) -> int:
        conn = connect(self._config)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM user_books WHERE user_id = ? AND status = 'tracking'",
                (user_id,),
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()

    def count_book_lines(self, user_id: int, book_id: str) -> int:
        conn = connect(self._config)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM user_book_lines "
                "WHERE book_id = ? AND user_id = ? AND removed_at IS NULL",
                (book_id, user_id),
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()

    def count_inflight_agents(self, user_id: int) -> int:
        conn = connect(self._config)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM agent_runs "
                "WHERE requested_by_user_id = ? AND status IN ('queued', 'running')",
                (user_id,),
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()

    def usage_payload(self, sub: SubscriptionRow, user_id: int) -> Dict[str, Any]:
        limits = self.limits(sub)
        book_limit = self.watchlist_book_limit(sub)
        fund_limit = self.active_fund_limit(sub)
        return {
            "active_funds": {
                "used": self.count_active_funds(user_id),
                "limit": fund_limit,
            },
            "watchlist_books": {
                "used": self.count_active_books(user_id),
                "limit": book_limit,
            },
            "agent_runs": self._usage.snapshot(
                sub.id, "agent_runs", limits.agent_runs_per_month
            ),
            "agent_ic_memo": self._usage.snapshot(
                sub.id, "agent_ic_memo", limits.agent_ic_memo_per_month
            ),
            "holding_intel": self._usage.snapshot(
                sub.id, "holding_intel", limits.holding_intel_per_day
            ),
            "manual_refresh": self._usage.snapshot(
                sub.id, "manual_refresh", limits.manual_refresh_per_month
            ),
        }

    def limits_payload(self, sub: SubscriptionRow) -> Dict[str, Any]:
        limits = self.limits(sub)
        return {
            "tier": limits.tier,
            "seat_limit": sub.seat_limit,
            "live_trading": sub.live_trading_enabled and limits.live_trading,
            "max_active_funds": self.active_fund_limit(sub),
            "max_books": self.watchlist_book_limit(sub),
            "max_lines_per_book": limits.max_lines_per_book,
            "agent_runs_per_month": limits.agent_runs_per_month,
            "agent_ic_memo_per_month": limits.agent_ic_memo_per_month,
            "holding_intel_per_day": limits.holding_intel_per_day,
            "manual_refresh_per_month": limits.manual_refresh_per_month,
            "long_cycle_candidates": limits.long_cycle_candidates,
            "concurrent_agents": limits.concurrent_agents,
        }

    def subscription_payload(self, sub: SubscriptionRow) -> Dict[str, Any]:
        limits = self.limits(sub)
        return {
            "id": sub.id,
            "tier": sub.tier,
            "tier_label": limits.label,
            "seat_limit": sub.seat_limit,
            "live_trading_enabled": sub.live_trading_enabled and limits.live_trading,
            "paid_through_at": sub.billing_paid_through_at,
            "trial_ends_at": sub.trial_ends_at,
            "subscription_status": sub.billing_subscription_status,
        }

    def check_quota(
        self,
        user_id: int,
        quota: str,
        *,
        amount: int = 1,
        book_id: Optional[str] = None,
    ) -> None:
        sub = self.subscription_for_user(user_id)
        limits = self.limits(sub)
        tier = limits.tier

        if quota == "active_funds":
            used = self.count_active_funds(user_id)
            fund_limit = self.active_fund_limit(sub)
            if fund_limit is not None and used + amount > fund_limit:
                raise QuotaExceeded("active_funds", tier=tier, limit=fund_limit, used=used)
            return

        if quota == "watchlist_books":
            used = self.count_active_books(user_id)
            book_limit = self.watchlist_book_limit(sub)
            if book_limit is not None and used + amount > book_limit:
                raise QuotaExceeded("watchlist_books", tier=tier, limit=book_limit, used=used)
            return

        if quota == "watchlist_lines":
            if not book_id:
                raise ValueError("book_id required for watchlist_lines quota")
            used = self.count_book_lines(user_id, book_id)
            if used + amount > limits.max_lines_per_book:
                raise QuotaExceeded(
                    "watchlist_lines",
                    tier=tier,
                    limit=limits.max_lines_per_book,
                    used=used,
                )
            return

        if quota == "concurrent_agents":
            used = self.count_inflight_agents(user_id)
            if used + amount > limits.concurrent_agents:
                raise QuotaExceeded(
                    "concurrent_agents",
                    tier=tier,
                    limit=limits.concurrent_agents,
                    used=used,
                )
            return

        limit_map = {
            "agent_runs": limits.agent_runs_per_month,
            "agent_ic_memo": limits.agent_ic_memo_per_month,
            "holding_intel": limits.holding_intel_per_day,
            "manual_refresh": limits.manual_refresh_per_month,
            "dashboard_reads": limits.dashboard_reads_per_hour,
        }
        if quota not in limit_map:
            raise ValueError(f"Unknown quota: {quota}")
        limit = limit_map[quota]
        used = self._usage.get_used(sub.id, quota)
        if used + amount > limit:
            snap = self._usage.snapshot(sub.id, quota, limit)
            raise QuotaExceeded(
                quota,
                tier=tier,
                limit=limit,
                used=used,
                resets_at=snap.get("resets_at"),
            )

    def consume_quota(self, user_id: int, quota: str, *, amount: int = 1) -> None:
        self.check_quota(user_id, quota, amount=amount)
        sub = self.subscription_for_user(user_id)
        if quota in {"active_funds", "watchlist_books", "watchlist_lines", "concurrent_agents"}:
            return
        self._usage.consume(sub.id, quota, amount)

    def force_simulated_for_persona(self, persona_id: str) -> bool:
        from src.workspace.user_book_personas import UserBookPersonaRepository

        row = UserBookPersonaRepository(self._config).get_by_persona_id(persona_id)
        if row is None:
            return True
        user_id = int(row["user_id"])
        sub = self.subscription_for_user(user_id)
        if not self.can_access(sub):
            return True
        limits = self.limits(sub)
        if not limits.live_trading or not sub.live_trading_enabled:
            return True
        return False

    def long_cycle_candidates_for_persona(self, persona_id: str) -> Optional[int]:
        from src.workspace.user_book_personas import UserBookPersonaRepository

        row = UserBookPersonaRepository(self._config).get_by_persona_id(persona_id)
        if row is None:
            return 150
        sub = self.subscription_for_user(int(row["user_id"]))
        return self.limits(sub).long_cycle_candidates

    def update_tier(
        self,
        user_id: int,
        *,
        tier: str,
        live_trading_enabled: Optional[bool] = None,
        billing_subscription_id: Optional[str] = None,
        billing_subscription_status: Optional[str] = None,
        billing_paid_through_at: Optional[str] = None,
        razorpay_plan_id: Optional[str] = None,
    ) -> SubscriptionRow:
        sub = self.ensure_subscription(user_id)
        limits = get_tier(tier)
        live = limits.live_trading if live_trading_enabled is None else live_trading_enabled
        with session(self._config) as conn:
            conn.execute(
                """
                UPDATE workspace_subscriptions SET
                    tier = ?,
                    seat_limit = 1,
                    live_trading_enabled = ?,
                    billing_subscription_id = COALESCE(?, billing_subscription_id),
                    billing_subscription_status = COALESCE(?, billing_subscription_status),
                    billing_paid_through_at = COALESCE(?, billing_paid_through_at),
                    razorpay_plan_id = COALESCE(?, razorpay_plan_id),
                    updated_at = ?
                WHERE owner_user_id = ?
                """,
                (
                    limits.tier,
                    1 if live else 0,
                    billing_subscription_id,
                    billing_subscription_status,
                    billing_paid_through_at,
                    razorpay_plan_id,
                    _now().isoformat(),
                    user_id,
                ),
            )
        return self.subscription_for_user(user_id)

    def sync_billing_columns(self, user_id: int) -> None:
        """Keep legacy workspace_users billing columns aligned for older code paths."""
        sub = self.subscription_for_user(user_id)
        limits = self.limits(sub)
        with session(self._config) as conn:
            conn.execute(
                """
                UPDATE workspace_users SET
                    billing_subscription_id = ?,
                    billing_subscription_status = ?,
                    billing_paid_through_at = ?,
                    billing_trial_started_at = COALESCE(billing_trial_started_at, ?)
                WHERE id = ?
                """,
                (
                    sub.billing_subscription_id,
                    sub.billing_subscription_status,
                    sub.billing_paid_through_at,
                    sub.trial_ends_at,
                    user_id,
                ),
            )
