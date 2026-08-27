# Temporal Worker Deployment

The FastAPI service creates work. This worker is the only process that executes agent prompts.

## Build And Install

On the production VM after the source is deployed:

```bash
cd /opt/fund
sudo docker build -f Dockerfile.agent-worker -t nivelo-agent-worker:latest .
sudo install -D -m 0644 deploy/nivelo-agent-worker.service /etc/systemd/system/nivelo-agent-worker.service
sudo install -D -m 0600 deploy/agent-worker.env.example /etc/nivelo/agent-worker.env
sudoedit /etc/nivelo/agent-worker.env
sudo systemctl daemon-reload
sudo systemctl enable --now nivelo-agent-worker
```

`TEMPORAL_ADDRESS` must point to a self-hosted Temporal service reachable from the VM. `WORKSPACE_AGENT_MODEL` and its provider credential must be approved and configured on the host; no fallback model is used.

## Verify

```bash
sudo systemctl status nivelo-agent-worker
sudo journalctl -u nivelo-agent-worker -n 100 --no-pager
```

Start one non-production research case and verify its `temporal_workflow_id`, trace ID, event history, and persisted artifact. Confirm no proposal, trade, or holding records changed.
