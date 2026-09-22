# Nivelo — AI-Managed Fund Engine: Pricing & Rate-Limit Model

**Product positioning:** Nivelo is an **AI-managed fund engine for PM desks** — not a research platform. A PM configures an initial portfolio from a watchlist; a strategy persona then runs the fund end-to-end: scans the universe, proposes trades, and **places real orders** through the connected broker (Angel One / Alpaca). The moat is **autonomous strategy execution on a PM-seeded mandate**, with full audit trail.

**Audience:** Indian PMS, AIF, family-office PM desks (not retail)  
**Currency:** INR, ex-GST  
**Date:** September 2026  
**Status:** Draft for internal review

---

## Executive summary

Nivelo turns a PM's watchlist into a **self-driving fund**. The PM sets the mandate (watchlist + strategy template + capital); the engine runs it daily — scanning the full market universe, generating proposals, and executing orders against the desk's broker account. Every decision is journaled with rationale, so the desk keeps an auditable record of *why* the AI traded.

The product is split across:

- **Frontend:** React on **Vercel** at **https://nivelo.in** — mandate setup, watchlists, persona oversight, `/work` agent desk, approvals, live positions.
- **Backend:** FastAPI + PostgreSQL + cron + Temporal on a **GCP VM** (Mumbai) — strategy engine, broker connectors, journal, agent runtime.

**Not in scope for customer pricing:** WhatsApp/Twilio EOD digests (internal ops only).

### The moat (why this is not a research tool)

| Research platform (what this is NOT) | Nivelo fund engine (what this IS) |
|---|---|
| Read-only reports | **Places real orders** via broker connectors |
| Static watchlist | Watchlist is the **seed mandate**; strategy rebalances it |
| Human decides each trade | Strategy persona **auto-executes** within risk rails |
| No audit of reasoning | Every proposal/order journaled with thesis + triggers |
| One-size model | Each desk runs **its own strategy persona** (its own fund) |

The code already supports live execution: with `DRY_RUN=false`, `build_broker` returns a real `AngelOneClient`/`AlpacaClient` and `execute_approved` places market orders. `execution.auto_approve` lets the fund self-approve within configured risk limits. Today it runs in `DRY_RUN=true` (paper) — the paid product flips that switch per desk.

### Tier at a glance

| Tier | Price (INR/mo, ex-GST) | Seats | Live funds | Agent runs/mo |
|------|------------------------|-------|-----------|---------------|
| **Sandbox** | ₹24,999 | 1 | 1 (paper only) | 5 |
| **Desk** | ₹74,999 | 3 | 1 (live) | 20 |
| **Fund** | ₹1,49,999 | 8 | 3 (live) | 80 |
| **Enterprise** | from ₹2,99,999 | Custom | Custom | Custom |

**Trial:** 14 days on Sandbox (paper only, no live orders, no agents beyond 5).

---

## Architecture (what we price)

```
┌─────────────────────────────────────────────────────────┐
│  Vercel — nivelo.in                                     │
│  • Mandate setup  • Watchlists  • Persona oversight     │
│  • Approvals  • Live positions  • /work agent desk      │
└───────────────────────────┬─────────────────────────────┘
                            │ HTTPS (CORS)
                            ▼
┌─────────────────────────────────────────────────────────┐
│  GCP VM — fund-poc (asia-south1)                        │
│  Caddy → FastAPI → PostgreSQL (journal)                 │
│  Cron: long_cycle (analyse → approve → execute), close  │
│  Broker connectors: Angel One / Alpaca (live orders)    │
│  Temporal + agent worker                                │
└───────────────────────────┬─────────────────────────────┘
                            │ Broker APIs
                            ▼
              ┌──────────────────────────┐
              │  Desk's broker account   │
              │  (Angel One / Alpaca)    │
              │  Real orders, real fills │
              └──────────────────────────┘
```

- **Shared platform work** (runs once for all customers): 7 house personas on weekday `long_cycle`, EOD snapshot publish, IPO dossier LLM, earnings analysis (seasonal).
- **Per-organisation work:** each desk's `userbook_*` fund persona — daily full-universe scan, proposal generation, **order execution**, NAV close, plus agent runs and on-demand holding intelligence.

---

## Cost stack (monthly, platform)

Approximate INR, ex-GST. FX ~₹84/USD.

### Fixed infra (shared)

| Component | Est. monthly | Notes |
|-----------|-------------|-------|
| GCP `e2-medium` (Mumbai) | ₹2,400 | 2 vCPU, 4 GB RAM |
| Disk + snapshots + GCS backup | ₹400 | 30 GB + nightly pg_dump |
| Vercel production | ₹0–1,700 | Hobby early; Pro ~$20 at scale |
| Domain nivelo.in | ₹100 | ~₹1,200/yr amortized |
| Cloud Logging / Ops Agent | ₹200 | Low volume |
| **Infra subtotal (POC / single VM)** | **₹3,100–4,800** | See at-scale networking below before quoting Enterprise |

### At-scale networking & DNS (not in POC subtotal)

Today the stack is **GCP Compute + Caddy on the VM + Vercel for nivelo.in** — not AWS. **Route 53 does not appear in this architecture**; frontend DNS/SSL is handled by Vercel, and API TLS is Caddy on the VM with registrar A/AAAA records (or a POC hostname). The ₹100/mo domain line is **registration only**, not managed DNS query volume or health checks.

These items become material when you need **HA API**, **cleaner DNS ops**, or **per-customer isolation** — not at first paid Desk.

| Component | Est. monthly (INR) | When it kicks in | Notes |
|-----------|-------------------|------------------|-------|
| GCP **HTTPS load balancer** + forwarding rule | ₹1,500–2,500 | Multi-VM or blue/green API; uptime SLA for Fund+ | ~$18/mo minimum rule + LB capacity; Caddy-on-VM stays ₹0 until then |
| **Google Cloud DNS** (managed zone + queries) | ₹200–800 | Leave registrar DNS; need API subdomain (`api.nivelo.in`), geo, or audit trail | Route 53 equivalent if you ever move DNS to AWS (~₹200–800 at B2B query volumes) |
| **Static / reserved external IP** | ₹500–700 | Stable API endpoint while rebuilding VM; LB front-end | Ephemeral IP is free with running VM |
| **Egress** (API + backups + snapshot restore) | ₹500–3,000 | ~10–25 active desks, heavier dashboard polling, GCS restore drills | DEPLOY_GCP assumes &lt;$1/mo at POC; budget ~₹100/100 GB from Mumbai |
| **Cloud Armor** (WAF) / IP allowlists | ₹0–4,000 | Enterprise security questionnaire; optional before then | Often deferred; Caddy + auth + rate limits first |
| **GCP API Gateway / Cloud Endpoints** | ₹0 (not planned) | Only if you split public edge from FastAPI | Current design: Caddy → FastAPI; no gateway fee today |
| **Dedicated VM per Enterprise desk** | ₹10,000–25,000 each | Isolated Postgres/journal or co-located deploy | `e2-standard-4`–`e2-standard-8` Mumbai; priced in Enterprise quote, not shared pool |
| **Temporal** (self-hosted on VM) | ₹0 incremental at POC | Already on fund VM / Docker | Temporal Cloud would be a separate line item if adopted |

**Stack reference (what we actually run):**

```
nivelo.in          → Vercel (DNS + SSL included on Pro)
api.nivelo.in      → registrar or Cloud DNS → GCP VM :443 (Caddy) → FastAPI :8000
fund-poc cron      → same VM (no separate gateway)
```

**Phase model (shared platform):**

| Phase | Trigger | Extra infra/mo (approx.) | Cumulative shared infra |
|-------|---------|--------------------------|-------------------------|
| **0 — POC** | 1 VM, &lt;5 desks | ₹0 (included above) | ₹3,100–4,800 |
| **1 — CPU** | ≥3 full-universe live funds | +₹7,600 (`e2-medium` → `e2-standard-4`) | ~₹11,000–13,000 |
| **2 — Edge** | ≥10 desks, API SLA, or VM rebuild pain | +₹2,200–6,000 (Cloud DNS + static IP + LB optional) | ~₹13,000–19,000 |
| **3 — Enterprise** | Isolated tenant / white-label | +₹10,000–25,000 **per dedicated VM** | Custom; not amortized into Desk/Fund list price |

### Variable (usage-driven)

| Component | Baseline/mo | At ~10 live desks | Driver |
|-----------|-------------|-------------------|--------|
| DeepSeek LLM (house personas) | ₹300–600 | ₹600–1,200 | 7 personas × ~22 weekdays × 2 calls/run (digest + decision) |
| IPO dossier LLM | ₹200–500 | ₹500 | Up to 6 issues/day on EOD publish |
| Earnings LLM | ₹0–800 | ₹800 | Results season only; shared cache |
| **Agent runs (`/work`)** | ₹50–200 | ₹500–1,000 | 1 LLM call per run (10k–18k token budget) + Temporal worker CPU |
| Screener/Yahoo fundamentals | ₹0 cash | CPU-bound | 500–2,672 symbol cache rebuilds |
| Razorpay MDR | ~2.4% of revenue | Pass-through | ~2% + 0.99% subscription + GST on fees |

### Agent run economics (`/work`)

Each agent run makes exactly **one** structured LLM call (`src/workspace/agent_runtime.py`), capped by a per-contract token budget of **10k–18k tokens** (`src/workspace/agent_platform.py` `AGENT_CATALOG`):

| Agent | Token budget | Timeout |
|-------|-------------|---------|
| Company Deep Dive | 18,000 | 900s |
| Comparative Screen | 16,000 | 900s |
| Event Impact | 12,000 | 600s |
| Portfolio Risk Review | 10,000 | 600s |
| IC Memo | 10,000 | 600s |

**Cost per run (DeepSeek off-peak):** worst case ~18k input + 2k output ≈ $0.005 ≈ **₹0.45/run**. Even the Fund tier's 80 runs/mo costs **~₹36/mo in tokens** — negligible.

**The real constraint is worker throughput, not tokens:** each run can occupy a Temporal worker activity for up to 10–15 minutes, and the worker allows only **2 concurrent model activities**. Agent quotas per tier exist to protect the worker queue, not the LLM bill. Source fetching today uses free sources (SEC EDGAR, RSS news); **EODHD is not integrated**, so agents carry no paid data cost.

### Key cost insight

**LLM tokens are cheap** (~₹500/mo for all 7 house personas on DeepSeek off-peak).

**The expensive marginal unit is a desk's live fund persona** running full-universe `long_cycle` over ~2,672 NSE symbols every weekday. A cold fundamentals pass can take **70+ minutes per persona per day**. Tiers must **gate and rate-limit** fund runs — not offer unlimited full scans on lower tiers.

### Ops cost (the part research tools don't carry)

Because Nivelo **places real orders**, the platform carries operational responsibility that a read-only tool does not:

| Ops item | Est. monthly | Why |
|----------|-------------|-----|
| Uptime monitoring + alerting | ₹500–1,000 | Ops Agent metrics/alerts already wired; a missed `long_cycle` or failed order must page someone |
| On-call / incident response | ₹8,000–15,000 | Live orders = real money at risk; someone must respond to broker API failures, rejected orders, stale prices before market open |
| Broker API maintenance | ₹2,000–4,000 | Angel One SmartAPI token refresh, session management, rate-limit handling; Alpaca key rotation |
| Reconciliation & audit | ₹3,000–6,000 | Daily journal vs broker fills; every live order must reconcile against broker confirmations |
| Backup/DR testing | ₹1,000–2,000 | pg_dump restore drills; a corrupted journal during a live fund is an existential event |
| **Ops subtotal** | **₹14,500–28,000** | Scales with number of live desks |

This ops layer is a **real cost of the moat** — it is why a desk pays ₹75k+/mo instead of ₹25k for a dashboard.

### Legal & compliance (the launch gate)

Because Nivelo **places real orders on customer broker accounts**, legal spend is not optional overhead — it is a pre-launch blocker and a recurring cost. Sandbox stays paper-only partly *because* live execution cannot be sold until the regulatory structure below is settled.

**One-time (pre-launch, before flipping `DRY_RUN=false` for any customer):**

| Item | Est. cost | Why |
|------|-----------|-----|
| **SEBI structuring opinion** | ₹2–5L | The existential question: when the engine auto-executes on a desk's broker account, is Nivelo an *investment adviser* (RIA registration), a *portfolio manager* (PMS registration, ₹5 Cr net-worth requirement), or a *software vendor*? The viable structure is almost certainly "desk trades under its own PMS/AIF licence; Nivelo is a tool" — but that needs a written law-firm opinion before launch |
| Customer agreements (MSA + order form) | ₹1–3L | Execution disclaimer, limitation of liability (cap at fees paid), "AI decides, desk's licence executes" framing, audit-trail access, termination |
| ToS + Privacy Policy (DPDP Act 2023) | ₹50k–1L | We store broker API credentials and trading data — consent flows, breach notification, data-principal rights |
| Trademark (Nivelo / nivelo.in) | ₹15–30k | Brand protection before paying customers |
| **One-time subtotal** | **~₹4–9L** | Pre-revenue cost the tier prices must recover |

**Recurring:**

| Item | Est. monthly | Why |
|------|-------------|-----|
| Legal counsel retainer | ₹25,000–50,000 | Per-deal contract tweaks (Enterprise), regulatory questions as SEBI's stance on AI-driven execution evolves |
| Professional indemnity / E&O insurance | ₹4,000–17,000 | ₹50k–2L/yr. Backstop if a bug places a wrong order and a desk loses money — the MSA liability cap is only as good as the insurance behind it |
| Company secretarial / ROC / bookkeeping | ₹3,000–5,000 | Standard B2B billing compliance |
| DPDP / security audit (at Enterprise scale) | amortized ₹8,000–17,000 | ₹1–2L/yr. Enterprise desks will demand a security audit or pen-test before connecting broker credentials |
| **Legal subtotal** | **₹40,000–89,000** | Heaviest in year one; counsel retainer can drop to ad-hoc once templates stabilise |

**The legal gate and the product gate are the same gate:** `DRY_RUN` per desk tier (implementation roadmap item 3) is also the regulatory control — no customer gets live orders before the SEBI opinion and MSA exist.

---

## LLM call inventory (from codebase)

| Call | Trigger | Frequency | Scope |
|------|---------|-----------|-------|
| News digest | `analyse` / `long_cycle` / `daily_monitor` | 1 per persona run | Per persona (+ per desk fund) |
| Decision memo | Same | 1 per persona run | Per persona |
| Earnings analysis | Results season | 1 per symbol with new period | Shared cache |
| IPO dossier | `publish_dashboard` | Up to 6 issues/day | Platform |
| Agent output | User starts `/work` run | 1 per agent run (10k–18k token budget) | Per user |
| Intraday LLM | Intraday watch | Disabled in config today | — |

**Primary model:** DeepSeek `deepseek-v4-flash` (~$0.22/M input, $0.66/M output off-peak).  
**Fallback:** Groq `openai/gpt-oss-120b` on JSON parse failure only.

**Agent catalog** (1 LLM call each): Company Deep Dive, Event Impact, Comparative Screen, Portfolio Risk Review, IC Memo.

---

## Per-user operations & cost drivers

| Operation | LLM | Compute | Notes |
|-----------|-----|---------|-------|
| Read EOD dashboard | 0 | Snapshot read | Shared platform data |
| Watchlist + symbol search | 0 | CSV in memory | Light |
| Watchlist MTM snapshot | 0 | Yahoo last prices | Post-close cron |
| **Activate fund persona** | 0 at click | Daily full pipeline + **live orders** | **Heavy** |
| Holding intelligence API | 0–1 | Fundamentals fetch | Per request |
| Agent run | 1 | Temporal worker + free sources (SEC EDGAR, RSS) | Per run |
| Manual long_cycle refresh | 2 | Full pipeline | Ad-hoc |
| **Live order execution** | 0 | Broker API calls | Per order; broker-side charges are the desk's |

**Code ceilings** (tiers sit below these): 5 watchlists, 25 lines/book, 5 active personas max.

---

## Subscription tiers (detail + pricing rationale)

Pricing anchor: an Indian PMS desk pays its **execution algo vendor** ₹50k–2L/mo and its **research terminal** ₹25k–1L/mo. Nivelo replaces the *decision + execution* loop, not just the screen. We price between an algo-execution tool and a junior quant hire.

### Tier 1 — Sandbox — ₹24,999 / month / organisation

**Buyer:** PM evaluating the engine on paper before connecting real capital.

**Includes:**
- nivelo.in workspace (Google OAuth)
- EOD snapshot: house fund personas, IPO feed, performance, sector weights
- Watchlists: **2 books**, **15 lines** each
- **1 paper fund persona** (strategy template, full universe, `DRY_RUN=true` — simulated orders only)
- Scheduled weekday run with **`max_candidates=150`** (~4–5 min vs 70+ min full scan)
- **5 agent runs / month**
- Holding intelligence: **10 lookups / day**

**Excludes:** Live order placement, manual refreshes.

**Why ₹24,999:** This is the **paid proof-of-concept** tier. The desk sees the strategy run their mandate on paper for a month before wiring real money. Covers infra share + light compute; priced to convert, not to profit. The moat is proven here — the desk watches the AI trade their watchlist before paying for live execution.

---

### Tier 2 — Desk — ₹74,999 / month / organisation (up to 3 seats)

**Buyer:** Small PMS desk or lead PM ready to run **one live fund**.

**Includes Sandbox, plus:**
- Watchlists: **5 books**, **25 lines**
- **1 live fund persona** (`DRY_RUN=false` — **real orders** via desk's Angel One/Alpaca account)
- Scheduled weekday run with **`max_candidates=150`** (capped scan keeps runtime predictable)
- **20 agent runs / month**
- Holding intelligence: **30 lookups / day**
- **2 manual fund refreshes / month** (ad-hoc long_cycle)
- **Auto-approve within risk rails** (execution.auto_approve with per-position/sector caps)
- Order audit trail + reconciliation report

**Why ₹74,999:** This is the first tier where **real money moves**. The price carries the ops layer (monitoring, reconciliation, broker maintenance) plus compute for a daily live fund run. A desk running ₹1–5 Cr through one AI fund pays less than a part-time dealer. Gross margin target **~55%** after ops + infra.

---

### Tier 3 — Fund — ₹1,49,999 / month / organisation (up to 8 seats)

**Buyer:** Established PMS / AIF running **multiple strategies** on live capital.

**Includes Desk, plus:**
- **3 live fund personas** (e.g. balanced + momentum + small-cap mandates)
- **80 agent runs / month**
- Holding intelligence: **100 lookups / day**
- **5 manual fund refreshes / month**
- **Full-universe** long_cycle (no candidate cap)
- **SEC EDGAR filings** source enabled for agents (US coverage; free)
- Priority agent queue + email support + onboarding + monthly strategy review call
- Multi-seat approval workflow (PM approves, engine executes)

**Why ₹1,49,999:** Three concurrent live funds require **VM upsize to e2-standard-4** (~₹10k/mo) and heavier ops (3× reconciliation, 3× broker sessions). A 3-strategy desk pays less than one quant analyst's salary while getting 24/7 autonomous execution with full audit.

---

### Tier 4 — Enterprise — from ₹2,99,999 / month

**Buyer:** Multi-desk AIF, family office, or white-label fund-as-a-service.

**Includes:** Custom seats, dedicated VM or isolated Postgres, custom strategy templates, SLA with market-hours response, custom agent quotas, US market personas, invoice/NEFT, DPA, optional co-located deployment. **Infra (LB, Cloud DNS, static IP, egress, optional WAF) is quoted explicitly** — not folded into shared Desk/Fund list prices.

**Why custom:** Dedicated infra (e2-standard-4/8), compliance, custom strategy development, and white-label overhead. Priced against a quant team, not a software seat. **Budget +₹15,000–40,000/mo** platform infra on top of shared costs before margin (see At-scale networking).

---

## Rate limits (per organisation)

| Operation | Sandbox | Desk | Fund | Enterprise |
|-----------|---------|------|------|------------|
| Seats | 1 | 3 | 8 | Custom |
| Watchlist books | 2 | 5 | 5 | Custom |
| Lines per book | 15 | 25 | 25 | 25 |
| Active fund personas | 1 (paper) | 1 (live) | 3 (live) | Unlimited |
| Live order placement | No | Yes | Yes | Yes |
| Scheduled long_cycle | 1, cap 150 candidates | 1, cap 150 candidates | 3, full universe | Custom |
| Manual long_cycle refresh | 0 | 2/mo | 5/mo | Custom |
| Agent runs | 5/mo | 20/mo | 80/mo | Custom |
| IC memo agent | 1/mo | 5/mo | 15/mo | Custom |
| Holding intelligence | 10/day | 30/day | 100/day | Custom |
| Dashboard API reads | 300/hr | 600/hr | 1200/hr | Custom |
| Concurrent agent runs | 1 | 1 | 2 | Custom |
| Auto-approve (live) | N/A | Yes (risk-railed) | Yes (risk-railed) | Custom |

**Over limit:** HTTP `429` with `{ code: "quota_exceeded", tier, limit, used }`.

**Live-order safety rails (all live tiers):** per-position cap, sector cap, max slippage %, daily turnover cap — already enforced in `execution` config; these are product features, not just limits.

---

## Unit economics example

**5 × Desk @ ₹74,999/mo (all live)**

| Line | Monthly (Phase 0) | Monthly (Phase 1–2, ~10 desks) |
|------|-------------------|--------------------------------|
| Revenue (ex-GST) | ₹3,74,995 | ₹7,49,990 (10× Desk) |
| Infra — VM + Vercel + domain | ₹5,000 | ₹16,000 (VM upsize + edge DNS/LB optional) |
| LLM — house personas + desk funds | ₹3,500 | ₹6,000 |
| Agent runs | ₹100 | ₹800 |
| Ops (monitoring, reconciliation, broker maintenance) | ₹20,000 | ₹25,000 |
| Legal & compliance (retainer + E&O + CS, amortized) | ₹35,000 | ₹45,000 |
| Razorpay (~2.4%) | ₹9,000 | ₹18,000 |
| **Gross margin** | **~₹3,02,000 (~81%)** | **~₹6,39,000 (~85%)** |

With VM upsize (`e2-standard-4`, +₹7.6k/mo) at 10 live desks: margin holds **~80–85%** even after Phase 2 edge (~₹4k/mo) — **Desk/Fund list prices unchanged**. Note the **one-time legal spend (~₹4–9L)** is recovered by roughly the first 1–2 Desk-tier months of margin.

Scaling trigger: move **`e2-medium` → `e2-standard-4`** when ≥3 concurrent full-universe live funds saturate CPU (~₹10k/mo incremental). Add **Phase 2 edge** (DNS + optional LB) when shared API uptime becomes a sales requirement, not when the first Desk signs.

---

## Subscription pricing vs infra scale

**Do list prices change when you add load balancers and DNS?** Mostly **no** for Sandbox / Desk / Fund through Phase 1–2; **yes** for Enterprise and optional add-ons.

| Tier | Change list price when…? | Rationale |
|------|--------------------------|-----------|
| **Sandbox** ₹24,999 | **No** through Phase 2 | Paper-only; shared POC infra; conversion tier |
| **Desk** ₹74,999 | **No** until ~15–20 live desks on shared VM **or** Phase 2 edge &gt;₹6k/mo | Extra ~₹6–10k/mo infra is &lt;2% of revenue at 5× Desk; absorb in margin |
| **Fund** ₹1,49,999 | **No** through Phase 2; optional **+₹10,000–15,000/mo** if you productise **HA API** (LB + Armor + priority queue) as **Fund Plus** | Full-universe compute already priced in; edge is incremental |
| **Enterprise** from ₹2,99,999 | **Yes — always custom** | Dedicated VM, DNS, LB, WAF, and egress are **line-itemed** in the SOW; floor should assume **+₹15,000–40,000/mo** infra over shared platform before margin |

**Absorption rule of thumb:** keep published Desk/Fund prices flat while **shared infra &lt;~5% of MRR**. At ~15 live Desk customers (~₹11.25L MRR), Phase 1+2 infra (~₹15–19k) is ~1.3–1.7% — **no tier change needed**. Revisit list prices or introduce **Fund Plus** only if you add HA edge **and** shared infra crosses **~₹25k/mo** without Enterprise isolation.

**What customers should *not* pay separately (bundled):** registrar domain, Vercel Pro, POC Cloud Logging, Caddy TLS on VM.

**What Enterprise *does* pay for (in custom quote):** dedicated compute, Cloud DNS zones per tenant, load balancer + static IP, extra egress, optional Cloud Armor, co-located deployment, EODHD or other paid data feeds if enabled.

**Route 53:** only relevant if the org moves DNS to AWS; treat as interchangeable with Cloud DNS in quotes (~same order of magnitude). It is **not** a line item in the current GCP+Vercel stack.

---

## vs current billing

| Today | Proposed |
|-------|----------|
| Single plan ₹999/mo (env placeholder) | Four B2B tiers ₹24,999–₹2,99,999+ |
| 60-day trial | 14-day Sandbox trial (paper only) |
| Binary access (trial/active/expired) | Tier + per-operation quotas + live/paper gate |
| Razorpay single `RAZORPAY_PLAN_ID` | One plan ID per tier |
| All personas `DRY_RUN=true` | Live execution gated to Desk+ tiers |

---

## Open decisions

1. **Live-order liability:** Who is the investment adviser of record? (Desk's PMS licence vs Nivelo as sub-adviser — see Legal & compliance; the SEBI structuring opinion must land before flipping `DRY_RUN=false` for customers.)
2. **RIA registration path:** Does Nivelo ever pursue its own SEBI RIA registration? It would unlock selling to desks *without* their own licence (bigger market) at the cost of a much larger regulatory surface.
3. **Desk candidate cap:** Keep `max_candidates=150` on Desk (recommended) vs full universe at higher price.
4. **Agent data sources:** EODHD is **not integrated** — agents currently run on free sources (SEC EDGAR, RSS news) with degraded coverage. Decide later which paid source to add and whether it becomes a Fund+ add-on.
5. **Annual prepay:** 10× monthly (2 months free) on Desk/Fund.
6. **AUM-linked pricing:** Consider a bps-on-AUM overlay for Enterprise (e.g. 5–10 bps on capital under management) once live track record exists.
7. **Fund Plus / HA API:** Optional tier or add-on (+₹10–15k/mo) if Phase 2 LB + Armor becomes a standard Fund deliverable rather than Enterprise-only.
8. **DNS ownership:** Stay on registrar + Vercel vs migrate `api.nivelo.in` to Cloud DNS before first Enterprise security review.

---

## Implementation roadmap (engineering follow-up)

1. **Schema:** `workspace_subscriptions` tier + quota columns + `live_trading_enabled` flag (`src/journal/db.py`)
2. **Entitlements:** `src/workspace/entitlements.py` — check quotas on persona activation, agents, intelligence API, **and gate `DRY_RUN` per desk tier**
3. **Live-order gate:** Wire tier → `force_simulated` in `build_broker` so Sandbox can never place real orders
4. **Billing:** Multi-plan Razorpay in `src/workspace/billing.py`
5. **Observability:** `quota_denied` + `live_order_placed` OPS events; reconciliation dashboard
6. **Frontend:** Mandate setup flow, tier/usage meters, approval queue, upgrade CTA on Vercel app (investment-research-web)

---

*Internal doc — grounded in congenial-octo repo cost drivers. Live order placement requires regulatory review before customer launch.*
