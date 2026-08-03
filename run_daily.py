#!/usr/bin/env python3
"""Entry point for the fund's daily cycle."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional

from src.config import (
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
        choices=["analyse", "execute", "cycle", "close", "dashboard", "status", "watch"],
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
        help="run only this persona (e.g. calan for intraday paper)",
    )
    return parser


def _print_config_problems(config) -> None:
    problems = config.validate()
    if problems:
        label = getattr(config, "persona_id", "default")
        print(f"Configuration problems for {label!r}:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        raise SystemExit(
            "Fix config/settings.yaml before running. See scripts/setup_check.py."
        )


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


def _intraday_summary_path(config) -> Path:
    return config.intraday_dir / f"{config.persona_id}_latest.json"


def _load_intraday_summary(config) -> Optional[dict]:
    path = _intraday_summary_path(config)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _run_intraday_persona(persona_id: str, args, config) -> int:
    from src.broker.factory import build_market_data_broker
    from src.intraday.reporting import save_summary, summarize_result
    from src.intraday.scanner import IntradayScanner
    from src.intraday.simulator import IntradaySimulator
    from src.intraday.watch import IntradayWatchEngine
    from src.intraday.data import IntradayDataService
    from src.universe import load_universe
    from src.dashboard.service import money, pct

    if args.command == "status":
        summary = _load_intraday_summary(config)
        if not summary:
            print(
                f"\n[{persona_id}] No intraday paper result saved yet. "
                f"Start with: python run_daily.py analyse --persona {persona_id}"
            )
            return 0
        print(f"\n[{persona_id}] Intraday paper/backtest ({persona_description(persona_id)})")
        print(f"  period        {summary['start_date']} to {summary['end_date']}")
        print(f"  net P&L       {money(summary['total_net_pnl'])}")
        print(f"  return        {pct(summary['total_return_pct'], 2, True)}")
        print(f"  trades        {summary['total_trades']}")
        print(f"  win rate      {pct(summary['win_rate_pct'])}")
        print(f"  expectancy    {summary['expectancy_r']:+.3f}R")
        return 0

    as_of = _parse_date(args.as_of) or date.today()
    interval = str(config.get("intraday.default_interval", "FIVE_MINUTE")).upper()
    broker = build_market_data_broker(config, force_simulated=args.simulated_broker)

    try:
        broker.connect()
        data = IntradayDataService(config, broker=broker)
        universe = load_universe(config)
        symbols = universe.symbols
        if args.max_candidates:
            symbols = symbols[: args.max_candidates]

        if args.command == "watch":
            state = IntradayWatchEngine(config, data, symbols).run_until_close()
            print(f"\n[{persona_id}] Intraday watch finished")
            print(f"  date          {state.trading_date}")
            print(
                f"  trades        {len(state.completed_trades)} completed, "
                f"{len(state.open_positions)} open"
            )
            print(f"  net P&L       Rs {state.daily_pnl:,.2f}")
            if state.lockout_reason:
                print(f"  lockout       {state.lockout_reason}")
            return 0

        if args.command in {"execute", "close", "learn"}:
            print(f"\n[{persona_id}] Intraday is paper-only; no separate {args.command} step.")
            return 0

        if args.command not in {"analyse", "cycle"}:
            return 0

        frames = {}
        fetch_limit = args.max_candidates or 25
        for symbol in symbols[:fetch_limit]:
            frame = data.candles(symbol, as_of, interval=interval)
            if not frame.empty:
                frames[symbol] = frame

        previous_days = data.previous_day_stats_batch(list(frames.keys()), as_of, interval=interval)
        candidates = IntradayScanner(config).scan(frames, previous_days)
        result = IntradaySimulator(config).run(frames, previous_days, as_of, as_of)
        summary = summarize_result(result)
        path = save_summary(config, summary)

        print(f"\n[{persona_id}] Intraday paper run for {as_of} ({interval})")
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


def _run_persona(persona_id: str, args, base_config) -> int:
    from src.orchestrator import FundManager

    try:
        pcfg = get_persona_config(persona_id)
    except ValueError as exc:
        print(f"Persona error: {exc}", file=sys.stderr)
        return 1

    print(f"\n{'─' * 60}")
    print(f"Persona: {persona_id} — {persona_description(persona_id)}")
    print(f"{'─' * 60}")

    _print_config_problems(pcfg)

    if pcfg.get("intraday.enabled", False):
        return _run_intraday_persona(persona_id, args, pcfg)

    manager = FundManager(config=pcfg, force_simulated=args.simulated_broker)
    as_of = _parse_date(args.as_of)

    def _auto_mode(manager, args) -> bool:
        if args.auto_approve is None:
            return manager.auto_approval_enabled
        return bool(args.auto_approve)

    def _clear_pending(manager, args) -> None:
        from src.orchestrator import AutoApprovalRefused

        if _auto_mode(manager, args):
            try:
                manager.auto_approve()
                return
            except AutoApprovalRefused as exc:
                print(f"\nAuto mode refused: {exc}", file=sys.stderr)
        manager.wait_for_approval(timeout_minutes=args.wait)

    try:
        if args.command == "analyse":
            result = manager.analyse(
                as_of=as_of,
                skip_news=args.skip_news,
                refresh_fundamentals=args.refresh_fundamentals,
                max_candidates=args.max_candidates,
            )
            if result.proposal_count:
                print(f"\n[{persona_id}] {result.proposal_count} proposal(s) pending.")
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
            return 0

        if args.command == "watch":
            print(f"\n[{persona_id}] Watch mode is only available for intraday personas.")
            return 1

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        manager.disconnect()

    return 0


def _auto_mode(manager, args) -> bool:
    if args.auto_approve is None:
        return manager.auto_approval_enabled
    return bool(args.auto_approve)


def _clear_pending(manager, args) -> None:
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


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)

    config = get_config()
    setup_logging(
        level="DEBUG" if args.verbose else str(config.get("logging.level", "INFO")),
        log_file=config.path("logging.file", "data/fund.log"),
    )

    if args.persona:
        return _run_persona(args.persona, args, config)

    if args.command == "status":
        _report_status()
        return 0

    if args.command == "dashboard":
        from src.dashboard.app import serve

        serve()
        return 0

    _print_config_problems(config)

    from src.orchestrator import FundManager

    manager = FundManager(force_simulated=args.simulated_broker)
    as_of = _parse_date(args.as_of)

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
                    f"\n{result.proposal_count} proposal(s) pending. Auto mode is "
                    f"on, so they are approved and placed by:\n"
                    f"  python run_daily.py execute"
                )
            elif result.proposal_count:
                host = config.get("dashboard.host", "127.0.0.1")
                port = config.get("dashboard.port", 8000)
                print(
                    f"\n{result.proposal_count} proposal(s) await your approval.\n"
                    f"Start the dashboard and review them:\n"
                    f"  python run_daily.py dashboard   ->  http://{host}:{port}/proposals"
                )
            else:
                print("\nNo trades proposed today. See the memo:")
                print("  python run_daily.py status")
            return 0

        if args.command == "execute":
            if _auto_mode(manager, args) and manager.pending_count():
                _clear_pending(manager, args)
            summary = manager.execute_approved()
            print(f"\n{summary.describe()}")
            return 0 if not summary.failed else 1

        if args.command == "cycle":
            result = manager.analyse(
                as_of=as_of,
                skip_news=args.skip_news,
                refresh_fundamentals=args.refresh_fundamentals,
                max_candidates=args.max_candidates,
            )
            if not result.proposal_count:
                print("\nNo trades proposed today; nothing to approve or execute.")
                return 0

            _clear_pending(manager, args)
            summary = manager.execute_approved(
                technicals=result.technicals, fundamentals=result.fundamentals
            )
            print(f"\n{summary.describe()}")
            return 0

        if args.command == "close":
            manager.close_of_day(as_of=as_of)
            return 0

        if args.command == "watch":
            print("Watch mode requires an intraday persona, e.g. --persona calan.", file=sys.stderr)
            return 1

    except KeyboardInterrupt:
        print("\nInterrupted. Nothing further was sent to the broker.", file=sys.stderr)
        return 130
    finally:
        manager.disconnect()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
