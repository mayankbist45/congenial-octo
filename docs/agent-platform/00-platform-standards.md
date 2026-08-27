# Agent Platform Standards

## Decision

Production agent workers run on Python 3.11+, PostgreSQL, PydanticAI, Temporal, and OpenTelemetry. The existing FastAPI service remains the authenticated control plane; the worker is a separately deployed process.

## Contracts

- Pydantic validates every request, tool input, source record, and artifact.
- PostgreSQL is required in production. SQLite is supported only for focused tests.
- Dependencies are pinned in `requirements.txt`; update pins and the worker image together.
- Public APIs are FastAPI/OpenAPI endpoints under `/api/v1/workspace`.
- Raw model text is diagnostic data, never a trusted artifact.

## Security And Operations

- Agents are research-only. They have no imports from broker, proposal, trade, or execution modules.
- All work has an idempotency key, timeout, retry boundary, trace ID, and auditable terminal state.
- Do not record credentials, bearer tokens, or complete source documents in traces or logs.
- Instrument FastAPI and worker activities with OpenTelemetry; export through standard OTLP environment configuration.

## Acceptance

CI runs backend unit tests, PostgreSQL integration tests, Temporal workflow tests, and frontend tests from the two child repositories.

## Deferred

Tenant isolation and custom agent definitions are not part of the first pilot.
