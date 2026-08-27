# Phase 4: Evidence And Source Quality

## Evidence Order

Render structured data first, then key signals, source links/timestamps, coverage state, caveats, memo, assessment, confidence, and next step.

## Coverage Rules

Every source declares one of `available`, `partial`, `stale`, or `unavailable`. Unsupported markets are accepted as scope but must return their true availability. A run with partial, stale, or unavailable critical evidence cannot claim high confidence.

## Acceptance

Tests cover unavailable markets, stale data, conflicting sources, malformed model output, and failed tools. All must preserve an inspectable artifact/event trail.

## Deferred

Paid data-provider connectors are added only with a customer-backed coverage requirement.
