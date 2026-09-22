# HLD: Entitlements, Rate Limits, and Razorpay Tiers

See implementation in:

- Backend: `src/workspace/tiers.py`, `entitlements.py`, `usage.py`, `billing.py`
- Schema: `workspace_subscriptions`, `workspace_usage_counters` in `src/journal/db.py`
- Frontend: `investment-research-web` — `AccountContext`, `Subscribe`, `Profile`, watchlist pages

## v1 seat model

One Google login = one subscription = **1 seat**. Multi-seat org model is stubbed (`workspace_orgs`, `workspace_org_members`) for Phase 2.

## Tiers

| Tier | Price | Live | Funds | Books | Agent runs/mo |
|------|-------|------|-------|-------|---------------|
| sandbox | ₹24,999 | paper | 1 | 2 | 5 |
| desk | ₹74,999 | yes | 1 | 5 | 20 |
| fund | ₹1,49,999 | yes | 3 | 5 | 80 |
| enterprise | from ₹2,99,999 | yes | unlimited | 5 | custom |

## Razorpay env

```
RAZORPAY_PLAN_SANDBOX=plan_xxx
RAZORPAY_PLAN_DESK=plan_yyy
RAZORPAY_PLAN_FUND=plan_zzz
RAZORPAY_PLAN_ENTERPRISE=plan_aaa
```

Legacy `RAZORPAY_PLAN_ID` maps to Desk.

## Local testing

Backend:

```bash
cd congenial-octo
.venv/bin/python -m pytest tests/test_entitlements.py tests/test_workspace.py -q
```

Frontend:

```bash
cd investment-research-web
npm run test
npm run dev   # localhost:5173 → backend at config apiBaseUrl
```

Billing is off by default (`WORKSPACE_BILLING_ENABLED=false`); all users get Sandbox limits with full access.
