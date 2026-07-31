# GCP deployment architecture

How the fund runs in the cloud: what lives where, how data is stored, and how
the pieces connect. For deploy steps and cost estimates, see
[DEPLOY_GCP.md](DEPLOY_GCP.md).

The design goal is portability. One VM mirrors the laptop setup (local Postgres,
repo on disk, cron instead of launchd). Nothing depends on a Google-only service,
so leaving GCP is a `pg_dump` and one re-run of `bootstrap_vm.sh` on any Debian
box — not a rewrite.

## High-level layout

```
Your Mac                          GCP project
─────────                         ─────────────────────────────────────
deploy_gcp.sh ──scp/ssh──►  VM: fund-poc (e2-medium, asia-south1-a Mumbai)
     │                           │
     │ SSH tunnel :8000          ├── /opt/fund/              application
     ▼                           ├── PostgreSQL @ 127.0.0.1   journal (durable)
Browser → localhost:8000         ├── cron                     08:15 / 18:00 / 19:30
                                 ├── data/cache/              price & fundamentals
                                 ├── data/*.log               → Cloud Logging
                                 └── pg_dump (19:30) ────────► GCS bucket
```

| Location | Role |
| --- | --- |
| **Laptop** | Deploy, push code, open SSH tunnel to the dashboard. Not on the critical path for daily runs. |
| **VM** | Everything operational: Python app, PostgreSQL, cron, dashboard service, caches, logs. |
| **GCS bucket** | Off-machine journal backups (`journal/journal-YYYY-MM-DD.dump`). |
| **Cloud Logging** | Durable copy of log files via Google's Ops Agent. Readable even if the VM is down. |

## What runs on the VM

| Component | How it runs | Notes |
| --- | --- | --- |
| Morning cycle | cron 08:15 IST weekdays | `run_scheduled.sh cycle` → research, auto-approve, simulated orders |
| Close of day | cron 18:00 IST weekdays | `run_scheduled.sh close` → mark to close, record NAV |
| Journal backup | cron 19:30 IST weekdays | `backup_journal.sh` → pg_dump + upload to GCS |
| Dashboard | systemd `mutualfund-dashboard` | Always on at `127.0.0.1:8000`; reach via SSH tunnel only |
| PostgreSQL | Debian package, localhost only | Database `mutualfund`, role `fund` |
| Cloud Logging | Ops Agent | Tails `scheduled.log`, `fund.log`, `backup.log` |

Timezone is `Asia/Kolkata`, so crontab times are IST.

## Paths on the VM

Application root is **`/opt/fund/`**.

| Path | Purpose |
| --- | --- |
| `/opt/fund/.venv/` | Python virtualenv |
| `/opt/fund/.env` | Secrets (mode 600): Groq key, `DRY_RUN`, VM-generated `DATABASE_URL` |
| `/opt/fund/config/settings.yaml` | Fund policy (risk limits, turnover, universe) |
| `/opt/fund/run_daily.py` | Main entry point |
| `/opt/fund/data/cache/price_history.pkl` | Yahoo price cache (12 h TTL) |
| `/opt/fund/data/cache/fundamentals.json` | Screener.in fundamentals cache (7 d TTL) |
| `/opt/fund/data/scheduled.log` | Cron output for cycle and close |
| `/opt/fund/data/fund.log` | Application detail log |
| `/opt/fund/data/backup.log` | Nightly backup output |
| `/opt/fund/data/dashboard.log` | Dashboard systemd stdout/stderr |
| `/opt/fund/data/backups/journal-*.dump` | Local pg_dump copies (14 days kept) |
| `/opt/fund/data/briefings/` | Generated memos (narrative; decisions are in Postgres) |
| `/etc/cron.d/mutualfund` | Weekday schedule |
| `/etc/systemd/system/mutualfund-dashboard.service` | Dashboard unit file |
| `/var/lib/postgresql/...` | PostgreSQL data directory (Debian default) |

Default deploy settings (overridable via env vars in `deploy_gcp.sh`):

| Setting | Default |
| --- | --- |
| GCP project | `gcloud config` project |
| VM name | `fund-poc` |
| Zone | `asia-south1-a` (Mumbai) |
| Machine type | `e2-medium` (2 vCPU, 4 GB) |
| Disk | 30 GB balanced persistent |
| App directory | `/opt/fund` |
| Backup bucket | `GCP_BACKUP_BUCKET` env var |

## PostgreSQL: how the journal is stored

### Connection

1. `config/settings.yaml` defaults to `postgresql:///mutualfund`.
2. On bootstrap, `scripts/bootstrap_vm.sh` creates database `mutualfund`, role
   `fund` with a random password, and writes the DSN into `/opt/fund/.env`:
   ```
   DATABASE_URL=postgresql://fund:<password>@127.0.0.1:5432/mutualfund
   ```
3. `src/config.py` resolves the DSN: **`DATABASE_URL` in `.env` wins** over
   `settings.yaml`.
4. `src/journal/db.py` connects via `psycopg` to `127.0.0.1:5432` only. Port
   5432 is never opened to the internet.

When you push from the laptop, `deploy_gcp.sh` **strips `DATABASE_URL`** from
`.env` so your local Homebrew DSN does not overwrite the VM's password.

Schema is created automatically on first connect (`init_db` / `setup_check.py`).

### What Postgres holds (irreplaceable)

| Table | Contents |
| --- | --- |
| `runs` | Each daily cycle: date, status, portfolio value, turnover used |
| `proposals` | Buy/sell proposals with rationale, thesis, approval status |
| `trades` | Executed fills (simulated while `DRY_RUN=true`) |
| `position_meta` | Entry price, thesis, peak price per holding |
| `position_snapshots` | End-of-run holdings with scores, weights, risk metrics |
| `nav_history` | Daily NAV series vs benchmark |
| `risk_history` | Beta, tracking error, concentration |
| `news_digests` | LLM-generated news summaries per day |

### What files hold (rebuildable)

| File | Source | If lost |
| --- | --- | --- |
| `data/cache/price_history.pkl` | Yahoo Finance | Re-fetched next run (~minutes) |
| `data/cache/fundamentals.json` | Screener.in (~500 names) | Re-scraped (~8+ min at 1 req/s) |
| `data/briefings/` | Generated each run | Narrative only; journal has the decisions |
| `data/*.log` | Runtime output | Also in Cloud Logging |

Price and fundamentals caches are pushed from the laptop on deploy so the first
cloud run does not cold-start a full Screener scrape.

## How data persists

Three independent layers:

| Layer | Mechanism | Protects against | Retention |
| --- | --- | --- | --- |
| Persistent disk | 30 GB boot disk on the VM | Reboots, stop/start | Until VM is destroyed |
| pg_dump → GCS | Cron 19:30 IST | Disk failure, VM deletion | 14 days local; bucket policy is yours |
| Disk snapshots | `./scripts/deploy_gcp.sh snapshots` | Broken OS / packages | 7 nightly snapshots |

Backup flow (`scripts/backup_journal.sh`):

1. `pg_dump --format=custom` → `/opt/fund/data/backups/journal-YYYY-MM-DD.dump`
2. Upload to `gs://<bucket>/journal/`
3. Prune local dumps older than 14 days

Restore into a rebuilt VM:

```bash
gcloud storage cp gs://YOUR_BUCKET/journal/journal-2026-07-31.dump .
pg_restore --clean --if-exists --dbname="$DATABASE_URL" journal-2026-07-31.dump
```

## Secrets and environment

Everything sensitive lives in `/opt/fund/.env` (mode 600):

| Variable | Set by | Notes |
| --- | --- | --- |
| `DATABASE_URL` | Bootstrap on VM | Generated password; never pushed from laptop |
| `FUND_BACKUP_BUCKET` | Bootstrap | GCS bucket name for nightly dumps |
| `OPENAI_API_KEY` | Pushed from laptop | Groq or other OpenAI-compatible provider |
| `OPENAI_BASE_URL` | Pushed from laptop | Optional custom endpoint |
| `DRY_RUN` | Pushed from laptop | `true` keeps cloud runs on paper |
| `INITIAL_CAPITAL_INR` | Pushed from laptop | Starting NAV for tracking |
| `ANGELONE_*` | Pushed if present | Broker creds (unused while `DRY_RUN=true`) |

Fund policy (risk limits, turnover budget, universe) stays in tracked
`config/settings.yaml` — not secrets.

## Daily schedule (weekdays, IST)

| Time | Script | What happens |
| --- | --- | --- |
| 08:15 | `run_scheduled.sh cycle` | Fetch prices, fundamentals, news → quant screen → LLM proposals → risk validation → auto-approve (paper) → place simulated orders |
| 18:00 | `run_scheduled.sh close` | Mark portfolio at closing prices, record NAV and risk metrics. Waits until 18:00 because Yahoo's official close lags the bell. |
| 19:30 | `backup_journal.sh` | Dump journal to disk and GCS |

The dashboard is not on a schedule. It runs continuously as a systemd service
for manual review when needed. In unattended mode (`execution.auto_approve: true`
and `DRY_RUN=true`), the morning cycle does not wait for anyone at the dashboard.

## Accessing the dashboard

There is no public URL. The dashboard binds to `127.0.0.1:8000` only and has no
authentication, so access is through an SSH tunnel:

```bash
./scripts/deploy_gcp.sh dashboard
```

Then open **http://localhost:8000** in your browser. Ctrl-C closes the tunnel;
the dashboard keeps running on the VM.

## Inspecting from your laptop

```bash
./scripts/deploy_gcp.sh status      # fund summary (last run, NAV, positions)
./scripts/deploy_gcp.sh check         # health check (DB, config, credentials)
./scripts/deploy_gcp.sh logs          # tail data/scheduled.log on the VM
./scripts/deploy_gcp.sh cloudlogs     # read logs from Cloud Logging
./scripts/deploy_gcp.sh cycle           # force a morning run now
./scripts/deploy_gcp.sh close           # force a close run now
./scripts/deploy_gcp.sh backup          # manual pg_dump + GCS upload
./scripts/deploy_gcp.sh dashboard       # SSH tunnel to localhost:8000
./scripts/deploy_gcp.sh ssh             # shell on the VM
./scripts/deploy_gcp.sh stop | start    # pause/resume compute billing
```

On the VM after SSH:

```bash
cat /etc/cron.d/mutualfund
sudo systemctl status mutualfund-dashboard
ls -lh /opt/fund/data/backups/
gcloud storage ls gs://YOUR_BUCKET/journal/
cd /opt/fund && .venv/bin/python scripts/setup_check.py
```

## What is deliberately not used

| Service | Why not |
| --- | --- |
| **Cloud SQL** | Most expensive line item; painful to migrate off later. Local Postgres on the VM is free and portable. |
| **Cloud Run / Lambda** | Ephemeral filesystem loses caches on every cold start. Fundamentals re-scrape alone is ~500 sequential requests. Also needs a managed DB. |
| **Public :8000** | Dashboard has no auth and can approve trades. SSH tunnel only. |
| **GKE** | One process, one cron — a VM is fewer moving parts for a POC. |
| **Cloud NAT** | VM keeps a normal ephemeral public IP so scraping is not behind one shared NAT IP. |

## Related docs

- [DEPLOY_GCP.md](DEPLOY_GCP.md) — one-time setup, deploy commands, cost, security, day-75 exit plan
- [../README.md](../README.md) — local setup, daily use, fund policy
