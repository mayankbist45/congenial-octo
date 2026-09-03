# Phase 4: Evidence And Source Quality

## Evidence Order

Render structured data first, then key signals, source links/timestamps, coverage state, caveats, memo, assessment, confidence, and next step.

## Coverage Rules

Every source declares one of `available`, `partial`, `stale`, or `unavailable`. Unsupported markets are accepted as scope but must return their true availability. A run with partial, stale, or unavailable critical evidence cannot claim high confidence.

With live tool calls, coverage is derived from the sources a run actually
consulted: the artifact's `coverage_status` is normalized to the source-based
truth (`available` only when every required category returned usable data,
`partial` when some did, `unavailable` when none did), and `source_labels` only
ever cite providers the run reached. The `portfolio_snapshot` source reads the
frozen published persona snapshot; IC Memo reads this case's completed
artifacts and cites their run ids. SEC EDGAR (US filings) needs only
`SEC_USER_AGENT`; EODHD (`market_data`/`fundamentals`/`news`) needs an
`EODHD_API_TOKEN`; NSE/BSE disclosures stay disabled by policy.

## Acceptance

Tests cover unavailable markets, stale data, conflicting sources, malformed model output, and failed tools. All must preserve an inspectable artifact/event trail.

## Deferred

Paid data-provider connectors are added only with a customer-backed coverage requirement.
