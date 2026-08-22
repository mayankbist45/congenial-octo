#!/bin/bash
# Deploy a Kronos validation VM on GCP.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZONE="${GCP_KRONOS_ZONE:-us-central1-a}"
INSTANCE="${GCP_KRONOS_INSTANCE:-congenial-octo-kronos}"
PROFILE="${GCP_KRONOS_PROFILE:-gpu}"
MACHINE_TYPE="${GCP_KRONOS_MACHINE_TYPE:-g2-standard-4}"
DISK_SIZE="${GCP_KRONOS_DISK_SIZE:-100GB}"
GPU_FAMILY="${GCP_KRONOS_GPU_FAMILY:-g2}"
GPU_TYPE="${GCP_KRONOS_GPU_TYPE:-nvidia-l4}"
GPU_COUNT="${GCP_KRONOS_GPU_COUNT:-1}"
REMOTE_DIR="/opt/fund"

if [ "$PROFILE" = "cpu" ] && [ -z "${GCP_KRONOS_MACHINE_TYPE:-}" ]; then
    MACHINE_TYPE="e2-standard-4"
fi

die() {
    echo "error: $*" >&2
    exit 1
}

require_gcloud() {
    command -v gcloud >/dev/null 2>&1 ||
        die "gcloud not found. Install the SDK first."
    PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
    [ -n "$PROJECT" ] && [ "$PROJECT" != "(unset)" ] ||
        die "no project set. Run: gcloud config set project YOUR_PROJECT_ID"
}

gc() { gcloud --project "$PROJECT" "$@"; }

ssh_vm() {
    gc compute ssh "$INSTANCE" --zone "$ZONE" --command "$1"
}

instance_exists() {
    gc compute instances describe "$INSTANCE" --zone "$ZONE" >/dev/null 2>&1
}

create() {
    if instance_exists; then
        echo "$INSTANCE already exists in $ZONE"
        return 0
    fi
    echo "Creating Kronos VM $INSTANCE in $ZONE (profile=$PROFILE)"
    local args=(
        compute instances create "$INSTANCE"
        --zone "$ZONE"
        --machine-type "$MACHINE_TYPE"
        --image-family debian-12
        --image-project debian-cloud
        --boot-disk-size "$DISK_SIZE"
        --boot-disk-type pd-balanced
        --metadata enable-oslogin=TRUE
        --scopes https://www.googleapis.com/auth/devstorage.read_write,https://www.googleapis.com/auth/logging.write,https://www.googleapis.com/auth/monitoring.write
        --tags fund,kronos
        --labels purpose=kronos-validation
    )
    if [ "$PROFILE" = "cpu" ]; then
        gc "${args[@]}" || die "instance creation failed"
    elif [ "$GPU_FAMILY" = "g2" ]; then
        gc "${args[@]}" \
            --maintenance-policy TERMINATE \
            --restart-on-failure || die "instance creation failed"
    else
        gc "${args[@]}" \
            --maintenance-policy TERMINATE \
            --restart-on-failure \
            --accelerator "type=$GPU_TYPE,count=$GPU_COUNT" || die "instance creation failed"
    fi
}

push() {
    instance_exists || die "$INSTANCE does not exist yet. Run: $0 deploy"
    local tarball
    tarball="$(mktemp "${TMPDIR:-/tmp}/kronos-src.XXXXXX.tar.gz")"
    (cd "$PROJECT_DIR" &&
        {
            git ls-files -z -- .
            git ls-files -z --others --exclude-standard -- .
        } | COPYFILE_DISABLE=1 tar -czf "$tarball" --null -T -) ||
        die "could not archive working tree"
    gc compute scp "$tarball" "$INSTANCE:~/kronos-src.tar.gz" --zone "$ZONE" || die "scp failed"
    rm -f "$tarball"

    ssh_vm "sudo mkdir -p '$REMOTE_DIR' &&
            sudo tar -xzf ~/kronos-src.tar.gz -C '$REMOTE_DIR' &&
            rm -f ~/kronos-src.tar.gz &&
            sudo chmod +x '$REMOTE_DIR'/scripts/*.sh"

    if [ -f "$PROJECT_DIR/.env" ]; then
        gc compute scp "$PROJECT_DIR/.env" "$INSTANCE:/tmp/fund.env" --zone "$ZONE" || die "scp of .env failed"
        ssh_vm "sudo mv /tmp/fund.env '$REMOTE_DIR/.env' && sudo chown \$(id -un):\$(id -gn) '$REMOTE_DIR/.env' && chmod 600 '$REMOTE_DIR/.env'"
    fi
}

bootstrap() {
    instance_exists || die "$INSTANCE does not exist yet. Run: $0 deploy"
    ssh_vm "sudo FUND_TIMEZONE=America/New_York bash '$REMOTE_DIR/scripts/bootstrap_kronos_vm.sh' --as-user \$(id -un)" ||
        die "bootstrap failed"
}

status() {
    instance_exists || die "$INSTANCE does not exist yet"
    gc compute instances describe "$INSTANCE" --zone "$ZONE" \
        --format='table(name,status,zone,machineType.basename(),networkInterfaces[0].accessConfigs[0].natIP)'
}

check_gpu() {
    instance_exists || die "$INSTANCE does not exist yet"
    if [ "$PROFILE" = "cpu" ]; then
        ssh_vm "echo 'cpu profile: no GPU attached'"
    else
        ssh_vm "nvidia-smi || true"
    fi
}

runtime_check() {
    instance_exists || die "$INSTANCE does not exist yet"
    ssh_vm "cd '$REMOTE_DIR' && .venv/bin/python scripts/kronos_runtime_check.py --persona calan_us"
}

run_backtest() {
    instance_exists || die "$INSTANCE does not exist yet"
    ssh_vm "cd '$REMOTE_DIR' && .venv/bin/python scripts/intraday_backtest.py --persona calan_us --start 2026-08-10 --end 2026-08-10 --max-symbols 5 --interval FIVE_MINUTE"
}

case "${1:-}" in
deploy)
    require_gcloud
    create
    push
    bootstrap
    ;;
create|push|bootstrap|status|check-gpu|runtime-check|run-backtest)
    require_gcloud
    "${1//-/_}"
    ;;
ssh)
    require_gcloud
    gc compute ssh "$INSTANCE" --zone "$ZONE"
    ;;
destroy)
    require_gcloud
    gc compute instances delete "$INSTANCE" --zone "$ZONE"
    ;;
*)
    cat <<EOF
usage: $0 deploy|create|push|bootstrap|status|check-gpu|runtime-check|run-backtest|ssh|destroy

env overrides:
  GCP_KRONOS_PROFILE=cpu|gpu
  GCP_KRONOS_MACHINE_TYPE=...
EOF
    exit 1
    ;;
esac
