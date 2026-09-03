# Phase 7+: Rollout And Remaining Work

Status as of 2026-09-02: the agent platform is consolidated, deployed, and
pilot-verified end to end on the production VM with a DeepSeek worker.

## Branch Consolidation

The agent platform lived on two feature branches that had diverged from `main`
(both were missing the watchlist/user-persona work landed on `main`):

- Backend `feature/agents` ← `main` (merge commit): conflicts were additive in
  `src/dashboard/app.py` (both route sets kept) and `src/journal/db.py` (both
  migration sets kept). Full suite: 555 passed, 2 skipped.
- Frontend `feature/agents-pms` ← `main` (merge commit): seven additive
  conflicts resolved (`App.tsx`, `Layout.tsx`, `ThemeContext.tsx`, `index.css`,
  `mocks/handlers.js`, `routes.ts`, `vite.config.js`). Vitest: 59 passed;
  `npm run build` clean.

The frontend `App.tsx` originally wrapped `ResearchDataProvider` around
`RouterProvider`, which cannot work (it calls `useLocation`); the merge moved
`ResearchDataProvider` into `Layout.tsx` (inside the router) and kept
`QueryClientProvider` at the root.

## Local Pilot

Run the stack locally to iterate before prod:

```bash
# Temporal server + UI
docker compose --env-file deploy/temporal.env.local -f deploy/temporal-compose.yml up -d temporal temporal-ui temporal-postgresql

# Worker (Python 3.10+ required; the project venv is 3.9, so use Docker)
#   deploy/agent-worker.env.local: DATABASE_URL=...@host.docker.internal:5432/mutualfund
#   WORKSPACE_AGENT_MODEL=deepseek-v4-flash, OPENAI_BASE_URL=https://api.deepseek.com, OPENAI_API_KEY=...
#   Local Postgres must trust the Docker gateway (host.docker.internal) in pg_hba.
docker compose --env-file deploy/temporal.env.local -f deploy/temporal-compose.yml -f deploy/temporal-compose.local.yml --profile agent up -d --build agent-worker

# Control plane needs TEMPORAL_ADDRESS=127.0.0.1:7233 and temporalio installed (3.10+).
```

Result: a `company_deep_dive` case ran queued → running → prepare →
collect_sources → generate_output (DeepSeek) → persist → completed, with
`temporal_workflow_id`, `trace_id`, event history, and a persisted artifact.

## Production Enablement

The FastAPI control plane runs on Python 3.11 with `temporalio` installed. The
worker runs in Docker with **host networking** so it reaches both Temporal
(`127.0.0.1:7233`) and the loopback-only fund PostgreSQL (`127.0.0.1:5432`)
without exposing Postgres on the Docker bridge:

```bash
# /etc/nivelo/agent-worker.env
#   DATABASE_URL=postgresql://fund:...@127.0.0.1:5432/mutualfund
#   TEMPORAL_ADDRESS=127.0.0.1:7233
#   WORKSPACE_AGENT_MODEL=deepseek-v4-flash
#   OPENAI_BASE_URL=https://api.deepseek.com
#   OPENAI_API_KEY=<DeepSeek key>
# /opt/fund/.env
#   TEMPORAL_ADDRESS=127.0.0.1:7233

sudo systemctl restart mutualfund-dashboard
cd /opt/fund
sudo ./scripts/temporal_compose.sh --env-file /etc/nivelo/temporal.env \
  -f deploy/temporal-compose.yml -f deploy/temporal-compose.vm.yml \
  --profile agent up -d --build agent-worker
```

`deploy/temporal-compose.vm.yml` is the host-network override
(`network_mode: host`, `extra_hosts: []`, `TEMPORAL_ADDRESS: 127.0.0.1:7233`).

## Production Pilot Result

On 2026-09-04 agents moved to **real live tool calls**: each run gathers its
own sources (bounded gather → synthesize loop) and the artifact's coverage is
normalized to the sources actually reached. A `company_deep_dive` on NVDA (US)
now returns `coverage_status: partial` with a real `SEC EDGAR` filing source
(`available`) and EODHD `unavailable` (token not yet configured). All five
agents complete end-to-end; `portfolio_risk_review` reads the frozen persona
snapshot, and `ic_memo` synthesizes the case's completed artifacts citing their
run ids. Broker state is unchanged across every run.

The frontend `/work` workspace is deployed to Vercel production (nivelo.in):
per-agent launch forms (source boundaries, 2+ symbols for comparisons, persona
id for portfolio risk), and an "Add research" panel on a case so IC Memo (and
any agent) can run after a run completes.

## Access Model

Launch, cancel, and finalize are open to every verified workspace member (the
`_investment_lead` gate was relaxed from `pm`/`cio` to any workspace identity on
2026-09-02). No portfolio state is reachable from agent work: cases, runs, and
artifacts are forward-only journal tables with no foreign keys to proposals,
trades, or broker records. Reintroduce a role check in `_investment_lead` if a
PM/CIO review step is ever required.

## DeepSeek Integration Notes

- pydantic-ai 2.35.1's default OpenAI provider uses the Responses API, which
  DeepSeek does not accept, and its open agentic tool loop never terminates
  against DeepSeek. `agent_runtime._generate_output` therefore drives the tool
  loop explicitly over the chat-completions API with
  `thinking: {"type": "disabled"}`, bounded tool rounds, and a completion-token
  budget.
- The Temporal worker requires Python 3.10+ (temporalio 1.32) and an
  `activity_executor` for its sync activities; the workflow is registered with
  `UnsandboxedWorkflowRunner` because `agent_runtime.py` imports `src.config`,
  which the sandbox blocks.

## Remaining / Deferred

- **Merge to `main`**: the frontend agent work is on `main` (PR #4); the
  backend `feature/agents` is still an open PR (#32). Merge it so the
  `deploy-gcp.yml` auto-deploy stays in sync.
- **EODHD token**: set `EODHD_API_TOKEN` in `/opt/fund/.env` and
  `/etc/nivelo/agent-worker.env` to unlock market data/fundamentals/news
  (comparative screen, event impact, and full deep-dive fundamentals currently
  degrade to partial/unavailable because only SEC filings are wired). NSE/BSE
  disclosures remain off by policy.
- **OpenTelemetry**: `src/telemetry.py` instruments FastAPI only; an OTLP
  exporter and worker span export are not yet wired or verified.
- **Frontend acceptance**: keyboard paths, mobile layout, and reduced-motion
  coverage for `/work` exist in `Work.test.tsx`; a manual pass on the deployed
  UI is still worthwhile.
- **Deferred by design** (docs 00/01/02): tenant isolation, external client
  sharing, custom agent definitions, trade proposals, autonomous execution.
