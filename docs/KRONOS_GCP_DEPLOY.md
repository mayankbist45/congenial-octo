# Kronos on GCP

This document describes the practical deployment path for Kronos as of **August 11, 2026**.

## Recommended shape

- Keep the existing fund VM for the CPU app, scheduler, caches, and database.
- Use a separate **GPU-capable validation VM** for Kronos-first experiments.
- Start with **pretrained Kronos-small** in-process.
- Keep `shadow` mode first.
- Focus the first evaluation loop on **US intraday** using `calan_us`.

## Why this shape

- The repo already supports a VM-centric GCP workflow.
- Kronos is already integrated in `src/intraday/kronos_service.py`.
- Pretrained Kronos is enough for initial inference validation.
- Fine-tuning should follow only after offline evaluation and calibration.

## New scripts

### Deploy a Kronos GPU VM

```bash
export GCP_PROJECT=your-project
export GCP_KRONOS_ZONE=us-central1-a
./scripts/deploy_kronos_gcp.sh deploy
```

### Deploy a Kronos CPU-only VM

If the billing account is still in free tier or you want a cheap validation box first:

```bash
export GCP_PROJECT=your-project
export GCP_KRONOS_PROFILE=cpu
export GCP_KRONOS_ZONE=us-central1-a
./scripts/deploy_kronos_gcp.sh deploy
```

Recommended CPU-only defaults:

- instance: `congenial-octo-kronos`
- zone: `us-central1-a`
- machine type: `e2-standard-4`
- disk: `100GB`

CPU-only tradeoff:

- works on free-tier-compatible paid compute without GPU accelerators
- slower first-load and batch inference
- still good enough for shadow validation, export, and offline evaluation

Useful follow-ups:

```bash
./scripts/deploy_kronos_gcp.sh status
./scripts/deploy_kronos_gcp.sh check-gpu
./scripts/deploy_kronos_gcp.sh runtime-check
./scripts/deploy_kronos_gcp.sh run-backtest
./scripts/deploy_kronos_gcp.sh ssh
```

Defaults:

- instance: `congenial-octo-kronos`
- zone: `us-central1-a`
- machine type: `g2-standard-4`
- disk: `100GB`

## Offline shadow evaluation

Run Kronos against cached or freshly fetched intraday candles and compare its predicted return signal with realized forward returns.

```bash
./.venv/bin/python scripts/kronos_shadow_eval.py \
  --persona calan_us \
  --start 2026-08-10 \
  --end 2026-08-10 \
  --max-symbols 5
```

Outputs:

- CSV of per-window forecast-vs-realized rows
- JSON summary with directional hit rate and expected/realized correlation

Default output directory:

- `data/kronos/`

## Export fine-tuning data

When enough historical coverage exists, export candles into a Kronos-friendly training dataset.

```bash
./.venv/bin/python scripts/export_kronos_finetune_data.py \
  --persona calan_us \
  --start 2026-08-01 \
  --end 2026-08-11 \
  --max-symbols 12
```

Outputs:

- combined CSV under `data/kronos/exports/<persona>/`
- manifest JSON describing dates, symbols, and row count

## Runtime migration path

`src/intraday/kronos_service.py` now supports:

- `backend: local`
- `backend: http`

Phase 1 should keep:

```yaml
intraday:
  kronos:
    backend: "local"
    mode: "shadow"
```

Later, if Kronos should move to a dedicated inference service, switch to:

```yaml
intraday:
  kronos:
    backend: "http"
    endpoint_url: "http://kronos-internal:8090"
```

That migration should not require changes in candidate ranking logic.
