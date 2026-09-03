#!/bin/bash
# Turn a fresh Debian VM into a machine that runs the fund unattended.
#
# Called by deploy_gcp.sh, but nothing in here is Google-specific: it works on
# any Debian or Ubuntu box (Hetzner, Oracle, a spare Linux machine), which is
# what keeps the exit from Google cheap.
#
#   sudo ./scripts/bootstrap_vm.sh --as-user mayank
#
# Idempotent: safe to re-run after a code push. It will not touch an existing
# DATABASE_URL, so the journal password survives re-bootstrapping.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

APP_USER=""
[ "${1:-}" = "--as-user" ] && APP_USER="${2:-}"
[ -n "$APP_USER" ] || APP_USER="${SUDO_USER:-$(id -un)}"

DB_NAME="${FUND_DB_NAME:-mutualfund}"
DB_USER="${FUND_DB_USER:-fund}"
BACKUP_BUCKET="${FUND_BACKUP_BUCKET:-}"
TZ_NAME="${FUND_TIMEZONE:-Asia/Kolkata}"

[ "$(id -u)" = "0" ] || {
    echo "error: run me with sudo" >&2
    exit 1
}
id "$APP_USER" >/dev/null 2>&1 || {
    echo "error: no such user: $APP_USER" >&2
    exit 1
}

APP_GROUP="$(id -gn "$APP_USER")"
SHARED_GROUP="${FUND_SHARED_GROUP:-fund}"

as_app() { runuser -u "$APP_USER" -- "$@"; }

echo "Bootstrapping $PROJECT_DIR for $APP_USER"

# --- Shared secrets group ---------------------------------------------------
# Collaborators added to this group can read /opt/fund/.env (mode 640).
getent group "$SHARED_GROUP" >/dev/null 2>&1 || groupadd "$SHARED_GROUP"
usermod -aG "$SHARED_GROUP" "$APP_USER" 2>/dev/null || true

# --- Clock ------------------------------------------------------------------
# The schedule is written in IST because the market is. Setting the whole box to
# Asia/Kolkata means crontab times, log timestamps and the fund's own idea of
# "today" all agree, which is one less thing to reason about at 08:15.
timedatectl set-timezone "$TZ_NAME"
echo "  timezone $TZ_NAME"

# --- Packages ---------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-dev build-essential \
    postgresql postgresql-client libpq-dev \
    git cron logrotate ca-certificates docker.io docker-compose >/dev/null ||
    {
        echo "error: package installation failed" >&2
        exit 1
    }
systemctl enable --now postgresql cron docker >/dev/null 2>&1
echo "  packages installed, PostgreSQL $(psql --version | awk '{print $3}') running"

# gsutil/gcloud is only needed to copy backups off the box. Absence is not fatal.
if ! command -v gcloud >/dev/null 2>&1; then
    apt-get install -y -qq google-cloud-cli >/dev/null 2>&1 ||
        echo "  note: google-cloud-cli unavailable; backups stay on the local disk"
fi

# --- Journal database -------------------------------------------------------
chown -R "$APP_USER":"$SHARED_GROUP" "$PROJECT_DIR"
find "$PROJECT_DIR" -type d -exec chmod 2775 {} +
find "$PROJECT_DIR" -type f ! -path "$PROJECT_DIR/.env" ! -path "$PROJECT_DIR/.venv/*" -exec chmod 664 {} +
find "$PROJECT_DIR/scripts" -type f -name '*.sh' -exec chmod 775 {} + 2>/dev/null || true
if [ -d "$PROJECT_DIR/.venv/bin" ]; then
    find "$PROJECT_DIR/.venv/bin" -type f -exec chmod 775 {} +
fi
chmod -R g+rw "$PROJECT_DIR/data" "$PROJECT_DIR/logs" 2>/dev/null || true
chmod g+s "$PROJECT_DIR" "$PROJECT_DIR/data" "$PROJECT_DIR/logs" 2>/dev/null || true
as_app mkdir -p "$PROJECT_DIR/data/backups"

if [ -f "$ENV_FILE" ] && grep -qE '^DATABASE_URL=postgresql' "$ENV_FILE"; then
    echo "  DATABASE_URL already present in .env; leaving the database alone"
else
    DB_PASSWORD="$(openssl rand -hex 24)"

    if ! sudo -u postgres psql -tAc \
        "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
        sudo -u postgres createuser "$DB_USER" || exit 1
    fi
    sudo -u postgres psql -qc \
        "ALTER ROLE \"$DB_USER\" WITH LOGIN PASSWORD '$DB_PASSWORD'" >/dev/null || exit 1

    if ! sudo -u postgres psql -tAc \
        "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
        sudo -u postgres createdb "$DB_NAME" -O "$DB_USER" || exit 1
    fi

    # Written to .env rather than settings.yaml because it carries a password,
    # and read over the loopback interface only: 5432 is never exposed.
    tmp="$(mktemp)"
    [ -f "$ENV_FILE" ] && grep -v '^DATABASE_URL=' "$ENV_FILE" >"$tmp"
    echo "DATABASE_URL=postgresql://$DB_USER:$DB_PASSWORD@127.0.0.1:5432/$DB_NAME" >>"$tmp"
    install -o "$APP_USER" -g "$SHARED_GROUP" -m 640 "$tmp" "$ENV_FILE"
    rm -f "$tmp"
    echo "  created database $DB_NAME owned by $DB_USER, DSN written to .env"
fi

if [ -n "$BACKUP_BUCKET" ] && ! grep -q '^FUND_BACKUP_BUCKET=' "$ENV_FILE"; then
    echo "FUND_BACKUP_BUCKET=$BACKUP_BUCKET" >>"$ENV_FILE"
    echo "  backups will be copied to gs://$BACKUP_BUCKET"
fi

# --- Python environment -----------------------------------------------------
if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
    as_app python3 -m venv "$PROJECT_DIR/.venv" || exit 1
fi
as_app "$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip
as_app "$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt" ||
    {
        echo "error: dependency installation failed" >&2
        exit 1
    }
echo "  virtualenv ready ($("$PROJECT_DIR/.venv/bin/python" --version))"

# --- Temporal ---------------------------------------------------------------
# ponytail: one VM stack is sufficient for the pilot; split Temporal onto its
# own managed database/cluster before multi-tenant production workloads.
install -d -o "$APP_USER" -g "$SHARED_GROUP" -m 750 /etc/nivelo
TEMPORAL_ENV=/etc/nivelo/temporal.env
if [ ! -f "$TEMPORAL_ENV" ]; then
    printf 'TEMPORAL_POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)" >"$TEMPORAL_ENV"
    chown "$APP_USER":"$SHARED_GROUP" "$TEMPORAL_ENV"
    chmod 640 "$TEMPORAL_ENV"
fi

AGENT_ENV=/etc/nivelo/agent-worker.env
if [ ! -f "$AGENT_ENV" ]; then
    DB_URL="$(grep '^DATABASE_URL=' "$ENV_FILE" | cut -d= -f2-)"
    EODHD="$(grep '^EODHD_API_TOKEN=.' "$ENV_FILE" | cut -d= -f2- || true)"
    SEC_UA="$(grep '^SEC_USER_AGENT=.' "$ENV_FILE" | cut -d= -f2- || true)"
    cat >"$AGENT_ENV" <<EOF
DATABASE_URL=${DB_URL/127.0.0.1/host.docker.internal}
TEMPORAL_ADDRESS=temporal:7233
TEMPORAL_NAMESPACE=default
WORKSPACE_AGENT_MODEL=
OPENAI_API_KEY=
EODHD_API_TOKEN=${EODHD}
SEC_USER_AGENT=${SEC_UA}
EOF
    chown "$APP_USER":"$SHARED_GROUP" "$AGENT_ENV"
    chmod 640 "$AGENT_ENV"
fi

install -D -m 0644 "$PROJECT_DIR/deploy/nivelo-temporal.service" /etc/systemd/system/nivelo-temporal.service
systemctl daemon-reload
systemctl enable --now nivelo-temporal >/dev/null 2>&1 &&
    echo "  Temporal server up; dashboard remains private at 127.0.0.1:8233" ||
    echo "  note: Temporal failed; check journalctl -u nivelo-temporal"

chmod +x "$PROJECT_DIR"/scripts/*.sh

# --- Schedule ---------------------------------------------------------------
# Generated from config/markets/*.yaml so US watch times track DST.
chmod +x "$PROJECT_DIR/scripts/run_market_intraday_watches.sh"
chmod +x "$PROJECT_DIR/scripts/run_intraday_watch.sh"
GENERATED_CRON="$PROJECT_DIR/data/generated_cron.txt"
as_app "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/generate_schedule.py" \
    --vm-tz "$TZ_NAME" \
    --app-user "$APP_USER" \
    --project-dir "$PROJECT_DIR" \
    --cron-only >"$GENERATED_CRON"
cat "$GENERATED_CRON" >/etc/cron.d/mutualfund
chmod 644 /etc/cron.d/mutualfund
echo "  cron installed from config/markets (see data/generated_cron.txt)"

# Intraday watch systemd units (one per market with intraday personas).
for market_id in india_equity us_equity crypto_spot_us; do
    market_unit="${market_id//_/-}"
    unit_file="/etc/systemd/system/mutualfund-${market_unit}-intraday-watch.service"
    as_app "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/generate_schedule.py" \
        --app-user "$APP_USER" \
        --project-dir "$PROJECT_DIR" \
        --systemd-only \
        --market "$market_id" >"$unit_file"
    systemctl disable "mutualfund-${market_unit}-intraday-watch" >/dev/null 2>&1 || true
done
systemctl daemon-reload
echo "  intraday watch services installed (cron starts them on session days)"

# Legacy calan-only unit kept for backward compatibility during migration.
chmod +x "$PROJECT_DIR/scripts/run_calan_watch.sh"
cat >/etc/systemd/system/mutualfund-calan-watch.service <<UNIT
[Unit]
Description=Calan intraday paper watch (deprecated — use india-equity unit)
After=network.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/scripts/run_calan_watch.sh
Restart=no
TimeoutStopSec=60
KillMode=mixed
StandardOutput=append:$PROJECT_DIR/data/calan_watch.log
StandardError=append:$PROJECT_DIR/data/calan_watch.log

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl disable mutualfund-calan-watch >/dev/null 2>&1 || true

cat >/etc/logrotate.d/mutualfund <<ROTATE
$PROJECT_DIR/data/*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
ROTATE

# --- Dashboard service ------------------------------------------------------
# The review UI runs as a managed service rather than something started by hand
# over SSH: a backgrounded python process would keep the SSH session open and
# die with the next reboot. It binds to dashboard.host from settings.yaml,
# which may be public when the VM firewall allows tcp:8000.
cat >/etc/systemd/system/mutualfund-dashboard.service <<UNIT
[Unit]
Description=AI Mutual Fund Manager dashboard
After=network.target postgresql.service

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python run_daily.py dashboard
Restart=on-failure
RestartSec=5
StandardOutput=append:$PROJECT_DIR/data/dashboard.log
StandardError=append:$PROJECT_DIR/data/dashboard.log

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now mutualfund-dashboard >/dev/null 2>&1 &&
    echo "  dashboard service up on dashboard.host:dashboard.port from settings.yaml" ||
    echo "  note: dashboard service failed; check journalctl -u mutualfund-dashboard"

# --- Cloud Logging ----------------------------------------------------------
# The fund runs unattended, so the record of why a trade fired must outlive the
# machine. Google's Ops Agent tails the app's log files and ships them to Cloud
# Logging, whose free tier (50 GiB/month) is orders of magnitude more than this
# app writes, so at this volume it is effectively free. The VM was created with
# the logging.write scope for exactly this. On a host without the agent (Hetzner,
# Oracle) the whole block is skipped and file logs plus logrotate stand alone.
if [ "${FUND_SKIP_CLOUD_LOGGING:-}" != "1" ] &&
    curl -fsS -m 5 -H 'Metadata-Flavor: Google' \
        http://metadata.google.internal/computeMetadata/v1/instance/id >/dev/null 2>&1; then
    if [ ! -f /etc/google-cloud-ops-agent/config.yaml ]; then
        curl -fsSO https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh &&
            bash add-google-cloud-ops-agent-repo.sh --also-install >/dev/null 2>&1
        rm -f add-google-cloud-ops-agent-repo.sh
    fi

    if [ -d /etc/google-cloud-ops-agent ]; then
        # scheduled.log is the story of each cron run; fund.log is the app's own
        # detail; backup.log records the nightly dump. Each becomes its own log
        # stream so deploy_gcp.sh cloudlogs can read them back by name.
        cat >/etc/google-cloud-ops-agent/config.yaml <<OPS
logging:
  receivers:
    fund_scheduled:
      type: files
      include_paths: [$PROJECT_DIR/data/scheduled.log]
    fund_app:
      type: files
      include_paths: [$PROJECT_DIR/data/fund.log]
    fund_backup:
      type: files
      include_paths: [$PROJECT_DIR/data/backup.log]
    fund_calan:
      type: files
      include_paths: [$PROJECT_DIR/data/calan_watch.log]
    fund_us_intraday:
      type: files
      include_paths: [$PROJECT_DIR/data/us_intraday_watch.log, $PROJECT_DIR/data/*_us_watch.log]
  service:
    pipelines:
      fund:
        receivers: [fund_scheduled, fund_app, fund_backup, fund_calan, fund_us_intraday]
OPS
        systemctl restart google-cloud-ops-agent >/dev/null 2>&1 &&
            echo "  Cloud Logging: Ops Agent shipping data/*.log (free at this volume)" ||
            echo "  note: Ops Agent installed but did not restart; check it on the VM"
    else
        echo "  note: Ops Agent install failed; file logs and logrotate still apply"
    fi
else
    echo "  Cloud Logging skipped (not on GCE or disabled); file logs still apply"
fi

# --- Verify -----------------------------------------------------------------
echo
as_app bash -c "cd '$PROJECT_DIR' && .venv/bin/python scripts/setup_check.py" || true
