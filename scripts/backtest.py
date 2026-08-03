"""Run backtests for one or all personas.

Usage:
    python scripts/backtest.py --persona balanced --start 2024-01-01 --end 2025-12-31
    python scripts/backtest.py --all --start 2024-01-01 --end 2025-12-31
    python scripts/backtest.py --optimize --persona balanced --start 2024-01-01 --end 2025-12-31
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest.engine import BacktestEngine, BacktestResult
from src.backtest.optimizer import PersonaOptimizer
from src.config import enabled_personas, get_persona_config
from src.logging_setup import get_logger, setup_logging

logger = get_logger(__name__)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _print_result(result: BacktestResult) -> None:
    print(f"\n{'='*60}")
    print(f"  Persona: {result.persona_id}")
    print(f"  Period:  {result.start_date} to {result.end_date}")
    print(f"{'='*60}")
    print(f"  Total Return:      {result.total_return_pct:+.2f}%")
    print(f"  Annualised Return: {result.annualised_return_pct:+.2f}%")
    print(f"  Max Drawdown:      {result.max_drawdown_pct:.2f}%")
    print(f"  Sharpe Ratio:      {result.sharpe_ratio or 'N/A'}")
    print(f"  Sortino Ratio:     {result.sortino_ratio or 'N/A'}")
    print(f"  Total Trades:      {result.total_trades}")
    print(f"  Turnover:          {result.turnover_pct:.1f}%")
    print(f"  Win Rate:          {result.win_rate_pct or 'N/A'}")
    print(f"{'='*60}\n")


def run_backtest(persona_id: str, start: date, end: date) -> BacktestResult:
    config = get_persona_config(persona_id)
    engine = BacktestEngine(config=config, start_date=start, end_date=end)
    return engine.run()


def run_optimization(persona_id: str, start: date, end: date, metric: str) -> None:
    optimizer = PersonaOptimizer(
        persona_id=persona_id,
        start_date=start,
        end_date=end,
    )
    result = optimizer.optimize(metric=metric)

    print(f"\n{'='*60}")
    print(f"  OPTIMIZATION RESULTS: {result.persona_id}")
    print(f"{'='*60}")
    print(f"\n  Original Parameters:")
    for k, v in result.original_params.items():
        print(f"    {k}: {v}")
    print(f"\n  Optimized Parameters:")
    for k, v in result.optimized_params.items():
        print(f"    {k}: {v}")
    print(f"\n  Performance Comparison:")
    print(f"    {'Metric':<25} {'Original':>10} {'Optimized':>10} {'Delta':>10}")
    print(f"    {'-'*55}")
    for attr in ("total_return_pct", "annualised_return_pct", "max_drawdown_pct",
                 "sharpe_ratio", "sortino_ratio"):
        orig = getattr(result.original_performance, attr)
        opt = getattr(result.optimized_performance, attr)
        delta = result.improvements.get(attr)
        print(
            f"    {attr:<25} "
            f"{orig if orig is not None else 'N/A':>10} "
            f"{opt if opt is not None else 'N/A':>10} "
            f"{f'{delta:+.2f}' if delta is not None else '':>10}"
        )
    print(f"{'='*60}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest persona strategies over historical data."
    )
    parser.add_argument(
        "--persona", type=str, default=None,
        help="Persona id to backtest (e.g. balanced, momentum_rider)",
    )
    parser.add_argument("--all", action="store_true", help="Backtest all enabled personas")
    parser.add_argument(
        "--start", type=_parse_date,
        default=date.today() - timedelta(days=365),
        help="Start date (YYYY-MM-DD). Default: 1 year ago.",
    )
    parser.add_argument(
        "--end", type=_parse_date,
        default=date.today(),
        help="End date (YYYY-MM-DD). Default: today.",
    )
    parser.add_argument("--optimize", action="store_true", help="Run parameter optimization")
    parser.add_argument(
        "--metric", type=str, default="sharpe_ratio",
        help="Metric to optimize (default: sharpe_ratio)",
    )

    args = parser.parse_args()
    setup_logging(level="INFO")

    if args.optimize:
        if not args.persona:
            parser.error("--optimize requires --persona")
        logger.info("Running optimization for %s", args.persona)
        run_optimization(args.persona, args.start, args.end, args.metric)
        return

    if args.all:
        personas = enabled_personas()
        logger.info("Backtesting %d personas: %s", len(personas), personas)
        results: list = []
        for pid in personas:
            result = run_backtest(pid, args.start, args.end)
            _print_result(result)
            results.append(result)

        # Summary comparison.
        print(f"\n{'='*60}")
        print("  COMPARISON SUMMARY")
        print(f"{'='*60}")
        print(f"  {'Persona':<22} {'Return%':>8} {'Ann%':>8} {'MaxDD%':>8} {'Sharpe':>8}")
        print(f"  {'-'*54}")
        for r in results:
            sharpe_str = f"{r.sharpe_ratio:.3f}" if r.sharpe_ratio else "N/A"
            print(
                f"  {r.persona_id:<22} "
                f"{r.total_return_pct:>+7.1f}% "
                f"{r.annualised_return_pct:>+7.1f}% "
                f"{r.max_drawdown_pct:>7.1f}% "
                f"{sharpe_str:>8}"
            )
        print()
        return

    if not args.persona:
        parser.error("Provide --persona or --all")

    result = run_backtest(args.persona, args.start, args.end)
    _print_result(result)


if __name__ == "__main__":
    main()
