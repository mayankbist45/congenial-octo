#!/usr/bin/env python3
"""Label meta_decisions rows with outcomes (stub for nightly review)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_persona_config
from src.intraday.meta import meta_label_from_barrier
from src.journal.repository import JournalRepository
from src.logging_setup import bind_log_context, make_run_label, setup_logging


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report unlabeled meta gate decisions (labeling TBD per trade link)."
    )
    parser.add_argument("--persona", default="apprentice")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    config = get_persona_config(args.persona)
    setup_logging(
        level=str(config.get("logging.level", "INFO")),
        log_file=config.path("logging.file", "data/fund.log"),
        persona_log_dir=config.path("logging.persona_dir", "data/logs/personas"),
    )
    bind_log_context(
        persona_id=config.persona_id,
        run_label=make_run_label("label_meta_decisions", config.persona_id),
        command="label_meta_decisions",
        market_id=config.market_id,
    )
    repo = JournalRepository(config)
    rows = repo.meta_decisions(limit=args.limit, unlabeled_only=True)
    if not rows:
        print(f"No unlabeled meta decisions for persona {args.persona!r}.")
        return 0

    print(f"Unlabeled meta decisions: {len(rows)}")
    for row in rows[:20]:
        features = row.get("features") or {}
        print(
            f"  {row['decision_ts']} {row['symbol']} {row['gate_decision']}"
            f" ({row.get('gate_method')}) strategy={row.get('strategy')}"
            f" style={features.get('fundamental_style', '—')}"
        )
    if len(rows) > 20:
        print(f"  ... and {len(rows) - 20} more")

    print(
        "\nOutcome labeling will attach triple-barrier results once trades are "
        "linked to decision_id. meta_label = meta_label_from_barrier(outcome_label)."
    )
    _ = meta_label_from_barrier  # referenced for future label job
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
