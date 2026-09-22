"""Razorpay-backed subscriptions with tier entitlements."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import HTTPException

from src.journal.db import session
from src.workspace.entitlements import EntitlementsService
from src.workspace.tiers import DEFAULT_TIER, SELF_SERVE_TIERS, get_tier, plan_id_for_tier, plans_payload, tier_for_plan_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(float(value), timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


class WorkspaceBilling:
    def __init__(self, config: Any):
        self._config = config
        self._entitlements = EntitlementsService(config)
        self.requested = os.getenv("WORKSPACE_BILLING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        try:
            self.trial_days = max(0, int(os.getenv("WORKSPACE_TRIAL_DAYS", "14")))
        except ValueError:
            self.trial_days = 14
        self.launch_at = _timestamp(os.getenv("WORKSPACE_BILLING_LAUNCH_AT"))
        self.dev_simulate = os.getenv("WORKSPACE_BILLING_DEV_SIMULATE", "false").strip().lower() in {"1", "true", "yes", "on"}
        self.key_id = os.getenv("RAZORPAY_KEY_ID", "").strip()
        self.key_secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
        self.webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
        legacy_plan = os.getenv("RAZORPAY_PLAN_ID", "").strip()
        razorpay_ready = all((self.key_id, self.key_secret, self.webhook_secret, self.launch_at)) and any(
            plan_id_for_tier(t) or legacy_plan for t in SELF_SERVE_TIERS
        )
        self.enabled = self.requested and (self.dev_simulate or razorpay_ready)

    def _require_configured(self) -> None:
        if not self.requested:
            raise HTTPException(status_code=404, detail="Subscriptions are not enabled.")
        if not self.enabled:
            raise HTTPException(status_code=503, detail="Subscriptions are not fully configured.")

    def _resolve_tier(self, tier: Optional[str]) -> str:
        cleaned = (tier or DEFAULT_TIER).strip().lower()
        if cleaned not in SELF_SERVE_TIERS:
            raise HTTPException(status_code=422, detail=f"Unsupported tier: {tier}")
        plan_id = plan_id_for_tier(cleaned)
        if not plan_id and not self.dev_simulate:
            raise HTTPException(status_code=503, detail=f"Razorpay plan not configured for tier {cleaned}.")
        return cleaned

    def account_payload(self, user: Dict[str, Any]) -> Dict[str, Any]:
        sub = self._entitlements.ensure_subscription(int(user["id"]), trial_days=self.trial_days)
        limits = self._entitlements.limits(sub)
        can_access = True if not self.enabled else self._entitlements.can_access(sub)
        state = "active" if not self.enabled else self._entitlements.access_state(sub)
        return {
            "subscription": self._entitlements.subscription_payload(sub),
            "limits": self._entitlements.limits_payload(sub),
            "usage": self._entitlements.usage_payload(sub, int(user["id"])),
            "entitlement": {
                "can_access": can_access,
                "state": state,
                "subscription_status": sub.billing_subscription_status,
                "trial_ends_at": sub.trial_ends_at,
                "paid_through_at": sub.billing_paid_through_at,
                "plan_label": limits.label,
                "plan_price": limits.price_display,
                "tier": sub.tier,
            },
        }

    def entitlement(self, user: Dict[str, Any]) -> Dict[str, Any]:
        return self.account_payload(user)["entitlement"]

    def require_access(self, user: Dict[str, Any]) -> Dict[str, Any]:
        payload = self.account_payload(user)
        if not payload["entitlement"]["can_access"]:
            raise HTTPException(
                status_code=403,
                detail={"code": "subscription_required", "entitlement": payload["entitlement"]},
            )
        return user

    def list_plans(self, user: Dict[str, Any]) -> Dict[str, Any]:
        sub = self._entitlements.subscription_for_user(int(user["id"]))
        return {
            "current_tier": sub.tier,
            "plans": plans_payload(),
        }

    def checkout(
        self,
        user: Dict[str, Any],
        *,
        tier: Optional[str] = None,
        intent: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._require_configured()
        selected = self._resolve_tier(tier or DEFAULT_TIER)
        limits = get_tier(selected)
        sub = self._entitlements.subscription_for_user(int(user["id"]))
        if self._entitlements.is_paid_active(sub) and self._entitlements.can_access(sub):
            if intent in {"watchlist_upgrade", "persona_upgrade"}:
                raise HTTPException(
                    status_code=409,
                    detail="Your subscription is already active. Choose a higher tier from Profile.",
                )
            if sub.tier == selected:
                raise HTTPException(status_code=409, detail="Your subscription is already active.")
        plan_id = plan_id_for_tier(selected) or (f"plan_dev_{selected}" if self.dev_simulate else None)
        subscription_id = (
            user.get("billing_subscription_id")
            if user.get("billing_subscription_status") in {"created", "authenticated", "pending"}
            else None
        )
        if not subscription_id:
            if self.dev_simulate:
                subscription_id = f"sub_dev_{user['id']}_{selected}"
            else:
                payload = self._razorpay(
                    "POST",
                    "/subscriptions",
                    {
                        "plan_id": plan_id,
                        "total_count": 1200,
                        "notes": {
                            "workspace_user_id": str(user["id"]),
                            "email": user["email"],
                            "tier": selected,
                        },
                    },
                )
                subscription_id = payload.get("id")
        if not subscription_id:
            raise HTTPException(status_code=502, detail="Razorpay did not return a subscription ID.")
        self._entitlements.update_tier(
            int(user["id"]),
            tier=selected,
            billing_subscription_id=subscription_id,
            billing_subscription_status="created",
            razorpay_plan_id=plan_id,
        )
        self._entitlements.sync_billing_columns(int(user["id"]))
        with session(self._config) as conn:
            conn.execute(
                "UPDATE workspace_users SET billing_subscription_id = ?, billing_subscription_status = COALESCE(billing_subscription_status, 'created'), billing_checkout_url = NULL WHERE id = ?",
                (subscription_id, user["id"]),
            )
        mode = "paper fund" if not limits.live_trading else "live fund"
        response = {
            "subscription_id": subscription_id,
            "key_id": "rzp_test_dev" if self.dev_simulate else self.key_id,
            "name": f"Nivelo {limits.label}",
            "description": f"Monthly {mode} workspace — {limits.price_display}/mo",
            "email": user["email"],
            "tier": selected,
        }
        if self.dev_simulate:
            response["dev_simulate"] = True
        return response

    def verify_payment(self, user: Dict[str, Any], payment_id: str, subscription_id: str, signature: str) -> Dict[str, Any]:
        self._require_configured()
        if not all((payment_id, subscription_id, signature)) or subscription_id != user.get("billing_subscription_id"):
            raise HTTPException(status_code=400, detail="Invalid subscription payment response.")
        if self.dev_simulate and subscription_id.startswith("sub_dev_"):
            if signature != "dev_simulate_ok":
                raise HTTPException(status_code=400, detail="Invalid dev subscription payment response.")
            tier = self._entitlements.subscription_for_user(int(user["id"])).tier
            limits = get_tier(tier)
            paid_through = _now() + timedelta(days=31)
            self._entitlements.update_tier(
                int(user["id"]),
                tier=tier,
                live_trading_enabled=limits.live_trading,
                billing_subscription_id=subscription_id,
                billing_subscription_status="active",
                billing_paid_through_at=paid_through.isoformat(),
            )
            self._entitlements.sync_billing_columns(int(user["id"]))
            payload = self.account_payload(user)
            return {"verified": True, **payload}
        expected = hmac.new(
            self.key_secret.encode(),
            f"{payment_id}|{subscription_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=400, detail="Invalid Razorpay payment signature.")
        subscription = self._razorpay("GET", f"/subscriptions/{subscription_id}")
        self._apply_razorpay_subscription(int(user["id"]), subscription)
        payload = self.account_payload(user)
        return {"verified": True, **payload}

    def cancel(self, user: Dict[str, Any]) -> Dict[str, Any]:
        self._require_configured()
        subscription_id = user.get("billing_subscription_id")
        if not subscription_id:
            raise HTTPException(status_code=404, detail="No subscription was found for this account.")
        payload = self._razorpay("POST", f"/subscriptions/{subscription_id}/cancel", {"cancel_at_cycle_end": 1})
        with session(self._config) as conn:
            conn.execute(
                "UPDATE workspace_users SET billing_subscription_status = ? WHERE id = ?",
                (payload.get("status", "cancelled"), user["id"]),
            )
        self._entitlements.update_tier(
            int(user["id"]),
            tier=self._entitlements.subscription_for_user(int(user["id"])).tier,
            billing_subscription_status=payload.get("status", "cancelled"),
        )
        self._entitlements.sync_billing_columns(int(user["id"]))
        return {"status": payload.get("status", "cancelled")}

    def handle_webhook(self, body: bytes, signature: str) -> None:
        self._require_configured()
        expected = hmac.new(self.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature or ""):
            raise HTTPException(status_code=400, detail="Invalid Razorpay webhook signature.")
        try:
            event = json.loads(body)
            subscription = event["payload"]["subscription"]["entity"]
            subscription_id = str(subscription["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid Razorpay subscription webhook.") from exc
        event_id = hashlib.sha256(body).hexdigest()
        with session(self._config) as conn:
            if conn.execute("SELECT 1 FROM workspace_billing_events WHERE id = ?", (event_id,)).fetchone():
                return
            conn.execute(
                "INSERT INTO workspace_billing_events (id, subscription_id, received_at) VALUES (?, ?, ?)",
                (event_id, subscription_id, _now().isoformat()),
            )
            user_row = conn.execute(
                "SELECT id FROM workspace_users WHERE billing_subscription_id = ?",
                (subscription_id,),
            ).fetchone()
            if user_row is None:
                user_row = conn.execute(
                    "SELECT owner_user_id AS id FROM workspace_subscriptions WHERE billing_subscription_id = ?",
                    (subscription_id,),
                ).fetchone()
        if user_row:
            self._apply_razorpay_subscription(int(user_row["id"]), subscription)

    def _apply_razorpay_subscription(self, user_id: int, subscription: Dict[str, Any]) -> None:
        subscription_id = str(subscription["id"])
        paid_through = _timestamp(subscription.get("current_end")) or _timestamp(subscription.get("end_at"))
        plan_id = str(subscription.get("plan_id") or "")
        tier = tier_for_plan_id(plan_id) or DEFAULT_TIER
        limits = get_tier(tier)
        with session(self._config) as conn:
            conn.execute(
                "UPDATE workspace_users SET billing_subscription_status = ?, billing_paid_through_at = ?, billing_checkout_url = NULL WHERE id = ? OR billing_subscription_id = ?",
                (
                    subscription.get("status"),
                    paid_through.isoformat() if paid_through else None,
                    user_id,
                    subscription_id,
                ),
            )
        status = str(subscription.get("status") or "").lower()
        live = limits.live_trading and status in {"active", "authenticated", "cancelled"}
        self._entitlements.update_tier(
            user_id,
            tier=tier,
            live_trading_enabled=live,
            billing_subscription_id=subscription_id,
            billing_subscription_status=subscription.get("status"),
            billing_paid_through_at=paid_through.isoformat() if paid_through else None,
            razorpay_plan_id=plan_id or None,
        )
        self._entitlements.sync_billing_columns(user_id)

    def _razorpay(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(f"https://api.razorpay.com/v1{path}", data=body, method=method)
        token = base64.b64encode(f"{self.key_id}:{self.key_secret}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
        request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=15) as response:
                return json.loads(response.read())
        except (HTTPError, URLError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="Razorpay is unavailable. Please try again.") from exc
