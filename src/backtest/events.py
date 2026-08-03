"""Event-study machinery: returns, buckets, Fama-MacBeth, walk-forward.

Conventions (spec §7.1, with the spec-silent choices stated):

- Event return at horizon h: close-to-close from entry_date (t=0) to t=h in
  the stock's own trading days; the market-adjusted excess is the ARITHMETIC
  difference r_i - r_bench (event-study standard). Portfolio sims use
  geometric excess per spec §7.5.
- The benchmark return spans the same two CALENDAR dates, taken at-or-before
  on the benchmark series (same-market calendars overlap almost fully).
- Net excess subtracts the round-trip cost (India 0.30%, US 0.05%, §7.3).
- Events without full forward data at a horizon are excluded AT THAT HORIZON
  only, and counted (right-censoring near the data end).
- CAR curves compound daily market-adjusted returns geometrically —
  (1+R_i)/(1+R_b) - 1 — visually identical to the arithmetic sums at these
  magnitudes and consistent with the sims.
- Fama-MacBeth: per-monthly-cohort mean net excess (or top-minus-bottom
  spread), t-stat = mean / (std/sqrt(N)) across cohorts (§7.1).
- Walk-forward (§7.7): first fold trains on signal years 2010-2014, tests
  2015; the window then EXPANDS annually to 2025. Selection data and
  evaluation data never overlap — stitched out-of-sample paths are the only
  headline evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.backtest.signals import EventSet, PriceTable
from src.logging_setup import get_logger

logger = get_logger(__name__)

ROUND_TRIP_COST = {"us": 0.0005, "india": 0.0030}  # §7.3
MIN_COHORTS_FM = 6  # below this a FM t-stat is not meaningful


@dataclass
class ReturnSet:
    """Per-horizon market-adjusted returns attached to an event frame."""

    events: pd.DataFrame
    horizons: Tuple[int, ...]
    cost: float
    dropped_no_price: int = 0

    def net_col(self, horizon: int) -> str:
        return f"ex_net_{horizon}"

    def gross_col(self, horizon: int) -> str:
        return f"ex_{horizon}"


class ReturnEngine:
    """Computes forward market-adjusted returns for event frames."""

    def __init__(self, event_set: EventSet):
        self.event_set = event_set
        self.market = event_set.market
        self.cost = ROUND_TRIP_COST[self.market]
        self._builder_calendar: Optional[PriceTable] = None

    def _calendar(self) -> PriceTable:
        if self._builder_calendar is None:
            from src.backtest.signals import EventBuilder

            builder = EventBuilder(self.market, store=None)  # calendar only
            self._builder_calendar = builder.calendar()
        return self._builder_calendar

    def attach(
        self,
        events: pd.DataFrame,
        horizons: Sequence[int],
        price_table_fn: Callable[[str], Optional[PriceTable]],
    ) -> ReturnSet:
        """Adds ex_{h} and ex_net_{h} columns for each horizon (vectorised)."""
        bench = self._calendar()
        events = events.copy()
        for h in horizons:
            events[f"ex_{h}"] = np.nan
        bench_dates, bench_close = bench.dates, bench.close
        dropped = 0
        for symbol, group in events.groupby("symbol"):
            table = price_table_fn(symbol)
            if table is None:
                dropped += len(group) * len(horizons)
                continue
            pos = group.entry_pos.fillna(-1).to_numpy(dtype=int)
            entry_dates = table.dates[np.clip(pos, 0, len(table.dates) - 1)]
            b0_idx = np.searchsorted(bench_dates, entry_dates, side="right") - 1
            for h in horizons:
                exit_pos = pos + h
                ok = (pos >= 0) & (exit_pos < len(table.close)) & (b0_idx >= 0)
                if not ok.any():
                    dropped += int((pos >= 0).sum())
                    continue
                base = table.close[pos[ok]]
                exit_dates = table.dates[exit_pos[ok]]
                b1_idx = np.searchsorted(bench_dates, exit_dates, side="right") - 1
                good = (b1_idx >= 0) & (base > 0)
                r_i = table.close[exit_pos[ok][good]] / base[good] - 1.0
                r_b = bench_close[b1_idx[good]] / bench_close[b0_idx[ok][good]] - 1.0
                target = group.index[ok][good]
                events.loc[target, f"ex_{h}"] = r_i - r_b
                dropped += int((pos >= 0).sum() - len(target))
        for h in horizons:
            events[f"ex_net_{h}"] = events[f"ex_{h}"] - self.cost
        return ReturnSet(events=events, horizons=tuple(horizons), cost=self.cost,
                         dropped_no_price=dropped)

    def car_curve(
        self,
        events: pd.DataFrame,
        price_table_fn: Callable[[str], Optional[PriceTable]],
        t_max: int = 252,
        pre_days: int = 0,
    ) -> pd.DataFrame:
        """Mean/median/quantile CAR per trading day, plus optional pre-window.

        Returns a frame indexed by t (-pre_days..t_max) with columns
        mean/median/p25/p75/n. t=0 is the entry close; pre-window values are
        measured BACKWARD from availability (diagnostic only, §7.1).
        """
        bench = self._calendar()
        curves: List[np.ndarray] = []
        for row in events.itertuples():
            table = price_table_fn(row.symbol)
            raw_pos = getattr(row, "entry_pos", -1)
            if table is None or not np.isfinite(raw_pos) or raw_pos < 0:
                continue
            pos = int(raw_pos)
            lo, hi = pos - pre_days, min(pos + t_max, len(table.close) - 1)
            if lo < 0 or hi <= pos:
                continue
            days = table.dates[lo: hi + 1]
            stock = table.close[lo: hi + 1]
            bench_prices = np.array(
                [bench.price_at_or_before(pd.Timestamp(d)) for d in days],
                dtype=float,
            )
            if not np.isfinite(bench_prices).all():
                continue
            anchor_s = float(stock[pre_days]) if pre_days else float(stock[0])
            anchor_b = float(bench_prices[pre_days]) if pre_days else float(bench_prices[0])
            # Entry close anchors at 1.0 both sides; curve = relative wealth ratio.
            wealth = (stock / anchor_s) / (bench_prices / anchor_b)
            full = np.full(pre_days + t_max + 1, np.nan)
            full[: len(wealth)] = wealth - 1.0
            curves.append(full)
        if not curves:
            return pd.DataFrame()
        mat = np.vstack(curves)
        idx = np.arange(-pre_days, t_max + 1)
        return pd.DataFrame(
            {
                "mean": np.nanmean(mat, axis=0),
                "median": np.nanmedian(mat, axis=0),
                "p25": np.nanpercentile(mat, 25, axis=0),
                "p75": np.nanpercentile(mat, 75, axis=0),
                "n": np.isfinite(mat).sum(axis=0),
            },
            index=idx,
        ).rename_axis("t")


# ------------------------------------------------------------------ bucketing


def assign_buckets(
    events: pd.DataFrame,
    score_col: str,
    n_buckets: int,
    tie_col: str = "g_rev",
) -> pd.Series:
    """Deterministic within-cohort buckets; bucket 0 = highest score.

    Ties break on higher revenue YoY growth, then symbol (§3.4). Events with
    a missing score get bucket -1 (unranked).
    """
    buckets = pd.Series(-1, index=events.index, dtype=int)
    for _, cohort in events.groupby("cohort"):
        ranked = cohort.dropna(subset=[score_col]).sort_values(
            [score_col, tie_col, "symbol"],
            ascending=[False, False, True],
        )
        n = len(ranked)
        if n == 0:
            continue
        for rank0, idx in enumerate(ranked.index):
            buckets.loc[idx] = min(int(rank0 * n_buckets / n), n_buckets - 1)
    return buckets


# -------------------------------------------------------------- Fama-MacBeth


def fama_macbeth(cohort_means: pd.Series) -> Tuple[float, int]:
    """t-stat across monthly cohort means; (nan, n) when too few cohorts."""
    means = cohort_means.dropna()
    n = len(means)
    if n < MIN_COHORTS_FM:
        return float("nan"), n
    sd = means.std(ddof=1)
    if sd <= 0:
        return float("nan"), n
    return float(means.mean() / (sd / np.sqrt(n))), n


def cohort_stats(
    events: pd.DataFrame, value_col: str
) -> pd.DataFrame:
    """Per-cohort mean/n for the value column."""
    return (
        events.groupby("cohort")[value_col]
        .agg(["mean", "count"])
        .rename(columns={"mean": "cohort_mean", "count": "n"})
    )


def per_year_stats(events: pd.DataFrame, value_col: str) -> pd.Series:
    """Mean net excess per calendar year of availability (sign consistency)."""
    years = events.availability.dt.year
    return events.groupby(years)[value_col].mean()


def hit_rate(events: pd.DataFrame, value_col: str) -> float:
    values = events[value_col].dropna()
    if not len(values):
        return float("nan")
    return float((values > 0).mean())


def bucket_summary(
    events: pd.DataFrame, bucket_col: str, value_col: str
) -> pd.DataFrame:
    """Mean/median/n per bucket plus top-bottom spread and rank correlation."""
    grouped = (
        events[events[bucket_col] >= 0]
        .groupby(bucket_col)[value_col]
        .agg(["mean", "median", "count"])
    )
    if len(grouped) >= 2:
        ranks = grouped["mean"].rank()
        order = pd.Series(grouped.index, index=grouped.index, dtype=float).rank()
        cov = np.cov(ranks, order)[0, 1]
        corr = cov / (ranks.std(ddof=1) * order.std(ddof=1)) if ranks.std(ddof=1) > 0 else np.nan
        grouped.attrs["spread_top_minus_bottom"] = float(
            grouped["mean"].iloc[0] - grouped["mean"].iloc[-1]
        )
        grouped.attrs["rank_corr"] = float(corr)
    return grouped


# --------------------------------------------------------------- walk-forward


@dataclass
class Fold:
    test_year: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp  # inclusive last day of training data


def make_folds(first_test_year: int = 2015, last_test_year: int = 2025) -> List[Fold]:
    """§7.7: 5-year first window, expanding thereafter; test = next year."""
    folds = []
    for year in range(first_test_year, last_test_year + 1):
        folds.append(
            Fold(
                test_year=year,
                train_start=pd.Timestamp(year - 5, 1, 1),
                train_end=pd.Timestamp(year - 1, 12, 31),
            )
        )
    return folds


def train_test_split(
    events: pd.DataFrame, fold: Fold
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    avail = events.availability
    train = events[(avail >= fold.train_start) & (avail <= fold.train_end)]
    test = events[events.availability.dt.year == fold.test_year]
    return train, test


def select_grid_cell(
    train: pd.DataFrame,
    grid: Sequence[dict],
    metric_fn: Callable[[pd.DataFrame, dict], float],
    tie_break: Optional[Callable[[dict], tuple]] = None,
    maximise: bool = True,
) -> Tuple[dict, pd.DataFrame]:
    """Scores every grid cell on TRAIN data only; returns (winner, table).

    Ties resolve through ``tie_break`` (simpler cells win). The metric is a
    Fama-MacBeth t-stat supplied by the caller.
    """
    rows = []
    for cell in grid:
        score = metric_fn(train, cell)
        rows.append({**cell, "score": score})
    table = pd.DataFrame(rows)
    valid = table.dropna(subset=["score"])
    if valid.empty:
        return dict(grid[0]), table
    valid = valid.copy()
    valid["tie"] = [tie_break(row) for _, row in valid.iterrows()] if tie_break else 0
    best_score = valid.score.max() if maximise else valid.score.min()
    winners = valid[np.isclose(valid.score, best_score, rtol=1e-9, atol=1e-12)]
    winner = winners.sort_values("tie").iloc[0]
    cell = {
        k: (v.item() if hasattr(v, "item") else v)
        for k, v in winner.items()
        if k in grid[0]
    }
    return cell, table
