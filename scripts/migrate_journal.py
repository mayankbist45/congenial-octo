"""Copy the fund journal from one database to another.

Written for the SQLite-to-PostgreSQL move, but the direction is not fixed: any
supported backend can be either end, which also makes it a way to take a
portable snapshot of a live journal.

Row ids are carried across unchanged, because proposals reference runs and
trades reference proposals, and the whole value of the journal is that those
links still resolve months later. On PostgreSQL the identity sequences are then
advanced past the imported ids so the next insert does not collide.

    python scripts/migrate_journal.py --to postgresql:///mutualfund
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.journal.db import (  # noqa: E402
    SEQUENCE_TABLES,
    TABLES,
    describe,
    session,
    table_counts,
    truncate_all,
)


def read_table(url: str, table: str) -> List[Dict[str, Any]]:
    with session(url) as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]


def write_table(url: str, table: str, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    columns = list(rows[0].keys())
    placeholders = ",".join("?" for _ in columns)
    statement = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    )
    with session(url) as conn:
        for row in rows:
            conn.execute(statement, tuple(row[column] for column in columns))
    return len(rows)


def fix_sequences(url: str) -> None:
    """Move identity sequences past the ids just imported."""
    with session(url) as conn:
        if conn.dialect != "postgres":
            return
        for table in SEQUENCE_TABLES:
            conn.execute(
                f"""
                SELECT setval(
                    pg_get_serial_sequence('{table}', 'id'),
                    COALESCE((SELECT MAX(id) FROM {table}), 1)
                )
                """
            )


def main() -> int:
    config = get_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from",
        dest="source",
        default=f"sqlite://{config.db_path}",
        help="source DSN (default: the configured SQLite file)",
    )
    parser.add_argument(
        "--to",
        dest="target",
        default=None,
        help="target DSN (default: whatever the current config points at)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="empty the target first; without this a non-empty target aborts",
    )
    args = parser.parse_args()

    source = args.source
    target = args.target or config.db_url

    if source == target:
        print(f"Source and target are the same database ({describe(source)})")
        return 1

    print(f"From: {describe(source)}")
    print(f"To:   {describe(target)}")

    try:
        before = table_counts(source)
    except Exception as exc:
        print(f"Cannot read the source: {exc}")
        return 1

    if not sum(before.values()):
        print("Source journal is empty; nothing to migrate.")
        return 0

    existing = table_counts(target)
    if sum(existing.values()):
        if not args.force:
            print(
                "Target already holds data "
                f"({sum(existing.values())} rows). Re-run with --force to "
                "replace it, or point --to at an empty database."
            )
            return 1
        print("Emptying the target first (--force)")
        truncate_all(target)

    print()
    for table in TABLES:
        rows = read_table(source, table)
        written = write_table(target, table, rows)
        print(f"  {table:<20} {written:>6} row(s)")

    fix_sequences(target)

    after = table_counts(target)
    mismatched = {t: (before[t], after[t]) for t in TABLES if before[t] != after[t]}
    print()
    if mismatched:
        for table, (expected, actual) in mismatched.items():
            print(f"MISMATCH {table}: expected {expected}, found {actual}")
        return 1

    print(f"Migration complete: {sum(after.values())} row(s) verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
