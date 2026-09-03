# Phase 3: Durable Agent Runtime

## Decision

Each `agent_run` maps to one Temporal workflow (`agent-run-{uuid}`) on the `nivelo-agent-work` task queue. The FastAPI control plane creates work and dispatches it; the separate worker executes it.

## Execution

- Dispatch records the Temporal workflow ID.
- The workflow runs bounded activities with a 15-minute start-to-close timeout.
- The model runs a **bounded gather → synthesize loop** against the OpenAI
  chat-completions API: up to three tool-call rounds let the model fetch the
  sources its contract allows, then a final request returns the structured
  `EvidenceArtifact` JSON. (pydantic-ai's open agentic loop never terminates
  against DeepSeek, so the runtime drives the loop explicitly.)
- Each tool call records a `SourceRecord` for the audit trail. The generation
  budget is enforced on completion tokens so re-sent tool history cannot trip
  it; DeepSeek thinking mode is disabled so function tool-calls work.
- Activity transitions and terminal errors are journalled in `agent_run_events`.
- Duplicate starts reuse the `(case_id, idempotency_key)` record.
- Cancellation changes the journalled run state; the deployed worker must also receive Temporal cancellation in the production rollout.

## Failure Behavior

If Temporal is not configured, work remains explicitly queued. If the model or a source is unavailable, the run fails or returns an unavailable artifact; it never invents evidence. The model's JSON is coerced leniently (schema drift is repaired) and coverage is normalized to the recorded sources before persistence.

## Acceptance

Verify restart, retry, duplicate submit, cancellation, timeout, and partial-source-failure behavior against PostgreSQL and a Temporal test environment.
