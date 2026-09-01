# Temporal Worker Deployment

The FastAPI service creates work. This worker is the only process that executes agent prompts.

## VM Deployment

GitHub Actions bootstraps Temporal whenever `deploy/`, `Dockerfile.agent-worker`,
`requirements.txt`, or deployment scripts change. `nivelo-temporal.service`
starts the Temporal server, a dedicated Temporal PostgreSQL volume, and UI.
The UI and gRPC endpoint bind to loopback only.

Open the dashboard through an SSH tunnel:

```bash
gcloud compute ssh YOUR_VM --zone YOUR_ZONE -- -L 8233:127.0.0.1:8233
```

Then visit `http://127.0.0.1:8233`. Do not publish the UI through Caddy.

Agent execution remains off after deployment. It was enabled on
2026-09-02 with a DeepSeek worker. The worker runs with **host networking**
so it reaches both Temporal and the loopback-only fund PostgreSQL:

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
See `08-rollout-and-remaining.md` for the pilot result and the DeepSeek /
pydantic-ai notes.

## Verify

```bash
sudo systemctl status nivelo-temporal
sudo journalctl -u nivelo-temporal -n 100 --no-pager
sudo docker compose --env-file /etc/nivelo/temporal.env -f /opt/fund/deploy/temporal-compose.yml ps
```

Start one non-production research case and verify its `temporal_workflow_id`, trace ID, event history, and persisted artifact. Confirm no proposal, trade, or holding records changed.
