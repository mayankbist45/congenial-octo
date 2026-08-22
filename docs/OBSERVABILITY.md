# Observability

This repo now exposes operations health in two layers:

- the dashboard at `/operations`
- structured `OPS_EVENT` log lines that Cloud Logging / Monitoring can consume

## What is emitted

The wrappers and app now produce these structured events:

- `scheduled_run`
  - `job=long_cycle|close|...`
  - `status=start|success|failure`
- `backup_run`
  - local dump lifecycle
- `backup_upload`
  - off-machine GCS upload lifecycle
- `intraday_launcher`
  - one launcher run per market
- `intraday_watch`
  - one watch wrapper run per persona
- `intraday_heartbeat`
  - periodic heartbeat from the live watch loop
  - includes `market_id`, `persona_id`, `session_state`
- `dashboard_service`
  - emitted on dashboard startup

All events are written as single-line JSON prefixed by `OPS_EVENT`.

## Dashboard page

`/operations` shows:

- morning portfolio run
- close run
- backup run
- India intraday watch
- US intraday watch
- dashboard service
- recent incidents from failed ops events and failed `run_stage_events`

This page is the fastest way to answer:

- did the morning cycle finish today?
- did close run?
- is backup current?
- are intraday watches alive during session windows?
- what failed most recently?

## GCP setup

Provision the first-pass Cloud Monitoring objects with:

```bash
chmod +x scripts/setup_gcp_observability.sh
GCP_PROJECT=congenial-octo \
ALERT_EMAILS=mayankbist45@gmail.com,calanpereira98@gmail.com \
./scripts/setup_gcp_observability.sh
```

What it creates:

- logs-based metrics for daily run success/failure
- logs-based metrics for close success/failure
- logs-based metrics for backup success/failure
- logs-based metrics for India/US intraday heartbeats
- logs-based metrics for India/US intraday launcher failures
- logs-based metric for dashboard restarts
- email notification channels
- first-pass alert policies

Important:

- email channels may need verification before alerts deliver
- absence alerts are intentionally conservative and should be tuned after a few real weekdays
- the script is idempotent for metrics, but alert policy creation may need manual dedupe if you rerun it many times

## Useful follow-on metrics

The current implementation gives a strong v1. Next high-value additions would be:

- per-stage latency percentiles from `run_stage_events`
- upstream dependency failure counts by provider
- cache freshness for prices, fundamentals, and news digests
- built-in VM alerts for CPU, memory, disk, and instance uptime
- restore-drill tracking for backup integrity, not just backup creation
