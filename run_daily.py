#!/usr/bin/env python3
"""Entry point for the fund's daily cycle.

Typical day, in three commands:

    python run_daily.py analyse       # before the open: research and propose
    python run_daily.py dashboard     # review and approve at localhost:8000
    python run_daily.py execute       # place what you approved

Or as one sequence that pauses for your approvals in between:

    python run_daily.py cycle --wait 90

With execution.auto_approve on, that same command runs start to finish with
nobody watching: the fund approves its own proposals and places them. That is
restricted to paper trading — set DRY_RUN=false and the fund refuses to
self-approve, reverting to waiting for you. Use --require-approval to insist on a
human for a single run, or --auto-approve to opt in for one.

After the close, record the day's NAV so performance is measured on closing
prices rather than the pre-market marks:

    python run_daily.py close
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from typing import List, Optional

from src.config import (
    DEFAULT_PERSONA,
    enabled_personas,
    get_config,
    get_persona_config,
    persona_description,
)
from src.logging_setup import setup_logging


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"Invalid date {value!r}; expected YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_daily.py",
        description="AI mutual fund manager for Indian equities (Nifty 500)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="analyse",
        choices=["analyse", "execute", "cycle", "close", "learn", "dashboard", "status"],
        help="what to run (default: analyse)",
    )
    parser.add_argument(
        "--as-of",
        metavar="YYYY-MM-DD",
        help="treat this date as today, for back-dated or catch-up runs",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=90,
        metavar="MINUTES",
        help="how long the cycle command waits for your approvals (default: 90)",
    )
    parser.add_argument(
        "--skip-news",
        action="store_true",
        help="skip the news pipeline; useful when feeds are down or for a fast rerun",
    )
    parser.add_argument(
        "--refresh-fundamentals",
        action="store_true",
        help="ignore the fundamentals cache and re-scrape everything (slow)",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        metavar="N",
        help="research only the N most liquid names, for a faster run",
    )
    parser.add_argument(
        "--simulated-broker",
        action="store_true",
        help="force the simulated broker even if Angel One credentials exist",
    )
    approval = parser.add_mutually_exclusive_group()
    approval.add_argument(
        "--auto-approve",
        dest="auto_approve",
        action="store_true",
        default=None,
        help="approve the fund's own proposals and trade unattended "
        "(paper trading only; refused when DRY_RUN=false)",
    )
    approval.add_argument(
        "--require-approval",
        dest="auto_approve",
        action="store_false",
        help="wait for your decision in the dashboard, overriding auto mode",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log at DEBUG level",
    )
    parser.add_argument(
        "--persona",
        metavar="ID",
        help="run only this persona (default: all enabled personas)",
    )
    return parser



def _report_status() -> None:
    from src.dashboard.service import DashboardService, money, pct

    service = DashboardService()
    overview = service.overview()
    performance = service.performance()

    if not overview["has_data"]:
        print("The fund has not run yet. Start with: python run_daily.py analyse")
        return

    run = overview["run"]
    print(f"{service.fund_name}")
    print(f"  last run        {run['run_date']} ({run['status']})")
    print(f"  fund value      {money(overview['total_value'])}")
    print(
        f"  cash            {money(overview['cash'])} "
        f"({pct(overview['cash_pct'])})"
    )
    print(f"  positions       {overview['position_count']}")
    print(f"  total return    {pct(performance['total_return_pct'], 2, True)}")
    print(
        f"  vs {service.benchmark_label:<12} "
        f"{pct(performance['excess_return_pct'], 2, True)}"
    )
    print(f"  turnover used   {pct(run['turnover_used_pct'])}")
    print(f"  awaiting you    {overview['pending_count']} proposal(s)")
    if service.is_dry_run:
        print("  mode            DRY RUN (orders are simulated)")
    else:
        print("  mode            LIVE (approved orders reach the broker)")


def _auto_mode(manager, args) -> bool:
    """Whether this run decides its own proposals; the flag beats the config."""
    if args.auto_approve is None:
        return manager.auto_approval_enabled
    return bool(args.auto_approve)


def _clear_pending(manager, args) -> None:
    """Get the pending queue decided, by the fund itself or by a human."""
    from src.orchestrator import AutoApprovalRefused

    if _auto_mode(manager, args):
        try:
            manager.auto_approve()
            return
        except AutoApprovalRefused as exc:
            print(
                f"\nAuto mode is on but {exc}.\n"
                f"Falling back to waiting for your approval in the dashboard.",
                file=sys.stderr,
            )

    manager.wait_for_approval(timeout_minutes=args.wait)


def _resolve_personas(args) -> List[str]:
    """Which personas to run: the one from --persona, or all enabled ones."""
    if args.persona:
        return [args.persona]
    return enabled_personas()


def _run_learning_for(config, as_of: Optional[date] = None) -> None:
    """Refresh advisory learning without affecting trading flows."""
    from src.learning import LearningEvaluator

    try:
        summary = LearningEvaluator(config).run(as_of=as_of)
        print(f"  {summary.describe()}")
    except Exception as exc:
        print(f"  learning skipped: {exc}", file=sys.stderr)


def _run_persona(persona_id: str, args, base_config) -> int:
    """Run a single command for one persona. Returns the exit code."""
    from src.orchestrator import FundManager

    try:
        pcfg = get_persona_config(persona_id)
    except ValueError as exc:
        print(f"Persona error: {exc}", file=sys.stderr)
        return 1

    print(f"\n{'─' * 60}")
    print(f"Persona: {persona_id} — {persona_description(persona_id)}")
    print(f"{'─' * 60}")

    _print_config_problems_for(pcfg)

    as_of = _parse_date(args.as_of)

    if args.command == "learn":
        _run_learning_for(pcfg, as_of=as_of)
        return 0

    manager = FundManager(config=pcfg, force_simulated=args.simulated_broker)

    try:
        if args.command == "analyse":
            result = manager.analyse(
                as_of=as_of,
                skip_news=args.skip_news,
                refresh_fundamentals=args.refresh_fundamentals,
                max_candidates=args.max_candidates,
            )
            if result.proposal_count and _auto_mode(manager, args):
                print(
                    f"\n[{persona_id}] {result.proposal_count} proposal(s) pending. "
                    f"Auto mode is on, so they are approved and placed by:\n"
                    f"  python run_daily.py execute --persona {persona_id}"
                )
            elif result.proposal_count:
                host = pcfg.get("dashboard.host", "127.0.0.1")
                port = pcfg.get("dashboard.port", 8000)
                print(
                    f"\n[{persona_id}] {result.proposal_count} proposal(s) await "
                    f"your approval.\n"
                    f"  python run_daily.py dashboard   ->  http://{host}:{port}/proposals"
                )
            else:
                print(f"\n[{persona_id}] No trades proposed today.")
            return 0

        if args.command == "execute":
            if _auto_mode(manager, args) and manager.pending_count():
                _clear_pending(manager, args)
            summary = manager.execute_approved()
            print(f"\n[{persona_id}] {summary.describe()}")
            return 0 if not summary.failed else 1

        if args.command == "cycle":
            result = manager.analyse(
                as_of=as_of,
                skip_news=args.skip_news,
                refresh_fundamentals=args.refresh_fundamentals,
                max_candidates=args.max_candidates,
            )
            if not result.proposal_count:
                print(f"\n[{persona_id}] No trades proposed today.")
                return 0

            _clear_pending(manager, args)
            summary = manager.execute_approved(
                technicals=result.technicals, fundamentals=result.fundamentals
            )
            print(f"\n[{persona_id}] {summary.describe()}")
            return 0

        if args.command == "close":
            manager.close_of_day(as_of=as_of)
            _run_learning_for(pcfg, as_of=as_of)
            return 0

    except KeyboardInterrupt:
        print("\nInterrupted. Nothing further was sent to the broker.", file=sys.stderr)
        return 130
    finally:
        manager.disconnect()

    return 0


def _print_config_problems_for(config) -> None:
    problems = config.validate()
    if problems:
        print(f"Configuration problems for persona {config.persona_id!r}:",
              file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        raise SystemExit(
            "Fix config/settings.yaml before running. See scripts/setup_check.py."
        )


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)

    config = get_config()
    setup_logging(
        level="DEBUG" if args.verbose else str(config.get("logging.level", "INFO")),
        log_file=config.path("logging.file", "data/fund.log"),
    )

    if args.command == "status":
        _report_status()
        return 0

    if args.command == "dashboard":
        from src.dashboard.app import serve

        serve()
        return 0

    personas = _resolve_personas(args)
    worst_exit = 0
    for persona_id in personas:
        exit_code = _run_persona(persona_id, args, config)
        worst_exit = max(worst_exit, exit_code)
        if exit_code == 130:
            break

    return worst_exit


if __name__ == "__main__":
    raise SystemExit(main())
