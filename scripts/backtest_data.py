#!/usr/bin/env python3
"""Download every dataset the quarterly-earnings backtest needs.

Legs (compose freely):

- ``--prices``  deep daily history for both universes plus SPY and ^CRSLDX.
- ``--us``      SEC EDGAR quarterly fundamentals for the S&P 500.
- ``--india``   Screener.in quarterly results for the most liquid Nifty names.
- ``--report``  regenerate data/backtest/coverage.md from the caches.

``--smoke`` runs all three legs on 25 US + 25 India names and validates the
outputs — always do that before a full pull. ``--max N`` caps the US leg at N
names and the India leg at the N most liquid (default 250). ``--resume``
skips tickers whose outputs already exist; without it, outputs are rebuilt
(EDGAR still reuses its raw JSON cache, so a rebuild stays off the network).

Typical first run: ``python scripts/backtest_data.py --smoke`` then
``python scripts/backtest_data.py --us --india --prices --resume``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.logging_setup import get_logger, setup_logging

logger = get_logger("backtest_data")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
BACKTEST_DIR = DATA_DIR / "backtest"

SMOKE_SIZE = 25
DEFAULT_INDIA_MAX = 250

US_BENCHMARKS = ["SPY"]
INDIA_BENCHMARKS = ["^CRSLDX"]


# ----------------------------------------------------------------------
# Universes
# ----------------------------------------------------------------------
def load_sp500(path: Optional[Path] = None) -> pd.DataFrame:
    """symbol/name/sector per constituent; first line is a fetched-on comment."""
    path = path or DATA_DIR / "sp500.csv"
    return pd.read_csv(path, comment="#")


def load_nifty500(path: Optional[Path] = None) -> pd.DataFrame:
    path = path or DATA_DIR / "nifty500.csv"
    frame = pd.read_csv(path)
    return frame.rename(columns=str.lower)


# ----------------------------------------------------------------------
# Legs
# ----------------------------------------------------------------------
def run_prices(
    us_symbols: List[str], india_symbols: List[str], resume: bool
) -> Dict[str, Dict[str, str]]:
    from src.backtest.prices import BacktestPriceStore

    store = BacktestPriceStore(BACKTEST_DIR / "prices")
    statuses = {
        "benchmarks": store.fetch_benchmarks(resume=resume),
        "us": store.fetch_us(us_symbols, resume=resume),
        "india": store.fetch_india(india_symbols, resume=resume),
    }
    for leg, leg_statuses in statuses.items():
        counts: Dict[str, int] = {}
        for status in leg_statuses.values():
            counts[status] = counts.get(status, 0) + 1
        logger.info("prices[%s]: %s", leg, counts)
    return statuses


def run_us(tickers: List[str], universe: pd.DataFrame, resume: bool) -> List[dict]:
    from src.backtest.edgar import EdgarClient, fetch_us_fundamentals

    fallback = {
        str(row.symbol).upper(): int(row.cik)
        for row in universe.itertuples()
        if pd.notna(getattr(row, "cik", None))
    }
    client = EdgarClient(BACKTEST_DIR / "us" / "raw")
    try:
        results = fetch_us_fundamentals(
            tickers,
            raw_dir=BACKTEST_DIR / "us" / "raw",
            quarterly_dir=BACKTEST_DIR / "us" / "quarterly",
            client=client,
            fallback_ciks=fallback,
            resume=resume,
        )
    finally:
        client.close()
    failed = [r.ticker for r in results if r.error]
    logger.info(
        "US leg: %d/%d succeeded; failures: %s",
        len(results) - len(failed),
        len(results),
        ", ".join(failed[:15]) or "none",
    )
    return [asdict(r) for r in results]


def run_india(symbols: List[str], resume: bool) -> List[dict]:
    from src.backtest.screener_quarters import (
        ScreenerQuarterlyClient,
        fetch_india_fundamentals,
    )

    client = ScreenerQuarterlyClient()
    try:
        results = fetch_india_fundamentals(
            symbols,
            quarterly_dir=BACKTEST_DIR / "india" / "quarterly",
            announcements_dir=BACKTEST_DIR / "india" / "announcements",
            client=client,
            resume=resume,
        )
    finally:
        client.close()
    failed = [r.symbol for r in results if r.error]
    logger.info(
        "India leg: %d/%d succeeded; rate limits hit: %d; failures: %s",
        len(results) - len(failed),
        len(results),
        client.rate_limited_count,
        ", ".join(failed[:15]) or "none",
    )
    return [asdict(r) for r in results]


def select_india_universe(nifty: pd.DataFrame, max_n: int) -> List[str]:
    """Top N Nifty 500 names by 60-day average daily turnover (cached prices)."""
    from src.backtest.prices import BacktestPriceStore, india_yahoo_symbol

    store = BacktestPriceStore(BACKTEST_DIR / "prices")
    symbols = [str(s).upper() for s in nifty["symbol"]]
    store_keys = {symbol: india_yahoo_symbol(symbol) for symbol in symbols}
    ranked = store.rank_by_turnover(list(store_keys.values()))
    have_prices = [key for key in ranked if store.average_daily_turnover(key) is not None]
    missing = [key for key in ranked if store.average_daily_turnover(key) is None]
    if missing:
        logger.warning(
            "No cached prices for %d Nifty names (ranked last): %s",
            len(missing),
            ", ".join(missing[:10]),
        )
    selected_keys = (have_prices + missing)[:max_n]
    by_key = {key: symbol for symbol, key in store_keys.items()}
    return [by_key[key] for key in selected_keys]


# ----------------------------------------------------------------------
# Smoke validation
# ----------------------------------------------------------------------
@dataclass
class SmokeCheck:
    name: str
    passed: bool
    detail: str


def validate_smoke(us_symbols: List[str], india_symbols: List[str]) -> List[SmokeCheck]:
    """Sane-value checks on whatever the smoke run just wrote."""
    from src.backtest.prices import BacktestPriceStore

    checks: List[SmokeCheck] = []
    today = date.today()

    us_dir = BACKTEST_DIR / "us" / "quarterly"
    us_frames = []
    for ticker in us_symbols:
        path = us_dir / f"{ticker.replace('.', '_')}.csv"
        if path.exists():
            us_frames.append(pd.read_csv(path, parse_dates=["period_end", "filed"]))
    us_all = pd.concat(us_frames, ignore_index=True) if us_frames else pd.DataFrame()

    checks.append(
        SmokeCheck(
            "us_outputs_exist",
            len(us_frames) >= len(us_symbols) * 0.8,
            f"{len(us_frames)}/{len(us_symbols)} tickers parsed",
        )
    )
    if not us_all.empty:
        filed_before_end = (us_all["filed"] < us_all["period_end"]).sum()
        future_filed = (us_all["filed"].dt.date > today).sum()
        profitable = us_all[us_all["net_income"] > 0]
        eps_positive_share = (
            (profitable["eps_diluted"] > 0).mean() if len(profitable) else 0.0
        )
        checks += [
            SmokeCheck(
                "us_filed_not_before_period_end",
                filed_before_end == 0,
                f"{filed_before_end} violations",
            ),
            SmokeCheck(
                "us_no_future_filed_dates",
                future_filed == 0,
                f"{future_filed} violations",
            ),
            SmokeCheck(
                "us_eps_sign_matches_profit",
                eps_positive_share > 0.9,
                f"EPS>0 in {eps_positive_share:.0%} of profitable quarters",
            ),
            SmokeCheck(
                "us_reasonable_depth",
                (us_all["period_end"].max() - us_all["period_end"].min()).days
                > 3 * 365,
                f"span {us_all['period_end'].min().date()}..{us_all['period_end'].max().date()}",
            ),
        ]

    india_dir = BACKTEST_DIR / "india" / "quarterly"
    india_frames = []
    for symbol in india_symbols:
        path = india_dir / f"{symbol}.csv"
        if path.exists():
            india_frames.append(pd.read_csv(path, parse_dates=["period_end"]))
    india_all = (
        pd.concat(india_frames, ignore_index=True) if india_frames else pd.DataFrame()
    )
    checks.append(
        SmokeCheck(
            "india_outputs_exist",
            len(india_frames) >= len(india_symbols) * 0.7,
            f"{len(india_frames)}/{len(india_symbols)} tickers parsed",
        )
    )
    if not india_all.empty:
        future_periods = (india_all["period_end"].dt.date > today).sum()
        sales_positive_share = (india_all["sales_cr"].dropna() > 0).mean()
        checks += [
            SmokeCheck(
                "india_no_future_periods",
                future_periods == 0,
                f"{future_periods} violations",
            ),
            SmokeCheck(
                "india_sales_mostly_positive",
                sales_positive_share > 0.9,
                f"sales>0 in {sales_positive_share:.0%} of quarters",
            ),
        ]

    store = BacktestPriceStore(BACKTEST_DIR / "prices")
    manifest = store.manifest()
    have = [t for t in US_BENCHMARKS + INDIA_BENCHMARKS if t in manifest]
    checks.append(
        SmokeCheck(
            "benchmark_prices_exist",
            len(have) == len(US_BENCHMARKS) + len(INDIA_BENCHMARKS),
            f"have {have}",
        )
    )
    depths = [d for d in store.history_depths().values() if d is not None]
    checks.append(
        SmokeCheck(
            "price_history_is_deep",
            bool(depths) and min(depths) <= date(2016, 1, 1),
            f"earliest first bar {min(depths) if depths else 'n/a'}",
        )
    )
    return checks


# ----------------------------------------------------------------------
# Coverage report
# ----------------------------------------------------------------------
def build_coverage_report(summary: Optional[dict]) -> str:
    """Markdown coverage report from the caches; honest about the gaps."""
    from src.backtest.prices import BacktestPriceStore

    lines: List[str] = []
    add = lines.append
    add("# Backtest data coverage")
    add("")
    add(f"Generated {datetime.now().isoformat(timespec='seconds')} from the caches "
        "under `data/backtest/`. Regenerate with "
        "`python scripts/backtest_data.py --report`.")
    add("")

    store = BacktestPriceStore(BACKTEST_DIR / "prices")

    # --- US fundamentals ---
    us_dir = BACKTEST_DIR / "us" / "quarterly"
    us_files = sorted(us_dir.glob("*.csv")) if us_dir.exists() else []
    quarter_counts: List[int] = []
    filed_ok = 0
    filed_total = 0
    deduced_total = 0
    row_total = 0
    us_first: List[date] = []
    for path in us_files:
        try:
            frame = pd.read_csv(path, parse_dates=["period_end", "filed"])
        except (pd.errors.ParserError, OSError):
            continue
        quarter_counts.append(len(frame))
        row_total += len(frame)
        deduced_total += int(frame["frame_deduced"].sum())
        valid = frame["filed"].notna() & (frame["filed"] >= frame["period_end"])
        filed_ok += int(valid.sum())
        filed_total += len(frame)
        if len(frame):
            us_first.append(frame["period_end"].min().date())

    add("## US fundamentals (SEC EDGAR)")
    add("")
    universe_size = len(load_sp500()) if (DATA_DIR / "sp500.csv").exists() else 0
    add(f"- Universe: {universe_size} S&P 500 constituents "
        f"(`data/sp500.csv`); parsed outputs for **{len(us_files)}** tickers.")
    if quarter_counts:
        dist = pd.Series(quarter_counts)
        add(
            f"- Quarters per ticker: min {dist.min()}, median {dist.median():.0f}, "
            f"max {dist.max()} ({row_total} rows total, "
            f"{deduced_total} frame-deduced)."
        )
        add(f"- Filing-date usability (filed present and >= period end): "
            f"**{100.0 * filed_ok / max(filed_total, 1):.1f}%** of rows.")
        add(f"- Earliest period end in cache: {min(us_first) if us_first else 'n/a'}.")
    add("")

    # --- India fundamentals ---
    india_dir = BACKTEST_DIR / "india" / "quarterly"
    india_files = sorted(india_dir.glob("*.csv")) if india_dir.exists() else []
    india_counts: List[int] = []
    india_periods: List[str] = []
    for path in india_files:
        try:
            frame = pd.read_csv(path)
        except (pd.errors.ParserError, OSError):
            continue
        india_counts.append(len(frame))
        india_periods.extend(str(p) for p in frame["period_end"])
    ann_dir = BACKTEST_DIR / "india" / "announcements"
    ann_files = sorted(ann_dir.glob("*.csv")) if ann_dir.exists() else []

    add("## India fundamentals (Screener.in)")
    add("")
    add(f"- Parsed quarterly tables for **{len(india_files)}** tickers.")
    if india_counts:
        dist = pd.Series(india_counts)
        add(
            f"- Quarters per ticker: min {dist.min()}, median {dist.median():.0f}, "
            f"max {dist.max()}; period ends span "
            f"{min(india_periods)}..{max(india_periods)}."
        )
    add(
        f"- Results-announcement dates captured for **{len(ann_files)}** tickers "
        f"({100.0 * len(ann_files) / max(len(india_files), 1):.1f}% of parsed); "
        "everyone else falls back to period_end + 45 days."
    )
    add("")

    # --- Prices ---
    manifest = store.manifest()
    depths = store.history_depths()
    add("## Prices (Yahoo Finance, auto-adjusted)")
    add("")
    add(f"- Cached series: **{len(manifest)}** (incl. benchmarks "
        f"{sorted(set(US_BENCHMARKS + INDIA_BENCHMARKS) & set(manifest))}).")
    dated = [d for d in depths.values() if d is not None]
    if dated:
        firsts = pd.Series(sorted(dated))
        add(
            f"- First-bar dates: min {firsts.iloc[0]}, median "
            f"{firsts.iloc[len(firsts) // 2]}, latest first bar {firsts.iloc[-1]}."
        )
    failed = [
        key for key, entry in manifest.items() if int(entry.get("rows", 0)) < 100
    ]
    if failed:
        add(f"- Series with suspiciously few rows (<100): {failed[:20]}.")
    add("")

    if summary:
        add("## Last pipeline run")
        add("")
        add(f"- Started: {summary.get('started_at')}, finished: "
            f"{summary.get('finished_at')}, args: `{summary.get('argv')}`")
        for leg in ("us", "india"):
            leg_rows = (summary.get("legs") or {}).get(leg) or []
            if leg_rows:
                failed_leg = [r for r in leg_rows if r.get("error")]
                add(f"- {leg.upper()} leg: {len(leg_rows) - len(failed_leg)}/"
                    f"{len(leg_rows)} succeeded; failures: "
                    f"{[r.get('ticker') or r.get('symbol') for r in failed_leg][:25]}")
        add("")

    add("## Known landmines (read before building on this data)")
    add("")
    add("### US / EDGAR")
    add("")
    add("- **Pre-XBRL `filed` dates are comparatives, not announcements.** SEC "
        "XBRL tagging only became universal around 2009-2011. Older periods "
        "appear inside later filings as prior-year comparatives, so e.g. a "
        "Q2-2008 row can carry `filed` of mid-2009. That is honest 'when this "
        "fact entered XBRL' metadata but NOT when the market learned the "
        "results. Rule of thumb: distrust rows where `filed - period_end` "
        "exceeds ~120 days, or simply start the backtest in 2010+.")
    add("- **Deduced quarters.** Rows with `frame_deduced=true` were recovered "
        "by subtracting YTD/annual durations (Q2=H1-Q1, Q3=9M-H1, Q4=FY-9M). "
        "Their `filed` is the longer duration's filing date, which is the "
        "correct point-in-time anchor. Deduced diluted EPS is approximate "
        "(weighted share counts drift), deduced flows are exact barring "
        "reclassifications.")
    add("- **First filing wins.** Amended filings (10-Q/A, 10-K/A) are dropped "
        "in favour of the original filing — restatements are deliberately "
        "ignored to stay point-in-time.")
    add("- **Row `filed` = latest filing across the four metrics** for that "
        "period: a quarter is tradeable only once everything about it is "
        "public.")
    add("- **Revenue tag transitions.** Pre-2018 revenue often lives only "
        "under legacy tags (`Revenues`, `SalesRevenueNet`); per-period "
        "fallback fills it, but level breaks at the tag switch are possible.")
    add("- **Missing metrics vary by sector.** Banks rarely tag "
        "`OperatingIncomeLoss`; expect NaNs and do not drop the row over one "
        "missing metric.")
    add("- **SEC's ticker map can point at the wrong entity.** XOM resolves to "
        "a holding-company CIK with no facts; the pipeline retries with the "
        "universe CSV's CIK column (from the same Wikipedia table) when the "
        "mapped CIK yields nothing.")
    add("- **The Wikipedia table itself had junk rows.** FDXF (FedEx Freight) "
        "and HONA (Honeywell Aerospace) are subsidiary registrants, not index "
        "members, and were removed — 501 constituents remain. Worth "
        "re-verifying membership if the universe is refreshed.")
    add("")
    add("### India / Screener")
    add("")
    add("- **Only ~12-13 quarters per company are exposed** in the free "
        "#quarters table — roughly three years of history. The India leg is a "
        "recent-window dataset, not a deep one.")
    add("- **Results announcement dates are best-effort.** Screener shows only "
        "the ~5 most recent announcements, so results dates exist only for "
        "companies that reported within the last few weeks of fetch day "
        f"({date.today().isoformat()}). Everything else must fall back to "
        "`period_end + 45 days` (the SEBI outer limit); note actuals are "
        "usually 20-45 days, so the fallback is conservative, and Q4/March "
        "results legally get 60 days.")
    add("- **Announcement dates carry no year on the page** ('25 Jul'); the "
        "year is inferred as the most recent non-future occurrence. The "
        "freshest entries use relative markers ('2h', '3d'), resolved against "
        "fetch day.")
    add("- **CCL / HAL / IEX exist on BOTH markets.** The price cache keys "
        "Indian series by Yahoo symbol (HAL.NS) precisely because these bare "
        "tickers collide with Carnival/Halliburton/IDEX. Always read the "
        "cache with market-aware keys.")
    add("- **Lazy-rendered quarter tables.** Some pages (banks/NBFCs) ship a "
        "#quarters table with labels and no values server-side; the parser "
        "retries the standalone page and records the rest as missing.")
    add("- **Units are Rs crore for flows** (not absolute rupees), Rs for EPS, "
        "percent for OPM — column names say which.")
    add("")
    add("### Prices")
    add("")
    add("- **Yahoo `auto_adjust` prices** (split/dividend-adjusted) — right for "
        "event-window returns, wrong if the engine needs raw traded levels.")
    add("- **Ticker churn.** Delisted/renamed Nifty symbols fail on Yahoo; "
        "they are the 'failed' statuses in the prices manifest and rank last "
        "in the liquidity ordering.")
    add("- **^CRSLDX is a total-return index** while SPY is adjusted price; "
        "benchmark comparisons must keep that asymmetry in mind.")
    add("")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--us", action="store_true", help="run the EDGAR leg")
    parser.add_argument("--india", action="store_true", help="run the Screener leg")
    parser.add_argument("--prices", action="store_true", help="run the prices leg")
    parser.add_argument("--report", action="store_true", help="write coverage.md")
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="cap US leg at N tickers; India leg at N most liquid (default 250)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="skip tickers whose outputs already exist",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="small end-to-end pass (25 US + 25 India + prices) with validation",
    )
    args = parser.parse_args(argv)

    setup_logging("INFO", log_file=BACKTEST_DIR / "pipeline.log")

    sp500 = load_sp500()
    nifty = load_nifty500()

    us_symbols = [str(s).upper() for s in sp500["symbol"]]
    india_all = [str(s).upper() for s in nifty["symbol"]]

    if args.smoke:
        us_selected = us_symbols[:SMOKE_SIZE]
        india_selected = india_all[:SMOKE_SIZE]
        run_all_legs = True
    else:
        us_selected = us_symbols[: args.max] if args.max else us_symbols
        india_selected = []
        run_all_legs = False

    summary = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "argv": " ".join(sys.argv),
        "legs": {},
    }

    if args.prices or args.smoke:
        price_india = india_selected if args.smoke else india_all
        run_prices(us_selected, price_india, resume=True)

    if args.us or args.smoke:
        summary["legs"]["us"] = run_us(us_selected, sp500, resume=args.resume)

    if args.india or args.smoke:
        if not india_selected:
            max_n = args.max or DEFAULT_INDIA_MAX
            india_selected = select_india_universe(nifty, max_n)
            logger.info(
                "India universe: top %d of %d by turnover",
                len(india_selected),
                len(india_all),
            )
        summary["legs"]["india"] = run_india(india_selected, resume=args.resume)

    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    if summary["legs"]:
        # --report-only runs read the last real run's summary; don't clobber it.
        summary_path = BACKTEST_DIR / "summary.json"
        with open(summary_path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)

    if args.smoke:
        checks = validate_smoke(us_selected, india_selected)
        failures = 0
        for check in checks:
            status = "PASS" if check.passed else "FAIL"
            failures += 0 if check.passed else 1
            print(f"  [{status}] {check.name}: {check.detail}")
        if failures:
            print(f"SMOKE FAILED: {failures}/{len(checks)} checks failed")
            return 1
        print(f"SMOKE OK: {len(checks)} checks passed")

    if args.report or args.smoke:
        stored_summary = summary
        if not summary["legs"] and (BACKTEST_DIR / "summary.json").exists():
            with open(BACKTEST_DIR / "summary.json", "r", encoding="utf-8") as handle:
                stored_summary = json.load(handle)
        report = build_coverage_report(stored_summary)
        (BACKTEST_DIR / "coverage.md").write_text(report, encoding="utf-8")
        logger.info("Wrote %s", BACKTEST_DIR / "coverage.md")

    if not (args.us or args.india or args.prices or args.smoke or args.report):
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
