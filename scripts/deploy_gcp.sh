#!/bin/bash
# Run the fund on a single Google Cloud VM, driven from this laptop.
#
# The cloud copy is deliberately the same shape as the local one: one machine
# with PostgreSQL on localhost, the repo on a persistent disk, and cron where
# launchd was. Nothing here is Google-specific except the provisioning, so the
# fund can be lifted to any VM later with only bootstrap_vm.sh.
#
#   ./scripts/deploy_gcp.sh deploy      create the VM, push the fund, bootstrap it
#   ./scripts/deploy_gcp.sh push        send local code, .env and caches again
#   ./scripts/deploy_gcp.sh status      run run_daily.py status up there
#   ./scripts/deploy_gcp.sh logs        follow data/scheduled.log on the VM
#   ./scripts/deploy_gcp.sh cloudlogs   read what the Ops Agent shipped to Cloud Logging
#   ./scripts/deploy_gcp.sh dashboard   start the dashboard and print its public URL
#   ./scripts/deploy_gcp.sh temporal    start Temporal and print the SSH tunnel command
#   ./scripts/deploy_gcp.sh ssh         shell on the VM
#   ./scripts/deploy_gcp.sh snapshots   nightly disk snapshots, 7 kept
#   ./scripts/deploy_gcp.sh stop|start  pause billing for compute, keep the disk
#   ./scripts/deploy_gcp.sh destroy     delete the VM (the disk goes with it)
#
# Overridable with environment variables:
#   GCP_PROJECT GCP_ZONE GCP_INSTANCE GCP_MACHINE_TYPE GCP_DISK_SIZE
#   GCP_BACKUP_BUCKET   a GCS bucket for nightly pg_dump copies
#
# The dashboard can be exposed publicly on tcp:8000. This script opens the
# firewall rule on the VM tag and prints the instance's external URL.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ZONE="${GCP_ZONE:-asia-south1-a}"
INSTANCE="${GCP_INSTANCE:-congenial-octo-fund-poc}"
MACHINE_TYPE="${GCP_MACHINE_TYPE:-e2-medium}"
DISK_SIZE="${GCP_DISK_SIZE:-30GB}"
BACKUP_BUCKET="${GCP_BACKUP_BUCKET:-}"
REMOTE_DIR="/opt/fund"
# Scraping Screener and Yahoo from a datacentre IP is already the weak link, so
# the VM keeps an ordinary ephemeral public address rather than routing through
# Cloud NAT, which would put every run behind one shared, well-known IP.

if [ -n "${CI:-}" ] || [ "${FUND_GCLOUD_QUIET:-0}" = "1" ]; then
    export CLOUDSDK_CORE_DISABLE_PROMPTS=1
fi

die() {
    echo "error: $*" >&2
    exit 1
}

require_gcloud() {
    command -v gcloud >/dev/null 2>&1 ||
        die "gcloud not found. Install the SDK: https://cloud.google.com/sdk/docs/install"
    PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
    [ -n "$PROJECT" ] && [ "$PROJECT" != "(unset)" ] ||
        die "no project set. Run: gcloud config set project YOUR_PROJECT_ID"
}

gc() { gcloud --project "$PROJECT" "$@"; }

gc_quiet() {
    if [ -n "${CI:-}" ] || [ "${FUND_GCLOUD_QUIET:-0}" = "1" ]; then
        gc --quiet "$@"
    else
        gc "$@"
    fi
}

public_ip() {
    gc compute instances describe "$INSTANCE" --zone "$ZONE" \
        --format='value(networkInterfaces[0].accessConfigs[0].natIP)'
}

ensure_dashboard_firewall() {
    gc compute firewall-rules create mutualfund-dashboard-public \
        --allow tcp:8000 \
        --target-tags fund \
        --source-ranges 0.0.0.0/0 >/dev/null 2>&1 ||
        true
}

_apply_shared_permissions() {
    ssh_vm "sudo groupadd fund 2>/dev/null || true &&
            sudo chown -R \$(id -un):fund '$REMOTE_DIR' &&
            sudo find '$REMOTE_DIR' -type d -exec chmod 2775 {} + &&
            sudo find '$REMOTE_DIR' -type f ! -path '$REMOTE_DIR/.env' ! -path '$REMOTE_DIR/.venv/*' -exec chmod 664 {} + &&
            sudo find '$REMOTE_DIR/scripts' -type f -name '*.sh' -exec chmod 775 {} + 2>/dev/null || true &&
            if [ -d '$REMOTE_DIR/.venv/bin' ]; then sudo find '$REMOTE_DIR/.venv/bin' -type f -exec chmod 775 {} +; fi &&
            sudo chmod -R g+rw '$REMOTE_DIR/data' '$REMOTE_DIR/logs' 2>/dev/null || true &&
            if [ -f '$REMOTE_DIR/.env' ]; then
                sudo chgrp fund '$REMOTE_DIR/.env' && chmod 640 '$REMOTE_DIR/.env';
            fi"
}

ssh_vm() {
    gc_quiet compute ssh "$INSTANCE" --zone "$ZONE" --command "$1"
}

instance_exists() {
    gc_quiet compute instances describe "$INSTANCE" --zone "$ZONE" >/dev/null 2>&1
}

create() {
    if instance_exists; then
        echo "  $INSTANCE already exists in $ZONE"
        return 0
    fi

    echo "Creating $MACHINE_TYPE in $ZONE ($DISK_SIZE persistent disk)"
    gc_quiet compute instances create "$INSTANCE" \
        --zone "$ZONE" \
        --machine-type "$MACHINE_TYPE" \
        --image-family debian-12 \
        --image-project debian-cloud \
        --boot-disk-size "$DISK_SIZE" \
        --boot-disk-type pd-balanced \
        --boot-disk-device-name "$INSTANCE" \
        --metadata enable-oslogin=TRUE \
        --scopes https://www.googleapis.com/auth/devstorage.read_write,https://www.googleapis.com/auth/logging.write,https://www.googleapis.com/auth/monitoring.write \
        --tags fund \
        --labels purpose=congenial-octo ||
        die "instance creation failed"

    echo "  waiting for SSH"
    for _ in $(seq 1 20); do
        ssh_vm true >/dev/null 2>&1 && break
        sleep 6
    done
    ensure_dashboard_firewall
}

push() {
    instance_exists || die "$INSTANCE does not exist yet. Run: $0 deploy"

    local tarball
    local tmpbase
    tmpbase="$(mktemp "${TMPDIR:-/tmp}/fund-src.XXXXXX")" ||
        die "could not allocate a temporary archive path"
    tarball="${tmpbase}.tar.gz"
    rm -f "$tmpbase"
    # Everything git would keep: tracked files plus new untracked ones that are
    # not gitignored, so a brand-new script reaches the VM before it is ever
    # committed. Taken from the working tree, so an uncommitted settings.yaml
    # tweak goes too. .env, caches and backups are gitignored and so excluded
    # here; .env is copied separately with tighter permissions. Logs and the old
    # SQLite sidecars, logs, and live intraday summaries are excluded: the VM
    # keeps its own run history; overwriting *_latest.json wipes live paper UI.
    local excludes=(
        ':!:data/*.log'
        ':!:data/fund.db*'
        ':!:data/intraday/*_latest.json'
        ':!:data/intraday/*_state.json'
    )
    (cd "$PROJECT_DIR" &&
        {
            git ls-files -z -- . "${excludes[@]}"
            git ls-files -z --others --exclude-standard -- . "${excludes[@]}"
        } | COPYFILE_DISABLE=1 tar -czf "$tarball" --null -T -) ||
        die "could not archive the working tree"

    echo "Pushing code to $INSTANCE:$REMOTE_DIR"
    gc_quiet compute scp "$tarball" "$INSTANCE:~/fund-src.tar.gz" --zone "$ZONE" ||
        die "scp of the source archive failed"
    rm -f "$tarball"

    ssh_vm "sudo mkdir -p '$REMOTE_DIR' &&
            sudo tar -xzf ~/fund-src.tar.gz -C '$REMOTE_DIR' &&
            rm -f ~/fund-src.tar.gz &&
            sudo find '$REMOTE_DIR' -name '._*' -delete &&
            sudo chmod +x '$REMOTE_DIR'/scripts/*.sh &&
            sudo mkdir -p '$REMOTE_DIR/data'" || die "unpacking on the VM failed"

    _apply_shared_permissions

    if [ -f "$PROJECT_DIR/.env" ]; then
        echo "Pushing .env"
        gc_quiet compute scp "$PROJECT_DIR/.env" "$INSTANCE:/tmp/fund.env" --zone "$ZONE" ||
            die "scp of .env failed"
        # DATABASE_URL is dropped from the laptop copy (Homebrew DSN). Preserve
        # the VM's existing DATABASE_URL and TEMPORAL_ADDRESS (Temporal points at
        # the local loopback on the VM, not at the laptop's compose network).
        ssh_vm "OLD_DB=\$(grep '^DATABASE_URL=' '$REMOTE_DIR/.env' 2>/dev/null || true) &&
                OLD_TEMPORAL=\$(grep '^TEMPORAL_ADDRESS=' '$REMOTE_DIR/.env' 2>/dev/null || true) &&
                grep -vE '^(DATABASE_URL|TEMPORAL_ADDRESS)=' /tmp/fund.env > '$REMOTE_DIR/.env' &&
                if [ -n \"\$OLD_DB\" ]; then echo \"\$OLD_DB\" >> '$REMOTE_DIR/.env'; fi &&
                if [ -n \"\$OLD_TEMPORAL\" ]; then echo \"\$OLD_TEMPORAL\" >> '$REMOTE_DIR/.env'; fi &&
                rm -f /tmp/fund.env &&
                sudo chgrp fund '$REMOTE_DIR/.env' &&
                chmod 640 '$REMOTE_DIR/.env'" ||
            die "installing .env failed"
        # Confirm we did not install the laptop's socket DSN on the VM.
        ssh_vm "grep '^DATABASE_URL=' '$REMOTE_DIR/.env' | grep -q '127.0.0.1' &&
                echo '  DATABASE_URL preserved (VM localhost PostgreSQL)' ||
                echo '  WARNING: DATABASE_URL missing or not 127.0.0.1 — run bootstrap_vm.sh on the VM'" ||
            true
    else
        echo "  no local .env found; create $REMOTE_DIR/.env on the VM by hand"
    fi

    _apply_shared_permissions

    # Caches are gitignored but worth carrying: a cold fundamentals cache means
    # ~500 Screener requests at one second apart on the first run.
    if [ -d "$PROJECT_DIR/data/cache" ]; then
        echo "Pushing price and fundamentals caches"
        gc_quiet compute scp --recurse "$PROJECT_DIR/data/cache" \
            "$INSTANCE:$REMOTE_DIR/data/" --zone "$ZONE" ||
            echo "  warning: cache copy failed; the first run will rebuild it" >&2
    fi

    echo "Restarting dashboard service"
    ssh_vm "sudo systemctl restart mutualfund-dashboard 2>/dev/null || true
            sudo systemctl restart nivelo-temporal 2>/dev/null || true
            if sudo '$REMOTE_DIR/scripts/temporal_compose.sh' --env-file /etc/nivelo/temporal.env -f '$REMOTE_DIR/deploy/temporal-compose.yml' --profile agent ps --status running --format '{{.Name}}' agent-worker 2>/dev/null | grep -q .; then
                sudo '$REMOTE_DIR/scripts/temporal_compose.sh' --env-file /etc/nivelo/temporal.env -f '$REMOTE_DIR/deploy/temporal-compose.yml' --profile agent up -d --build agent-worker
            fi"
}

bootstrap() {
    instance_exists || die "$INSTANCE does not exist yet. Run: $0 deploy"
    echo "Bootstrapping $INSTANCE (packages, PostgreSQL, venv, cron)"
    ssh_vm "sudo FUND_BACKUP_BUCKET='$BACKUP_BUCKET' bash '$REMOTE_DIR/scripts/bootstrap_vm.sh' --as-user \$(id -un)" ||
        die "bootstrap failed"
}

make_bucket() {
    [ -n "$BACKUP_BUCKET" ] || die "set GCP_BACKUP_BUCKET first"
    if gc_quiet storage buckets describe "gs://$BACKUP_BUCKET" >/dev/null 2>&1; then
        echo "  gs://$BACKUP_BUCKET already exists"
        return 0
    fi
    gc_quiet storage buckets create "gs://$BACKUP_BUCKET" \
        --location asia-south1 \
        --uniform-bucket-level-access ||
        die "bucket creation failed"
    echo "  created gs://$BACKUP_BUCKET"
}

snapshots() {
    local policy="${INSTANCE}-nightly"
    if ! gc_quiet compute resource-policies describe "$policy" --region "${ZONE%-*}" >/dev/null 2>&1; then
        gc_quiet compute resource-policies create snapshot-schedule "$policy" \
            --region "${ZONE%-*}" \
            --max-retention-days 7 \
            --daily-schedule \
            --start-time 21:00 \
            --on-source-disk-delete keep-auto-snapshots ||
            die "snapshot policy creation failed"
    fi
    gc_quiet compute disks add-resource-policies "$INSTANCE" \
        --zone "$ZONE" --resource-policies "$policy" 2>/dev/null ||
        echo "  policy already attached"
    echo "  nightly snapshots at 21:00 UTC, 7 kept"
}

dashboard() {
    instance_exists || die "$INSTANCE does not exist yet"
    echo "Ensuring the dashboard service is up on $INSTANCE"
    ensure_dashboard_firewall
    # The dashboard is a systemd service (see bootstrap_vm.sh), so starting it
    # returns at once instead of holding the SSH session open the way a
    # backgrounded python process would.
    ssh_vm "sudo systemctl start mutualfund-dashboard 2>/dev/null || true
            for _ in \$(seq 1 15); do
                (exec 3<>/dev/tcp/127.0.0.1/8000) 2>/dev/null && break
                sleep 1
            done
            if (exec 3<>/dev/tcp/127.0.0.1/8000) 2>/dev/null; then
                echo '  dashboard service is listening on port 8000'
            else
                echo '  dashboard did not come up; recent log:' >&2
                sudo journalctl -u mutualfund-dashboard -n 20 --no-pager 2>/dev/null
                tail -20 '$REMOTE_DIR/data/dashboard.log' 2>/dev/null
                exit 1
            fi" || die "could not start the dashboard"
    echo
    local ip
    ip="$(public_ip)"
    [ -n "$ip" ] || die "could not determine external IP"
    echo "Dashboard URL: http://$ip:8000"
}

temporal() {
    instance_exists || die "$INSTANCE does not exist yet"
    ssh_vm "sudo systemctl start nivelo-temporal &&
            (exec 3<>/dev/tcp/127.0.0.1/8233) 2>/dev/null" ||
        die "Temporal UI is not available; check journalctl -u nivelo-temporal"
    echo "Temporal UI: http://127.0.0.1:8233"
    echo "Run separately: gcloud compute ssh $INSTANCE --zone $ZONE -- -L 8233:127.0.0.1:8233"
}

case "${1:-help}" in
deploy)
    require_gcloud
    create
    push
    bootstrap
    echo
    echo "Deployed. The VM now runs on IST:"
    echo "  08:15  long_cycle       weekday long-horizon paper execution"
    echo "  09:00  weekly_research  Saturday candidate-pack refresh"
    echo "  18:00  close    mark the book and record NAV"
    echo "  19:30  backup   pg_dump of the journal"
    echo
    echo "Check it:  $0 status"
    echo "Watch it:  $0 logs"
    ;;

create)
    require_gcloud
    create
    ;;

push)
    require_gcloud
    push
    ;;

bootstrap)
    require_gcloud
    bootstrap
    ;;

bucket)
    require_gcloud
    make_bucket
    ;;

snapshots)
    require_gcloud
    snapshots
    ;;

status)
    require_gcloud
    ssh_vm "cd '$REMOTE_DIR' && .venv/bin/python run_daily.py status"
    ;;

check)
    require_gcloud
    ssh_vm "cd '$REMOTE_DIR' && .venv/bin/python scripts/setup_check.py"
    ;;

logs)
    require_gcloud
    ssh_vm "tail -f -n 60 '$REMOTE_DIR/data/scheduled.log'"
    ;;

cloudlogs)
    require_gcloud
    # What the Ops Agent shipped, so it is readable even when the VM is down. The
    # agent files each log line under jsonPayload.message; textPayload is only
    # set for entries written some other way, so both are printed.
    gc_quiet logging read \
        'logName:"logs/fund_scheduled" OR logName:"logs/fund_app" OR logName:"logs/fund_backup"' \
        --limit "${2:-100}" --freshness "${3:-2d}" --order asc \
        --format 'value(timestamp.date("%Y-%m-%d %H:%M:%S"), jsonPayload.message, textPayload)'
    ;;

daily_monitor | long_cycle | weekly_research | cycle | close | analyse)
    require_gcloud
    ssh_vm "cd '$REMOTE_DIR' && ./scripts/run_scheduled.sh $1 && tail -30 data/scheduled.log"
    ;;

backup)
    require_gcloud
    ssh_vm "cd '$REMOTE_DIR' && ./scripts/backup_journal.sh"
    ;;

dashboard)
    require_gcloud
    dashboard
    ;;

temporal)
    require_gcloud
    temporal
    ;;

ssh)
    require_gcloud
    gc_quiet compute ssh "$INSTANCE" --zone "$ZONE"
    ;;

stop)
    require_gcloud
    gc_quiet compute instances stop "$INSTANCE" --zone "$ZONE"
    echo "Stopped. The disk still costs a few dollars a month; the vCPU does not."
    ;;

start)
    require_gcloud
    gc_quiet compute instances start "$INSTANCE" --zone "$ZONE"
    ;;

destroy)
    require_gcloud
    if [ -n "${CI:-}" ] || [ "${FUND_CONFIRM_DESTROY:-0}" = "1" ]; then
        reply="y"
    else
        read -r -p "Delete $INSTANCE and its disk in $ZONE? [y/N] " reply
    fi
    [ "$reply" = "y" ] || exit 0
    gc_quiet compute instances delete "$INSTANCE" --zone "$ZONE"
    echo "Gone. Restore from the pg_dump in gs://$BACKUP_BUCKET if you rebuild."
    ;;

help | *)
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
    ;;
esac
