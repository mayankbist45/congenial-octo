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

as_app() { runuser -u "$APP_USER" -- "$@"; }

echo "Bootstrapping $PROJECT_DIR for $APP_USER"

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
    git cron logrotate ca-certificates >/dev/null ||
    {
        echo "error: package installation failed" >&2
        exit 1
    }
systemctl enable --now postgresql cron >/dev/null 2>&1
echo "  packages installed, PostgreSQL $(psql --version | awk '{print $3}') running"

# gsutil/gcloud is only needed to copy backups off the box. Absence is not fatal.
if ! command -v gcloud >/dev/null 2>&1; then
    apt-get install -y -qq google-cloud-cli >/dev/null 2>&1 ||
        echo "  note: google-cloud-cli unavailable; backups stay on the local disk"
fi

# --- Journal database -------------------------------------------------------
chown -R "$APP_USER":"$APP_GROUP" "$PROJECT_DIR"
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
    install -o "$APP_USER" -g "$APP_GROUP" -m 600 "$tmp" "$ENV_FILE"
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

chmod +x "$PROJECT_DIR"/scripts/*.sh

# --- Schedule ---------------------------------------------------------------
# Same two jobs as launchd on the laptop, plus a nightly dump. Times are local,
# which is IST because of the timezone set above. The close waits until 18:00
# because the free price feed publishes the official close with a lag.
cat >/etc/cron.d/mutualfund <<CRON
# AI Mutual Fund Manager - weekdays, Asia/Kolkata. Managed by bootstrap_vm.sh.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
MAILTO=""

15 8 * * 1-5 $APP_USER cd $PROJECT_DIR && ./scripts/run_scheduled.sh cycle
0 18 * * 1-5 $APP_USER cd $PROJECT_DIR && ./scripts/run_scheduled.sh close
30 19 * * 1-5 $APP_USER cd $PROJECT_DIR && ./scripts/backup_journal.sh >> data/backup.log 2>&1
CRON
chmod 644 /etc/cron.d/mutualfund
echo "  cron installed: 08:15 cycle, 18:00 close, 19:30 backup (weekdays IST)"

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
# die with the next reboot. It binds 127.0.0.1 only (dashboard.host in
# settings.yaml), so the sole way to reach it is the SSH tunnel that
# deploy_gcp.sh dashboard opens - no firewall rule, nothing on the public IP.
cat >/etc/systemd/system/mutualfund-dashboard.service <<UNIT
[Unit]
Description=AI Mutual Fund Manager dashboard (localhost only)
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
    echo "  dashboard service up on 127.0.0.1:8000 (reach it via the SSH tunnel)" ||
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
  service:
    pipelines:
      fund:
        receivers: [fund_scheduled, fund_app, fund_backup]
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
