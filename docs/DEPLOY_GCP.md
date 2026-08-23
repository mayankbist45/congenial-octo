# Running the fund on Google Cloud

A proof of concept that takes the laptop off the critical path: the fund runs
weekdays at 08:15 and 18:00 IST whether or not this MacBook is awake.

For a full picture of paths, Postgres tables, persistence layers, and how the
pieces connect, see [GCP_ARCHITECTURE.md](GCP_ARCHITECTURE.md).

The architecture is deliberately the same as local — **one VM, PostgreSQL on
localhost, the repo on a persistent disk, cron where launchd was**. Nothing is
built on a Google-only service, so moving to Hetzner, Oracle or a spare Linux
box later is a `pg_dump` and one re-run of `bootstrap_vm.sh`, not a rewrite.

## Why a VM and not serverless

The daily cycle takes minutes and runs twice a day, which sounds like a perfect
fit for functions. Two things spoil it. Cloud Run and Lambda have **ephemeral
filesystems**, so `data/cache/price_history.pkl` and `fundamentals.json` are lost
on every cold start — and rebuilding the fundamentals cache means roughly 500
Screener requests at one second apart. And the journal then needs a **managed
database**, which is the single most expensive line item and the one thing that
would be painful to migrate off later.

A serverless design is the right answer for a bigger version of this. For a POC
on trial credit, one small VM is fewer moving parts and a cheaper exit.

## What the $300 trial buys

Approximate `asia-south1` (Mumbai) on-demand pricing:

| Item | Per month | Over 90 days |
| --- | --- | --- |
| `e2-medium` (2 shared vCPU, 4 GB) | ~$28 | ~$84 |
| 30 GB balanced persistent disk | ~$3.50 | ~$10 |
| Egress and object storage | <$1 | ~$2 |
| **Total** | **~$32** | **~$96** |

That leaves two thirds of the credit unspent, which is the point: no tuning, no
stopping the instance overnight, no worrying that a re-scrape costs money.

Mumbai matters more here than it looks. Screener, Yahoo and the Indian news
feeds are all served from or near India, and the fundamentals scrape is
sequential with a one-second delay per name, so round-trip latency is paid ~500
times over. The alternative — an Always Free `e2-micro` in `us-central1` — is
free forever but slower and only 1 GB of RAM.

## One-time setup

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable compute.googleapis.com
```

Set a **budget alert** before anything else, in the console under Billing →
Budgets & alerts: one budget on the billing account, alerts at 25% and 50% of
$300. Credit does not stop the meter when it runs out; it stops when the trial
account is not upgraded, and the alert is what tells you which is happening.

Optionally, a bucket for off-machine journal dumps:

```bash
export GCP_BACKUP_BUCKET=yourname-fund-backups
./scripts/deploy_gcp.sh bucket
```

## Deploy

```bash
export GCP_BACKUP_BUCKET=yourname-fund-backups   # optional but recommended
./scripts/deploy_gcp.sh deploy
```

That single command creates the VM, pushes the fund, and provisions it:

1. **Creates** an `e2-medium` in `asia-south1-a` on Debian 12 with a 30 GB
   balanced disk.
2. **Pushes** everything git tracks (from the working tree, so uncommitted
   `settings.yaml` edits go too), plus `.env` at mode 600, plus the price and
   fundamentals caches so the first run does not re-scrape the universe.
3. **Bootstraps** it: timezone `Asia/Kolkata`, PostgreSQL, a `fund` role with a
   freshly generated password, the `mutualfund` database, a virtualenv from
   `requirements.txt`, cron, and logrotate. Then it runs `setup_check.py`, which
   creates the journal schema.

If you want merges to `main` to deploy automatically after the VM exists, see
[GITHUB_ACTIONS_GCP.md](GITHUB_ACTIONS_GCP.md).

`DATABASE_URL` is stripped from the `.env` on the way up — the laptop's DSN
points at a Homebrew server. The VM's own DSN with its generated password is
written on the machine and never leaves it.

The script is idempotent. After changing code locally:

```bash
./scripts/deploy_gcp.sh push
./scripts/deploy_gcp.sh bootstrap   # only if requirements.txt changed
```

## Daily operation

```bash
./scripts/deploy_gcp.sh status      # the fund's own status report
./scripts/deploy_gcp.sh logs        # follow data/scheduled.log on the VM
./scripts/deploy_gcp.sh cloudlogs   # read what shipped to Cloud Logging
./scripts/deploy_gcp.sh cycle       # force a run now, then show the tail
./scripts/deploy_gcp.sh dashboard   # start the dashboard, tunnel it to :8000
./scripts/deploy_gcp.sh ssh
```

The schedule installed at `/etc/cron.d/mutualfund`, weekdays, IST:

| Time | Command | What it does |
| --- | --- | --- |
| 08:15 | `run_scheduled.sh cycle` | research, self-approve, place simulated orders |
| 18:00 | `run_scheduled.sh close` | mark the book at closing prices, record NAV |
| 19:30 | `backup_journal.sh` | `pg_dump` the journal, copy it to the bucket |

Every run's output goes to `data/scheduled.log` on the disk and, via the Ops
Agent, to Cloud Logging (see below), so the history survives the machine.

`DRY_RUN=true` travels up in the `.env`, so the cloud copy is paper trading and
auto-approval stays confined to paper exactly as it is locally.

## Carrying the existing journal across

The VM's PostgreSQL is not reachable from here, and should not be. Use
`migrate_journal.py` in both directions with a SQLite file as the transport,
which keeps row ids — and therefore the links between runs, proposals and fills
— intact:

```bash
# on the laptop
python scripts/migrate_journal.py --from postgresql:///mutualfund \
                                  --to sqlite://data/journal-export.db
gcloud compute scp data/journal-export.db congenial-octo-fund-poc:/opt/fund/data/ --zone asia-south1-a

# on the VM
./scripts/deploy_gcp.sh ssh
cd /opt/fund && .venv/bin/python scripts/migrate_journal.py \
    --from sqlite://data/journal-export.db --force
```

Or skip it and let the cloud fund start with a clean NAV series. For a POC
that is often the more honest track record.

## Persistence and backups

| Data | Where it lives | If it is lost |
| --- | --- | --- |
| Journal: runs, proposals, fills, NAV, risk | PostgreSQL on the VM disk | Irreplaceable — this is what the backups protect |
| Price history, fundamentals | `data/cache/` on the VM disk | Rebuilt automatically, but the fundamentals scrape is slow |
| Briefings, memos, logs | `data/` on the VM disk | Only the narrative record; the journal has the decisions |
| Secrets | `.env`, mode 600 | Re-push from the laptop |

Three layers protect it. The **persistent disk** survives reboots and
stop/start, unlike a container filesystem. The **nightly `pg_dump`** goes to GCS
and is the backup that counts, since a dump on the same disk as its database is
not a backup. Optional **disk snapshots** cover breaking the machine rather than
the data:

```bash
./scripts/deploy_gcp.sh snapshots   # nightly, 7 kept
```

### Logs that outlive the machine

Beyond the disk, the bootstrap installs Google's **Ops Agent**, which tails
`data/scheduled.log`, `data/fund.log` and `data/backup.log` and ships them to
**Cloud Logging**. So the record of why a trade fired survives even if the VM
does not. Ingestion is free up to 50 GiB/month and this app writes a few MB, so
at POC volume it costs nothing. Read it back without SSH:

```bash
./scripts/deploy_gcp.sh cloudlogs            # last 100 lines, last 2 days
./scripts/deploy_gcp.sh cloudlogs 500 7d     # more lines, wider window
```

The three streams are `fund_scheduled`, `fund_app` and `fund_backup` under the
VM in the console's Logs Explorer. To opt out entirely, bootstrap with
`FUND_SKIP_CLOUD_LOGGING=1` and only the on-disk logs remain.

Restoring a journal into a rebuilt VM:

```bash
gcloud storage cp gs://YOUR_BUCKET/journal/journal-2026-08-01.dump .
pg_restore --clean --if-exists --dbname="$DATABASE_URL" journal-2026-08-01.dump
```

## Security

### React API proxy

The React workspace must not call the VM's port `8000` directly. Reserve a
static external IP, create a temporary `sslip.io` hostname for it, then run on
the VM:

```bash
sudo DASHBOARD_PUBLIC_HOST=203-0-113-10.sslip.io ./scripts/setup_https_proxy.sh
```

Open only TCP `80` and `443` on the VM tag and delete the public TCP `8000`
rule. Caddy terminates TLS and only forwards `/api/*` and `/healthz` to the
loopback FastAPI service; it does not expose the legacy Jinja pages. Configure
the Vercel rewrite and FastAPI `web.allowed_origins` with the resulting HTTPS
origin before setting `VITE_DATA_MODE=live`.

Only SSH is reachable. The dashboard binds to `127.0.0.1` because
`dashboard.host` says so, PostgreSQL listens on loopback only, and no firewall
rule is created for port 8000 — the `dashboard` subcommand tunnels it over SSH
instead. Access uses OS Login, so it follows your Google identity rather than a
key file copied around.

The default VPC allows SSH from anywhere. Worth narrowing to your own address:

```bash
gcloud compute firewall-rules create fund-ssh-from-me \
    --allow tcp:22 --source-ranges "$(curl -s ifconfig.me)/32" --target-tags fund
gcloud compute firewall-rules delete default-allow-ssh
```

Note that this needs redoing whenever your home IP changes.

`.env` on the VM holds the Groq key, the generated database password and — if
you ever put them there — Angel One credentials. It is mode 600 and owned by
your login user. Keep `DRY_RUN=true` while the box is a POC: a machine you are
still learning to operate is not where live orders should originate.

## The day-75 decision

Trial credit expires at **90 days or $300, whichever comes first**, and when a
trial ends resources are suspended and eventually deleted unless the account is
upgraded to paid. Put a reminder at day 75. Three ways out, all cheap because
nothing depends on a Google-specific service:

1. **Stay, and pay** — upgrade to a paid account. About $32/month for the same
   machine; downsizing to `e2-small` roughly halves it.
2. **Always Free `e2-micro`** — rebuild in `us-central1` and restore the dump.
   Free forever, 1 GB of RAM, and noticeably slower against Indian data sources.
3. **Leave Google** — Hetzner at ~€4/month is more machine than `e2-medium` for
   an eighth of the price. `bootstrap_vm.sh` runs there unchanged; restore the
   dump and reinstall cron.

Before any of them:

```bash
./scripts/deploy_gcp.sh backup   # fresh dump in the bucket
```

And if the POC has simply answered its question:

```bash
./scripts/deploy_gcp.sh destroy
```
