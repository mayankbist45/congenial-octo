# Phase 2: Curated Agent Contracts

## Agents

1. Company Deep Dive
2. Event Impact
3. Comparative Screen
4. Portfolio Risk Review
5. IC Memo

## Contract

Every agent accepts a bounded brief and normalized market, exchange, currency, symbols, and optional portfolio-persona scope. Each versioned contract declares its permitted tools, required source categories, timeout, retry limit, and token budget. The result is an `EvidenceArtifact`: structured data, key signals, coverage status, caveats, memo, assessment, confidence, source labels, and next step.

Assessments are limited to `strengthen`, `monitor`, `investigate`, `no_action`, and `escalate`. No buy/sell, sizing, or order instruction is permitted.

## Validation

Company and comparison agents require symbols; comparison requires two symbols; portfolio risk requires a persona; IC Memo requires completed evidence in the case and has only the case-artifact tool. Each agent's allowed tools are real, live source fetches bound to that contract (never a shared grab-bag); a contract therefore shapes both the evidence it may see and the scope it needs. EODHD is the optional multi-market source (market data, fundamentals, news), SEC EDGAR is the US filing source and needs only a `SEC_USER_AGENT`, NSE disclosures are disabled by default, and BSE disclosures require an approved BSE data-feed URL and key.

## Deferred

User-authored prompts, arbitrary tool access, and custom agent marketplace behavior.
