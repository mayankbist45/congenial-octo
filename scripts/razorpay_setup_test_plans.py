#!/usr/bin/env python3
"""Create Razorpay test subscription plans for all workspace tiers.

Reads RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET from .env (or the environment),
creates missing monthly plans, and prints env lines to paste into .env.

Usage (from repo root):
    .venv/bin/python scripts/razorpay_setup_test_plans.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.workspace.tiers import SELF_SERVE_TIERS, TIERS  # noqa: E402


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _auth_header() -> str:
    key_id = os.getenv("RAZORPAY_KEY_ID", "").strip()
    key_secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
    if not key_id or not key_secret:
        raise SystemExit("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env first.")
    token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    return f"Basic {token}"


def _razorpay(method: str, path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"https://api.razorpay.com/v1{path}",
        data=body,
        method=method,
        headers={
            "Authorization": _auth_header(),
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except HTTPError as exc:
        detail = exc.read().decode()
        raise SystemExit(f"Razorpay API error ({exc.code}): {detail}") from exc


def _list_plans() -> list[dict]:
    payload = _razorpay("GET", "/plans?count=100")
    return list(payload.get("items") or [])


def _find_plan(plans: list[dict], *, name: str, amount_paise: int) -> str | None:
    for plan in plans:
        item = plan.get("item") or {}
        if item.get("name") == name and int(item.get("amount") or 0) == amount_paise:
            return str(plan.get("id"))
    return None


def _create_plan(*, name: str, amount_paise: int, description: str) -> str:
    payload = _razorpay(
        "POST",
        "/plans",
        {
            "period": "monthly",
            "interval": 1,
            "item": {
                "name": name,
                "amount": amount_paise,
                "currency": "INR",
                "description": description,
            },
        },
    )
    plan_id = payload.get("id")
    if not plan_id:
        raise SystemExit(f"Razorpay did not return a plan id for {name}.")
    return str(plan_id)


def main() -> None:
    _load_dotenv()
    if not os.getenv("RAZORPAY_KEY_ID", "").startswith("rzp_test_"):
        print("Warning: key does not look like a Razorpay test key (expected rzp_test_...).")

    existing = _list_plans()
    env_lines: list[str] = []

    for tier_name in SELF_SERVE_TIERS:
        tier = TIERS[tier_name]
        plan_name = f"Nivelo {tier.label}"
        amount_paise = tier.price_inr * 100
        env_key = tier.razorpay_plan_env
        plan_id = _find_plan(existing, name=plan_name, amount_paise=amount_paise)
        if not plan_id:
            plan_id = _create_plan(
                name=plan_name,
                amount_paise=amount_paise,
                description=f"Monthly {tier.label} workspace — {tier.price_display}/mo",
            )
            print(f"Created {tier_name}: {plan_id}")
        else:
            print(f"Found {tier_name}: {plan_id}")
        env_lines.append(f"{env_key}={plan_id}")

    print("\nAdd or update these lines in .env:\n")
    for line in env_lines:
        print(line)


if __name__ == "__main__":
    main()
