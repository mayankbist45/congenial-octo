# Phase 1: Work Domain And Access

## Goal

Persist collaborative research cases independently from fund strategy runs and execution records.

## Interfaces

- `POST /api/v1/workspace/cases` creates a case for `pm` and `cio` roles.
- `GET /api/v1/workspace/cases` and `GET /api/v1/workspace/cases/{id}` are workspace-readable.
- `POST /api/v1/workspace/cases/{id}/comments` is member-readable/writable.
- `POST /api/v1/workspace/cases/{id}/finalize` is PM/CIO-only.

## Persistence

`research_cases`, `agent_runs`, `agent_run_events`, `agent_artifacts`, `agent_sources`, and `case_comments` are forward-only journal tables. No foreign key reaches proposals, trades, or broker records.

## Acceptance

PM/CIO can create and finalize. Members can read/comment but cannot launch or finalize. Admin remains operational-only. Every rejected request returns 403 and no portfolio state changes.

## Deferred

Per-tenant workspace IDs and external client sharing.
