#!/bin/bash
# Dump the journal, keep a fortnight locally, and copy the dump off the machine.
#
# The journal is the only data here that cannot be rebuilt: prices, fundamentals
# and news all come back on the next run, but the decision history, fills and NAV
# series are the fund's record of itself. So this runs nightly from cron and the
# copy in object storage is the one that matters - a dump sitting on the same
# disk as the database it came from is not a backup.
#
#   ./scripts/backup_journal.sh                       dump, prune, upload
#   FUND_BACKUP_BUCKET=my-bucket ./scripts/backup_journal.sh
#
# Restore into an empty database with:
#   pg_restore --clean --if-exists --dbname="$DATABASE_URL" journal-YYYY-MM-DD.dump

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
DEST_DIR="$PROJECT_DIR/data/backups"
KEEP_DAYS="${FUND_BACKUP_KEEP_DAYS:-14}"

# Homebrew's client on the laptop, Debian's in /usr/bin on the VM.
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Read the DSN the same way the application does: environment first, then .env.
# Nothing is sourced, because .env is data, not shell.
from_env_file() {
    [ -f "$ENV_FILE" ] || return 0
    grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d "\"'"
}

DSN="${DATABASE_URL:-$(from_env_file DATABASE_URL)}"
[ -n "$DSN" ] || DSN="postgresql:///mutualfund"
BUCKET="${FUND_BACKUP_BUCKET:-$(from_env_file FUND_BACKUP_BUCKET)}"

command -v pg_dump >/dev/null 2>&1 || {
    echo "error: pg_dump not on PATH" >&2
    exit 1
}

mkdir -p "$DEST_DIR"
STAMP="$(date '+%Y-%m-%d')"
ARCHIVE="$DEST_DIR/journal-$STAMP.dump"

echo "$(date '+%Y-%m-%d %H:%M:%S %Z')  dumping journal"
# Custom format: compressed, and pg_restore can be selective about tables.
if ! pg_dump --dbname="$DSN" --format=custom --file="$ARCHIVE"; then
    # Remove the stub pg_dump leaves behind, so a failed night cannot masquerade
    # as a backup in the directory listing.
    rm -f "$ARCHIVE"
    echo "  FAILED: pg_dump could not read the journal" >&2
    exit 1
fi
echo "  wrote $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"

if [ -n "$BUCKET" ]; then
    if command -v gcloud >/dev/null 2>&1; then
        if gcloud storage cp "$ARCHIVE" "gs://$BUCKET/journal/" >/dev/null 2>&1; then
            echo "  copied to gs://$BUCKET/journal/"
        else
            echo "  warning: upload to gs://$BUCKET failed; dump kept locally" >&2
        fi
    else
        echo "  warning: gcloud not installed; dump kept locally only" >&2
    fi
fi

# Local pruning only. Whatever retention the bucket has is set on the bucket, so
# a laptop mistake here cannot delete the off-machine history.
find "$DEST_DIR" -name 'journal-*.dump' -type f -mtime +"$KEEP_DAYS" -delete 2>/dev/null
echo "  $(find "$DEST_DIR" -name 'journal-*.dump' | wc -l | tr -d ' ') dump(s) kept locally"
