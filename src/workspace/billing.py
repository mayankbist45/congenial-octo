"""Small Razorpay-backed entitlement boundary for the read-only workspace."""

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
        self.requested = os.getenv("WORKSPACE_BILLING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        try:
            self.trial_days = max(0, int(os.getenv("WORKSPACE_TRIAL_DAYS", "60")))
        except ValueError:
            self.trial_days = 60
        self.launch_at = _timestamp(os.getenv("WORKSPACE_BILLING_LAUNCH_AT"))
        self.plan_id = os.getenv("RAZORPAY_PLAN_ID", "").strip()
        self.key_id = os.getenv("RAZORPAY_KEY_ID", "").strip()
        self.key_secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
        self.webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
        self.plan_label = os.getenv("WORKSPACE_PLAN_LABEL", "Nivelo Research").strip()
        self.plan_price = os.getenv("WORKSPACE_PLAN_PRICE", "Monthly").strip()
        self.enabled = self.requested and all((self.plan_id, self.key_id, self.key_secret, self.webhook_secret, self.launch_at))

    def _require_configured(self) -> None:
        if not self.requested:
            raise HTTPException(status_code=404, detail="Subscriptions are not enabled.")
        if not self.enabled:
            raise HTTPException(status_code=503, detail="Subscriptions are not fully configured.")

    def entitlement(self, user: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled:
            return {"can_access": True, "state": "active", "subscription_status": "active", "trial_ends_at": None, "paid_through_at": None, "plan_label": self.plan_label, "plan_price": self.plan_price}
        created_at = _timestamp(user.get("created_at")) or _now()
        started_at = _timestamp(user.get("billing_trial_started_at")) or max(created_at, self.launch_at or created_at)
        if not user.get("billing_trial_started_at"):
            with session(self._config) as conn:
                conn.execute("UPDATE workspace_users SET billing_trial_started_at = ? WHERE id = ?", (started_at.isoformat(), user["id"]))
            user["billing_trial_started_at"] = started_at.isoformat()
        paid_through = _timestamp(user.get("billing_paid_through_at"))
        status = str(user.get("billing_subscription_status") or "").lower()
        if status in {"active", "cancelled"} and paid_through and paid_through > _now():
            return {"can_access": True, "state": "active", "subscription_status": status, "trial_ends_at": (started_at + timedelta(days=self.trial_days)).isoformat(), "paid_through_at": paid_through.isoformat(), "plan_label": self.plan_label, "plan_price": self.plan_price}
        trial_ends_at = started_at + timedelta(days=self.trial_days)
        return {"can_access": trial_ends_at > _now(), "state": "trial" if trial_ends_at > _now() else "expired", "subscription_status": status or None, "trial_ends_at": trial_ends_at.isoformat(), "paid_through_at": paid_through.isoformat() if paid_through else None, "plan_label": self.plan_label, "plan_price": self.plan_price}

    def require_access(self, user: Dict[str, Any]) -> Dict[str, Any]:
        entitlement = self.entitlement(user)
        if not entitlement["can_access"]:
            raise HTTPException(status_code=403, detail={"code": "subscription_required", "entitlement": entitlement})
        return user

    def checkout(self, user: Dict[str, Any]) -> Dict[str, Any]:
        self._require_configured()
        if user.get("billing_subscription_status") == "active" and self.entitlement(user)["can_access"]:
            raise HTTPException(status_code=409, detail="Your subscription is already active.")
        subscription_id = user.get("billing_subscription_id") if user.get("billing_subscription_status") in {"created", "authenticated", "pending"} else None
        if not subscription_id:
            payload = self._razorpay("POST", "/subscriptions", {"plan_id": self.plan_id, "total_count": 1200, "notes": {"workspace_user_id": str(user["id"]), "email": user["email"]}})
            subscription_id = payload.get("id")
        if not subscription_id:
            raise HTTPException(status_code=502, detail="Razorpay did not return a subscription ID.")
        with session(self._config) as conn:
            conn.execute("UPDATE workspace_users SET billing_subscription_id = ?, billing_subscription_status = COALESCE(billing_subscription_status, 'created'), billing_checkout_url = NULL WHERE id = ?", (subscription_id, user["id"]))
        return {"subscription_id": subscription_id, "key_id": self.key_id, "name": self.plan_label, "description": "Monthly research workspace access", "email": user["email"]}

    def verify_payment(self, user: Dict[str, Any], payment_id: str, subscription_id: str, signature: str) -> Dict[str, bool]:
        self._require_configured()
        if not all((payment_id, subscription_id, signature)) or subscription_id != user.get("billing_subscription_id"):
            raise HTTPException(status_code=400, detail="Invalid subscription payment response.")
        expected = hmac.new(self.key_secret.encode(), f"{payment_id}|{subscription_id}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=400, detail="Invalid Razorpay payment signature.")
        return {"verified": True}

    def cancel(self, user: Dict[str, Any]) -> Dict[str, Any]:
        self._require_configured()
        subscription_id = user.get("billing_subscription_id")
        if not subscription_id:
            raise HTTPException(status_code=404, detail="No subscription was found for this account.")
        payload = self._razorpay("POST", f"/subscriptions/{subscription_id}/cancel", {"cancel_at_cycle_end": 1})
        with session(self._config) as conn:
            conn.execute("UPDATE workspace_users SET billing_subscription_status = ? WHERE id = ?", (payload.get("status", "cancelled"), user["id"]))
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
        paid_through = _timestamp(subscription.get("current_end")) or _timestamp(subscription.get("end_at"))
        with session(self._config) as conn:
            if conn.execute("SELECT 1 FROM workspace_billing_events WHERE id = ?", (event_id,)).fetchone():
                return
            conn.execute("INSERT INTO workspace_billing_events (id, subscription_id, received_at) VALUES (?, ?, ?)", (event_id, subscription_id, _now().isoformat()))
            conn.execute("UPDATE workspace_users SET billing_subscription_status = ?, billing_paid_through_at = ?, billing_checkout_url = NULL WHERE billing_subscription_id = ?", (subscription.get("status"), paid_through.isoformat() if paid_through else None, subscription_id))

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
