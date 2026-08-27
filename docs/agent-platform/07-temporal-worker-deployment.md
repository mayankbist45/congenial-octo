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

Agent execution remains off after deployment. To enable it after the Phase 3
review, set an approved model and provider credential in
`/etc/nivelo/agent-worker.env`, add `TEMPORAL_ADDRESS=127.0.0.1:7233` to
`/opt/fund/.env`, and start the worker profile:

```bash
cd /opt/fund
sudo docker compose --env-file /etc/nivelo/temporal.env -f deploy/temporal-compose.yml --profile agent up -d agent-worker
```

After this explicit start, every GitHub Actions source deployment rebuilds the
running worker. It never enables a stopped worker.

## Verify

```bash
sudo systemctl status nivelo-temporal
sudo journalctl -u nivelo-temporal -n 100 --no-pager
sudo docker compose --env-file /etc/nivelo/temporal.env -f /opt/fund/deploy/temporal-compose.yml ps
```

Start one non-production research case and verify its `temporal_workflow_id`, trace ID, event history, and persisted artifact. Confirm no proposal, trade, or holding records changed.
