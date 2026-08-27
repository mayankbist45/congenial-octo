# Phase 2: Curated Agent Contracts

## Agents

1. Company Deep Dive
2. Event Impact
3. Comparative Screen
4. Portfolio Risk Review
5. IC Memo

## Contract

Every agent accepts a bounded brief and normalized market, exchange, currency, symbols, and optional portfolio-persona scope. The result is an `EvidenceArtifact`: structured data, key signals, coverage status, caveats, memo, assessment, confidence, and next step.

Assessments are limited to `strengthen`, `monitor`, `investigate`, `no_action`, and `escalate`. No buy/sell, sizing, or order instruction is permitted.

## Validation

Company and comparison agents require symbols; comparison requires two symbols; portfolio risk requires a persona; IC Memo requires completed evidence in the case. Use PydanticAI's test model for deterministic contract tests.

## Deferred

User-authored prompts, arbitrary tool access, and custom agent marketplace behavior.
