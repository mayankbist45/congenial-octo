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

import os
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
        choices=[
            "analyse",
            "daily_monitor",
            "long_cycle",
            "weekly_research",
            "execute",
            "cycle",
            "close",
            "publish_dashboard",
            "learn",
            "dashboard",
            "status",
            "watch",
        ],
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
        "--refresh-ipo-research",
        action="store_true",
        help="replace only today's published IPO research data",
    )
    parser.add_argument(
        "--refresh-portfolio",
        action="store_true",
        help="replace today's published paper holdings with the current journal state",
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
    parser.add_argument(
        "--market",
        metavar="ID",
        help="run only personas belonging to this market (e.g. india_equity, us_equity)",
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


def _resolve_personas(args, base_config=None) -> List[str]:
    """Which personas to run: the one from --persona, or all enabled ones."""
    base = base_config or get_config()
    if args.persona:
        personas = [args.persona]
    else:
        personas = enabled_personas()
        from src.workspace.user_book_personas import active_userbook_persona_ids

        personas = list(dict.fromkeys(personas + active_userbook_persona_ids(base)))
    if getattr(args, "market", None):
        market_id = args.market.strip()
        from src.config import get_persona_config

        filtered = []
        for pid in personas:
            try:
                pcfg = get_persona_config(pid, base=base)
            except ValueError:
                continue
            if pcfg.market_id == market_id:
                filtered.append(pid)
        return filtered
    return personas


def _scheduled_long_personas(args, base_config) -> tuple[List[str], List[str]]:
    """Return unattended-safe long-horizon personas and configuration failures."""
    personas = _resolve_personas(args, base_config)
    eligible: List[str] = []
    problems: List[str] = []
    for persona_id in personas:
        try:
            config = get_persona_config(persona_id, base=base_config)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if config.get("intraday.enabled", False):
            continue
        if not bool(config.get("execution.auto_approve", False)):
            problems.append(f"{persona_id}: execution.auto_approve must be true")
            continue
        if not config.secrets.dry_run:
            problems.append(f"{persona_id}: DRY_RUN must be true for unattended execution")
            continue
        eligible.append(persona_id)
    if os.environ.get("FUND_SCHEDULED_RUN") == "1":
        from src.scheduler_controls import SchedulerControls

        controls = SchedulerControls(base_config)
        eligible = [
            persona_id for persona_id in eligible
            if controls.enabled(f"{'india' if get_persona_config(persona_id, base=base_config).market_id == 'india_equity' else 'us'}_long_cycle")
        ]
    return eligible, problems


def _run_learning_for(config, as_of: Optional[date] = None) -> None:
    """Refresh advisory learning without affecting trading flows."""
    from src.learning import LearningEvaluator

    try:
        summary = LearningEvaluator(config).run(as_of=as_of)
        print(f"  {summary.describe()}")
    except Exception as exc:
        print(f"  learning skipped: {exc}", file=sys.stderr)


def _run_intraday_persona(persona_id: str, args, config) -> int:
    """Run an intraday paper/backtest persona inside the normal daily loop."""
    from src.dashboard.service import DashboardService, money, pct

    if args.command == "status":
        summary = DashboardService(config).intraday_summary()
        if not summary:
            print(
                f"\n[{persona_id}] No intraday paper result saved yet. "
                f"Start with: python run_daily.py analyse --persona {persona_id}"
            )
            return 0
        print(f"\n[{persona_id}] Calan intraday paper/backtest")
        print(f"  period        {summary['start_date']} to {summary['end_date']}")
        print(f"  net P&L       {money(summary['total_net_pnl'])}")
        print(f"  return        {pct(summary['total_return_pct'], 2, True)}")
        print(f"  trades        {summary['total_trades']}")
        print(f"  win rate      {pct(summary['win_rate_pct'])}")
        print(f"  expectancy    {summary['expectancy_r']:+.3f}R")
        return 0

    from src.broker.factory import build_market_data_broker
    from src.intraday.data import IntradayDataService
    from src.intraday.reporting import save_summary, summarize_result
    from src.intraday.scanner import IntradayScanner
    from src.intraday.simulator import IntradaySimulator
    from src.intraday.watch import IntradayWatchEngine
    from src.intraday.spread_math import scan_symbols
    from src.universe import load_universe

    as_of = _parse_date(args.as_of) or date.today()
    max_symbols = args.max_candidates
    interval = str(config.get("intraday.default_interval", "FIVE_MINUTE")).upper()
    # Paper Calan still needs live Angel One candles; portfolio fills stay in
    # the intraday paper state, not in the equity journal broker.
    broker = build_market_data_broker(config, force_simulated=args.simulated_broker)

    try:
        broker.connect()
        data = IntradayDataService(config, broker=broker)
        universe = load_universe(config)
        symbols = scan_symbols(config, universe.symbols, max_symbols)

        if args.command == "watch":
            state = IntradayWatchEngine(config, data, symbols).run_until_close()
            print(f"\n[{persona_id}] Calan watch finished")
            print(f"  date          {state.trading_date}")
            print(f"  trades        {len(state.completed_trades)} completed, {len(state.open_positions)} open")
            print(f"  net P&L       Rs {state.daily_pnl:,.2f}")
            if state.lockout_reason:
                print(f"  lockout       {state.lockout_reason}")
            return 0

        frames = {}
        one_shot_symbols = symbols[:25] if max_symbols is None else symbols
        for symbol in one_shot_symbols:
            frame = data.candles(symbol, as_of, interval=interval)
            if not frame.empty:
                frames[symbol] = frame

        if args.command == "execute":
            print(
                f"\n[{persona_id}] Intraday execution is paper-only in v1; "
                "no live orders are placed."
            )
            return 0

        if args.command == "close":
            print(
                f"\n[{persona_id}] Intraday positions are force-closed inside "
                "the paper simulation; no separate close step is needed."
            )
            return 0

        if args.command == "learn":
            print(f"\n[{persona_id}] Learning is not wired for intraday paper results yet.")
            return 0

        if args.command not in {"analyse", "cycle"}:
            return 0

        previous_days = data.previous_day_stats_batch(
            list(frames.keys()), as_of, interval=interval
        )
        candidates = IntradayScanner(config).scan(frames, previous_days)
        result = IntradaySimulator(config).run(frames, previous_days, as_of, as_of)
        summary = summarize_result(result)
        path = save_summary(config, summary)

        print(f"\n[{persona_id}] Calan intraday paper run for {as_of} ({interval})")
        print(f"  scanned       {len(frames)} symbol(s)")
        print(f"  setups        {len(candidates)} candidate(s)")
        print(f"  trades        {summary['total_trades']}")
        print(f"  net P&L       Rs {summary['total_net_pnl']:,.2f}")
        print(f"  expectancy    {summary['expectancy_r']:+.3f}R")
        print(f"  result        {path}")
        if not frames:
            print(
                "  note          no intraday candles were available; "
                "Angel One credentials or cached candles are needed"
            )
        return 0
    finally:
        broker.disconnect()


def _persona_execution_flags(persona_id: str, pcfg, args) -> tuple[bool, Optional[int]]:
    force_simulated = bool(args.simulated_broker)
    max_candidates = args.max_candidates
    if persona_id.startswith("userbook_"):
        from src.workspace.entitlements import EntitlementsService

        entitlements = EntitlementsService(pcfg)
        force_simulated = force_simulated or entitlements.force_simulated_for_persona(persona_id)
        if max_candidates is None:
            max_candidates = entitlements.long_cycle_candidates_for_persona(persona_id)
    return force_simulated, max_candidates


def _run_persona(persona_id: str, args, base_config) -> int:
    """Run a single command for one persona. Returns the exit code."""
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

    if pcfg.get("intraday.enabled", False):
        return _run_intraday_persona(persona_id, args, pcfg)

    from src.orchestrator import FundManager

    if args.command == "learn":
        _run_learning_for(pcfg, as_of=as_of)
        return 0

    force_simulated, max_candidates = _persona_execution_flags(persona_id, pcfg, args)
    manager = FundManager(config=pcfg, force_simulated=force_simulated)

    try:
        if args.command == "watch":
            print(f"\n[{persona_id}] Watch mode is only available for intraday personas.")
            return 0

        if args.command == "analyse":
            result = manager.analyse(
                as_of=as_of,
                skip_news=args.skip_news,
                refresh_fundamentals=args.refresh_fundamentals,
                max_candidates=max_candidates,
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

        if args.command == "weekly_research":
            result = manager.weekly_research(
                as_of=as_of,
                refresh_fundamentals=args.refresh_fundamentals,
                max_candidates=max_candidates,
                trigger_source="manual",
            )
            print(
                f"\n[{persona_id}] Weekly research refreshed {len(result.candidates)} "
                f"candidate(s) for {result.as_of.isoformat()}."
            )
            return 0

        if args.command == "daily_monitor":
            result = manager.daily_monitor(
                as_of=as_of,
                skip_news=args.skip_news,
                refresh_fundamentals=args.refresh_fundamentals,
                trigger_source="manual",
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

        if args.command == "long_cycle":
            result = manager.analyse(
                as_of=as_of,
                skip_news=args.skip_news,
                refresh_fundamentals=args.refresh_fundamentals,
                max_candidates=max_candidates,
            )
            print(
                f"\n[{persona_id}] Scheduled long cycle screened "
                f"{len(result.candidates)} candidate(s)."
            )
            if result.proposal_count:
                print(
                    f"[{persona_id}] Created {result.proposal_count} proposal(s); "
                    "auto-approving in paper mode."
                )
                try:
                    approved = manager.auto_approve()
                except AutoApprovalRefused as exc:
                    print(
                        f"[{persona_id}] Scheduled long cycle refused auto-approval: {exc}",
                        file=sys.stderr,
                    )
                    return 1
                print(f"[{persona_id}] Auto-approved {approved} proposal(s).")
            else:
                print(f"[{persona_id}] No actionable proposals were created.")

            summary = manager.execute_approved(
                technicals=result.technicals, fundamentals=result.fundamentals
            )
            print(f"[{persona_id}] {summary.describe()}")
            return 0 if not summary.failed else 1

        if args.command == "cycle":
            result = manager.daily_monitor(
                as_of=as_of,
                skip_news=args.skip_news,
                refresh_fundamentals=args.refresh_fundamentals,
                trigger_source="manual",
            )
            if not result.proposal_count:
                print(f"\n[{persona_id}] No trades proposed today.")
                return 0

            _clear_pending(manager, args)
            summary = manager.execute_approved(
                technicals=result.technicals, fundamentals=result.fundamentals
            )
            print(f"\n[{persona_id}] {summary.describe()}")
            return 0 if not summary.failed else 1

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

    if args.command == "status" and args.persona:
        try:
            return _run_persona(args.persona, args, config)
        except SystemExit as exc:
            raise exc

    if args.command == "status":
        _report_status()
        return 0

    if args.command == "dashboard":
        from src.dashboard.app import serve

        serve()
        return 0

    if args.command == "publish_dashboard":
        from src.ipo.service import IpoResearchService
        from src.workspace.dashboard_snapshot import DashboardSnapshotService
        from src.workspace.user_books import UserBookService

        ipo_analysis = IpoResearchService(config).daily_analysis()
        snapshot = DashboardSnapshotService(config).publish(
            _parse_date(args.as_of) or date.today(),
            ipos=ipo_analysis,
            refresh_ipos=args.refresh_ipo_research,
            refresh_portfolio=args.refresh_portfolio,
        )
        user_books_recorded = UserBookService(config).record_all_snapshots(snapshot["snapshot_date"])
        print(f"Published delayed/EOD dashboard snapshot for {snapshot['snapshot_date']} at {snapshot['as_of']}")
        print(f"Recorded {user_books_recorded} user watchlist snapshot(s)")
        return 0

    if args.command == "long_cycle":
        personas, problems = _scheduled_long_personas(args, config)
        if problems:
            for problem in problems:
                print(f"Scheduled long cycle refused: {problem}", file=sys.stderr)
            return 1
        if not personas:
            if os.environ.get("FUND_SCHEDULED_RUN") == "1":
                from src.scheduler_controls import SchedulerControls

                controls = SchedulerControls(config)
                if not controls.enabled("india_long_cycle") and not controls.enabled("us_long_cycle"):
                    print("Scheduled long cycle skipped: all market controls are disabled.")
                    return 0
            print("Scheduled long cycle found no eligible long-horizon paper personas.", file=sys.stderr)
            return 1

        args.auto_approve = True
        worst_exit = 0
        for persona_id in personas:
            exit_code = _run_persona(persona_id, args, config)
            worst_exit = max(worst_exit, exit_code)
            if exit_code == 130:
                break
        return worst_exit

    personas = _resolve_personas(args, config)
    worst_exit = 0
    for persona_id in personas:
        exit_code = _run_persona(persona_id, args, config)
        worst_exit = max(worst_exit, exit_code)
        if exit_code == 130:
            break

    return worst_exit


if __name__ == "__main__":
    raise SystemExit(main())
