# Phase 3: Durable Agent Runtime

## Decision

Each `agent_run` maps to one Temporal workflow (`agent-run-{uuid}`) on the `nivelo-agent-work` task queue. The FastAPI control plane creates work and dispatches it; the separate worker executes it.

## Execution

- Dispatch records the Temporal workflow ID.
- The workflow runs bounded activities with a 15-minute start-to-close timeout.
- Activity transitions and terminal errors are journalled in `agent_run_events`.
- Duplicate starts reuse the `(case_id, idempotency_key)` record.
- Cancellation changes the journalled run state; the deployed worker must also receive Temporal cancellation in the production rollout.

## Failure Behavior

If Temporal is not configured, work remains explicitly queued. If the model or a source is unavailable, the run fails or returns an unavailable artifact; it never invents evidence.

## Acceptance

Verify restart, retry, duplicate submit, cancellation, timeout, and partial-source-failure behavior against PostgreSQL and a Temporal test environment.
