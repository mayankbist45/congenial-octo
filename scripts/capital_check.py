"""How much starting capital does this fund actually need?

Indian equities trade in whole shares, so a target position of Rs 38,000 in a
stock priced at Rs 30,000 buys one share and leaves the rest in cash. Two things
go wrong when the fund is underfunded:

  * Names priced above a whole position value cannot be bought at all, so the
    quant screen silently loses its most expensive candidates and the portfolio
    is chosen from a smaller universe than intended.
  * Every position rounds down, so cash piles up uninvested and real weights sit
    below target, which quietly turns an equity fund into a partly-cash one.

This script measures both against live cached prices and the fund's own sizing
rules, so the answer reflects this configuration rather than a rule of thumb.

    python scripts/capital_check.py
    python scripts/capital_check.py --capital 1000000 2500000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from src.config import get_config  # noqa: E402
from src.market_data.prices import PriceService  # noqa: E402
from src.risk.sizing import (  # noqa: E402
    MAX_SIZE_FACTOR,
    MIN_SIZE_FACTOR,
    REFERENCE_VOLATILITY_PCT,
)
from src.universe import load_universe  # noqa: E402

DEFAULT_CAPITAL = [500_000, 1_000_000, 1_500_000, 2_000_000, 2_500_000, 5_000_000]
# Share of the universe that must be reachable before capital is "enough".
COVERAGE_TARGET_PCT = 98.0
# Cash left stranded by whole-share rounding, as a share of the fund.
DRAG_TARGET_PCT = 1.0


def rupees(value: float) -> str:
    if value >= 1e7:
        return f"Rs {value / 1e7:,.2f} cr"
    return f"Rs {value / 1e5:,.2f} L"


def load_market() -> Tuple[Dict[str, float], Dict[str, float], Dict[str, float]]:
    """Latest close, annualised volatility and average daily turnover per symbol."""
    config = get_config()
    universe = load_universe(config)
    history = PriceService(config).cached_history()

    prices: Dict[str, float] = {}
    volatility: Dict[str, float] = {}
    turnover: Dict[str, float] = {}

    for entry in universe:
        frame = history.get(entry.symbol)
        if frame is None or frame.empty or "Close" not in frame:
            continue
        closes = frame["Close"].dropna()
        if len(closes) < 30:
            continue

        price = float(closes.iloc[-1])
        if price <= 0:
            continue
        prices[entry.symbol] = price

        returns = closes.pct_change().dropna().tail(250)
        volatility[entry.symbol] = (
            float(returns.std() * np.sqrt(252) * 100.0) if len(returns) > 20 else 25.0
        )

        if "Volume" in frame:
            recent = frame.tail(20)
            turnover[entry.symbol] = float(
                (recent["Close"] * recent["Volume"]).mean()
            )

    return prices, volatility, turnover


def size_factor(vol: float) -> float:
    """The same volatility scaling the position sizer applies."""
    if vol <= 0:
        return MAX_SIZE_FACTOR
    return max(min(REFERENCE_VOLATILITY_PCT / vol, MAX_SIZE_FACTOR), MIN_SIZE_FACTOR)


def assess(
    capital: float,
    symbols: List[str],
    prices: Dict[str, float],
    volatility: Dict[str, float],
    target_weight_pct: float,
    positions: int,
) -> Dict[str, float]:
    """Reachability and rounding drag for one capital level."""
    unbuyable: List[str] = []
    shortfalls: List[float] = []

    for symbol in symbols:
        price = prices[symbol]
        weight = target_weight_pct * size_factor(volatility.get(symbol, 25.0))
        target_value = weight / 100.0 * capital

        if price > target_value:
            unbuyable.append(symbol)
            continue
        # Whole shares only: the remainder stays in cash.
        shortfalls.append(target_value % price)

    # A full portfolio strands roughly one rounding remainder per position.
    drag = float(np.mean(shortfalls)) * positions if shortfalls else 0.0

    return {
        "target_value": target_weight_pct / 100.0 * capital,
        "unbuyable": len(unbuyable),
        "coverage_pct": 100.0 * (len(symbols) - len(unbuyable)) / max(len(symbols), 1),
        "drag_rupees": drag,
        "drag_pct": 100.0 * drag / capital,
        "priciest_missed": unbuyable[:3],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capital",
        type=float,
        nargs="+",
        default=DEFAULT_CAPITAL,
        help="capital levels to evaluate, in rupees",
    )
    args = parser.parse_args()

    config = get_config()
    positions = int(config.get("fund.target_positions", 25) or 25)
    cash_buffer = float(config.get("fund.min_cash_buffer_pct", 5.0) or 5.0)
    liquidity_floor = float(config.get("universe.min_avg_daily_turnover_inr", 0) or 0)
    target_weight_pct = (100.0 - cash_buffer) / positions

    prices, volatility, turnover = load_market()
    if not prices:
        print("No cached prices found. Run: python run_daily.py analyse")
        return 1

    investable = [
        s for s in prices if turnover.get(s, 0.0) >= liquidity_floor
    ]
    investable.sort(key=lambda s: prices[s])

    print(f"{config.get('fund.name')} - starting capital check\n")
    print(f"  target positions      {positions}")
    print(f"  cash buffer           {cash_buffer:.0f}%")
    print(f"  target weight         {target_weight_pct:.2f}% per position")
    print(f"  priced universe       {len(prices)} names")
    print(
        f"  investable universe   {len(investable)} names above the "
        f"{rupees(liquidity_floor)}/day liquidity floor"
    )

    values = np.array([prices[s] for s in investable])
    print(
        f"  share price spread    median Rs {np.median(values):,.0f}, "
        f"90th pct Rs {np.percentile(values, 90):,.0f}, "
        f"max Rs {values.max():,.0f} ({investable[-1]})\n"
    )

    print(
        f"{'capital':>12}  {'per position':>13}  {'reachable':>10}  "
        f"{'unbuyable':>9}  {'rounding drag':>14}"
    )
    print("  " + "-" * 68)

    results = []
    for capital in sorted(args.capital):
        stats = assess(
            capital, investable, prices, volatility, target_weight_pct, positions
        )
        results.append((capital, stats))
        print(
            f"{rupees(capital):>12}  {rupees(stats['target_value']):>13}  "
            f"{stats['coverage_pct']:>9.1f}%  {stats['unbuyable']:>9}  "
            f"{rupees(stats['drag_rupees']):>9} ({stats['drag_pct']:.2f}%)"
        )

    adequate = [
        capital
        for capital, stats in results
        if stats["coverage_pct"] >= COVERAGE_TARGET_PCT
        and stats["drag_pct"] <= DRAG_TARGET_PCT
    ]

    print()
    if adequate:
        print(
            f"Comfortable from {rupees(min(adequate))}: at least "
            f"{COVERAGE_TARGET_PCT:.0f}% of the investable universe is reachable "
            f"and rounding strands under {DRAG_TARGET_PCT:.0f}% of the fund."
        )
    else:
        print(
            "None of the levels tested clear both bars "
            f"({COVERAGE_TARGET_PCT:.0f}% reachable, under {DRAG_TARGET_PCT:.0f}% "
            "rounding drag). Try a higher --capital."
        )

    worst = results[0][1]
    if worst["priciest_missed"]:
        print(
            f"At {rupees(results[0][0])} the fund cannot buy even one share of, "
            f"for example: {', '.join(worst['priciest_missed'])}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
