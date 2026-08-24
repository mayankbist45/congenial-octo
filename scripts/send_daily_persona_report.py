#!/usr/bin/env python3
"""Build the end-of-day persona digest and push it to WhatsApp.

Examples:
  python scripts/send_daily_persona_report.py --market india_equity
  python scripts/send_daily_persona_report.py --market us_equity --dry-run
  python scripts/send_daily_persona_report.py --all --date 2026-08-07
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.logging_setup import setup_logging
from src.notify.persona_report import (
    default_session_day,
    format_combined_report,
    format_market_report,
)
from src.notify.whatsapp import load_whatsapp_config, send_whatsapp_message


def main() -> int:
    parser = argparse.ArgumentParser(description="Send EOD persona report to WhatsApp")
    parser.add_argument("--market", help="Market id (india_equity / us_equity)")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include every configured market in one message",
    )
    parser.add_argument(
        "--date",
        help="Session date YYYY-MM-DD (default: exchange-local today)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print/preview only; do not call Twilio",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print report to stdout and exit (no WhatsApp)",
    )
    args = parser.parse_args()

    setup_logging(level="INFO", log_file=PROJECT_DIR / "data" / "fund.log")

    if not args.all and not args.market:
        parser.error("pass --market <id> or --all")

    if args.date:
        day = date.fromisoformat(args.date)
    elif args.market:
        day = default_session_day(args.market)
    else:
        day = date.today()

    if args.all:
        body = format_combined_report(day)
    else:
        body = format_market_report(args.market, day)

    print(body)
    if args.print_only:
        return 0

    cfg = load_whatsapp_config()
    result = send_whatsapp_message(body, cfg, dry_run=args.dry_run)
    if result.get("skipped"):
        print("WhatsApp skipped: not configured", file=sys.stderr)
        return 2
    if not result.get("ok"):
        print(f"WhatsApp send failed: {result}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"dry-run ok ({result.get('chars')} chars to {len(result.get('recipients') or [])} recipient(s))")
    else:
        delivered = sum(1 for item in result.get("results", []) if item.get("ok"))
        print(f"WhatsApp digest sent to {delivered} recipient(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
