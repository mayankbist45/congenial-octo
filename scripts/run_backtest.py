#!/usr/bin/env python
"""Run the quarterly-earnings backtest (spec docs/earnings/backtest_spec.md).

One command builds events, runs the walk-forward grid selection (US), the
frozen-parameter out-of-sample run (India), the event studies, both portfolio
sims, the avoidance sim, and the scraped-dates sensitivity, then writes
data/backtest/report/ (report.md + CSVs + charts) via src/backtest/report.py.

Grid trimming record (sanctioned by the spec's hierarchy, primary first):
- Walk-forward selection fixes each signal's PRIMARY horizon (S1 +21d,
  S2 +126d, S3 +126d) and varies only the remaining grid dimensions; the
  other horizons are evaluated afterwards at the selected parameters as
  robustness. Selecting horizon jointly would mix horizons along the
  stitched out-of-sample path and make the §10 bars (defined at the primary
  horizon) uninterpretable.
- Revenue SUE (§3.1 robustness) is evaluated at the modal primary-variant
  parameters only, not across the full grid.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src.backtest.events import (
    ReturnEngine, assign_buckets, bucket_summary, cohort_stats, fama_macbeth,
    hit_rate, make_folds, per_year_stats, select_grid_cell, train_test_split,
)
from src.backtest.report import write_report
from src.backtest.signals import (
    EventBuilder, EventSet, PriceTable, S2Params, S3Params, s2_prepare,
    s2_signals, s2_state_exit, s3_run,
)
from src.backtest.sim import avoidance_sim, overlay_sim, unconstrained_sim
from src.logging_setup import get_logger

logger = get_logger(__name__)

OUT_DIR = Path("data/backtest/report")
CACHE_DIR = OUT_DIR / "cache"

HORIZONS = (21, 42, 63, 126, 189, 252)
S1_PRIMARY_H = 21
S2_PRIMARY_H = 126
S3_PRIMARY_H = 126

S1_THRESHOLD_GRID = (0.0, 1.0, 2.0)
S2_REV_GRID = (0.0, 0.05, 0.10)
S2_OPM_GRID = (0.0, 100.0, 200.0)
S2_K_GRID = (1, 2, 3)
S3_C_GRID = (2, 3)
S3_THETA_GRID = (200.0, 300.0, 500.0)

US_EVAL_START = pd.Timestamp("2010-01-01")  # fold train windows start here
US_OOS_START_YEAR = 2015
INDIA_SIM_START = pd.Timestamp("2024-07-01")
US_SIM_START = pd.Timestamp("2015-01-01")
US_SIM_END = pd.Timestamp("2025-12-31")


# ------------------------------------------------------------------ market run


@dataclass
class MarketRun:
    market: str
    event_set: EventSet
    events: pd.DataFrame  # eval window, returns attached, s2-prepared
    engine: ReturnEngine
    price_fn: Callable[[str], Optional[PriceTable]]
    n_buckets: int
    sectors: Dict[str, str]
    results: Dict[str, object] = field(default_factory=dict)


def price_fn_for(builder: EventBuilder) -> Callable[[str], Optional[PriceTable]]:
    return builder.price_table


def build_market(market: str, use_scraped: bool = False,
                 rebuild: bool = False) -> MarketRun:
    """Builds (or loads from cache) the event set + return-attached events."""
    tag = "india_scraped" if use_scraped else market
    cache = CACHE_DIR / f"{tag}_events.pkl"
    builder = EventBuilder(market, india_use_scraped_dates=use_scraped)
    price_fn = price_fn_for(builder)
    if cache.exists() and not rebuild:
        with cache.open("rb") as fh:
            payload = pickle.load(fh)
        event_set = payload["event_set"]
        events = payload["events"]
        engine = ReturnEngine(event_set)
        logger.info("%s: loaded %d events from cache", tag, len(events))
    else:
        event_set = builder.build()
        engine = ReturnEngine(event_set)
        events = engine.attach(event_set.events, HORIZONS, price_fn).events
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with cache.open("wb") as fh:
            pickle.dump({"event_set": event_set, "events": events}, fh)
    # S2 helper columns (persist flags, z-scores) merged onto the return frame.
    prepared = s2_prepare(event_set)
    extra_cols = ["symbol", "period_end"] + [
        c for c in prepared.columns
        if c.startswith(("persist_", "s2_z", "s2_score_v2", "s2_v2"))
    ]
    events = events.merge(prepared[extra_cols], on=["symbol", "period_end"], how="left")
    sectors = {s: f.sector.iloc[0] for s, f in event_set.series.items()}
    n_buckets = 10 if market == "us" else 5
    return MarketRun(market, event_set, events, engine, price_fn, n_buckets, sectors)


# --------------------------------------------------------------------- S1


S1_VARIANTS = ("ts", "ts_drift", "cs")


def s1_score_col(variant: str, m: Optional[int]) -> str:
    if variant == "ts":
        return f"sue_ts_m{m}"
    if variant == "ts_drift":
        return f"sue_ts_drift_m{m}"
    return "sue_cs"


def s1_grid(variant: str) -> List[dict]:
    if variant in ("ts", "ts_drift"):
        return [{"m": m, "thr": thr} for m in (4, 6, 8) for thr in S1_THRESHOLD_GRID]
    return [{"thr": thr} for thr in S1_THRESHOLD_GRID]


def s1_masks(events: pd.DataFrame, variant: str, cell: dict,
             n_buckets: int):
    m = cell.get("m")
    score = s1_score_col(variant, m)
    # Buckets are a within-cohort ranking of the frame at hand — never cached
    # across frames (train/test slices carry different cohorts).
    buckets = assign_buckets(events, score, n_buckets)
    thr = cell["thr"]
    top = (buckets == 0) & (events[score] >= thr)
    bottom = (buckets == n_buckets - 1) & (events[score] <= -thr)
    return top, bottom, buckets


def s1_spread_fm(events: pd.DataFrame, variant: str, cell: dict, n_buckets: int,
                 horizon: int) -> float:
    top, bottom, _ = s1_masks(events, variant, cell, n_buckets)
    col = f"ex_net_{horizon}"
    spreads = []
    for _, cohort in events.groupby("cohort"):
        t = cohort[top.loc[cohort.index].values][col].mean()
        b = cohort[bottom.loc[cohort.index].values][col].mean()
        if np.isfinite(t) and np.isfinite(b):
            spreads.append(t - b)
    t_stat, _ = fama_macbeth(pd.Series(spreads))
    return t_stat


def s1_headline(events: pd.DataFrame, n_buckets: int, horizon: int) -> dict:
    """Headline stats over a pooled (already filtered) top/bottom frame."""
    col = f"ex_net_{horizon}"
    out = {"horizon": horizon}
    spreads, top_means = [], []
    for _, cohort in events.groupby("cohort"):
        t = cohort[cohort.side == "top"][col].mean()
        b = cohort[cohort.side == "bottom"][col].mean()
        if np.isfinite(t) and np.isfinite(b):
            spreads.append(t - b)
            top_means.append(t)
    t_stat, n_cohorts = fama_macbeth(pd.Series(spreads))
    top = events[events.side == "top"]
    out.update({
        "spread_mean": float(np.mean(spreads)) if spreads else np.nan,
        "spread_fm_t": t_stat,
        "n_cohorts": n_cohorts,
        "top_mean": float(top[col].mean()) if len(top) else np.nan,
        "top_hit_rate": hit_rate(top, col),
        "n_top": int(len(top)),
        "n_bottom": int((events.side == "bottom").sum()),
    })
    per_year = top.groupby(top.availability.dt.year)[col].mean()
    out["top_per_year"] = per_year
    out["top_positive_years_share"] = float((per_year > 0).mean()) if len(per_year) else np.nan
    out["per_year_spread"] = events.groupby(events.availability.dt.year).apply(
        lambda g: g[g.side == "top"][col].mean() - g[g.side == "bottom"][col].mean()
    )
    return out


def s1_evaluate(run: MarketRun, frozen: Optional[Dict[str, dict]] = None) -> dict:
    """Walk-forward (US) or frozen (India) evaluation of all S1 variants."""
    primary = "ts" if run.market == "us" else "cs"
    events = run.events
    out: Dict[str, object] = {"primary_variant": primary, "variants": {}}
    folds = make_folds() if run.market == "us" else []
    winners_by_variant: Dict[str, List[dict]] = {v: [] for v in S1_VARIANTS}

    for variant in S1_VARIANTS:
        grid = s1_grid(variant)
        oos_frames: List[pd.DataFrame] = []
        fold_rows: List[dict] = []
        grid_tables: List[pd.DataFrame] = []

        def metric(train: pd.DataFrame, cell: dict, _v=variant) -> float:
            return s1_spread_fm(train, _v, cell, run.n_buckets, S1_PRIMARY_H)

        if folds:
            for fold in folds:
                train, test = train_test_split(events, fold)
                winner, table = select_grid_cell(
                    train, grid, metric,
                    tie_break=lambda r: (r["thr"], r.get("m", 0)),
                )
                winners_by_variant[variant].append(winner)
                fold_rows.append({"test_year": fold.test_year, **winner})
                table["test_year"] = fold.test_year
                grid_tables.append(table)
                top, bottom, _ = s1_masks(test, variant, winner, run.n_buckets)
                oos_frames.append(pd.concat([
                    test[top].assign(side="top", fold=fold.test_year),
                    test[bottom].assign(side="bottom", fold=fold.test_year),
                ]))
        else:
            cell = (frozen or {})[variant]
            top, bottom, _ = s1_masks(events, variant, cell, run.n_buckets)
            oos_frames.append(pd.concat([
                events[top].assign(side="top"), events[bottom].assign(side="bottom"),
            ]))
            fold_rows.append({"test_year": "frozen", **cell})

        pooled = pd.concat(oos_frames, ignore_index=True) if oos_frames else pd.DataFrame()
        headline = s1_headline(pooled, run.n_buckets, S1_PRIMARY_H) if len(pooled) else {}
        robustness = {}
        for h in (42, 63):
            robustness[h] = s1_headline(pooled, run.n_buckets, h) if len(pooled) else {}
        out["variants"][variant] = {
            "folds": fold_rows,
            "modal": _modal_cell(winners_by_variant[variant]) if folds else (frozen or {})[variant],
            "headline": headline,
            "robustness": robustness,
            "grid_tables": pd.concat(grid_tables) if grid_tables else pd.DataFrame(),
            "pooled": pooled,
        }
    out["frozen"] = {v: out["variants"][v]["modal"] for v in S1_VARIANTS}
    return out


def _modal_cell(cells: List[dict]) -> dict:
    if not cells:
        return {}
    counts = pd.Series([json.dumps(c, sort_keys=True) for c in cells]).value_counts()
    top = counts[counts == counts.max()]
    chosen = sorted(top.index)[0]  # deterministic tie → simplest (json order)
    return json.loads(chosen)


# --------------------------------------------------------------------- S2


def s2_grid(variant: str) -> List[dict]:
    if variant == "V1":
        return [
            {"theta_rev": tr, "theta_opm_bps": to, "persistence": k}
            for tr in S2_REV_GRID for to in S2_OPM_GRID for k in S2_K_GRID
        ]
    return [{"persistence": k} for k in S2_K_GRID]


def s2_params(cell: dict, variant: str) -> S2Params:
    return S2Params(
        theta_rev=cell.get("theta_rev", 0.0),
        theta_opm_bps=cell.get("theta_opm_bps", 0.0),
        persistence=cell["persistence"],
        variant=variant,
    )


def s2_cohort_fm(events: pd.DataFrame, variant: str, cell: dict,
                 horizon: int) -> float:
    firing = s2_signals(events, s2_params(cell, variant))
    if firing.empty:
        return float("nan")
    means = cohort_stats(firing, f"ex_net_{horizon}").cohort_mean
    t_stat, _ = fama_macbeth(means)
    return t_stat


def s2_headline(firing: pd.DataFrame, horizon: int) -> dict:
    col = f"ex_net_{horizon}"
    means = cohort_stats(firing, col).cohort_mean if len(firing) else pd.Series(dtype=float)
    t_stat, n_cohorts = fama_macbeth(means)
    per_year = firing.groupby(firing.availability.dt.year)[col].mean() if len(firing) else pd.Series(dtype=float)
    return {
        "horizon": horizon,
        "mean": float(firing[col].mean()) if len(firing) else np.nan,
        "fm_t": t_stat,
        "n_cohorts": n_cohorts,
        "n_events": int(len(firing)),
        "hit_rate": hit_rate(firing, col),
        "per_year": per_year,
        "positive_years_share": float((per_year > 0).mean()) if len(per_year) else np.nan,
    }


def s2_state_returns(run: MarketRun, firing: pd.DataFrame, params: S2Params,
                     cap: int = 252) -> pd.Series:
    """Net excess for state-based exits (§4.3) per firing event."""
    bench = run.engine._calendar()
    values = {}
    for event in firing.itertuples():
        table = run.price_fn(event.symbol)
        if table is None or event.entry_pos < 0:
            continue
        series_row = run.event_set.series[event.symbol].iloc[int(event.series_idx)]
        exit_date, days = s2_state_exit(run.event_set, params, series_row, cap=cap)
        end_pos = min(int(event.entry_pos) + days, len(table.close) - 1)
        if end_pos <= int(event.entry_pos):
            continue
        r_i = table.close[end_pos] / table.close[int(event.entry_pos)] - 1.0
        start_date = pd.Timestamp(table.dates[int(event.entry_pos)])
        end_date = pd.Timestamp(table.dates[end_pos])
        b0, b1 = bench.price_at_or_before(start_date), bench.price_at_or_before(end_date)
        if b0 is None or b1 is None:
            continue
        values[event.Index] = (r_i - (b1 / b0 - 1.0)) - run.engine.cost
    return pd.Series(values)


def s2_evaluate(run: MarketRun, frozen: Optional[Dict[str, dict]] = None) -> dict:
    events = run.events
    folds = make_folds() if run.market == "us" else []
    out: Dict[str, object] = {"variants": {}}
    for variant in ("V1", "V2"):
        grid = s2_grid(variant)
        oos_frames: List[pd.DataFrame] = []
        fold_rows: List[dict] = []
        winners: List[dict] = []
        grid_tables: List[pd.DataFrame] = []

        def metric(train: pd.DataFrame, cell: dict, _v=variant) -> float:
            return s2_cohort_fm(train, _v, cell, S2_PRIMARY_H)

        if folds:
            for fold in folds:
                train, test = train_test_split(events, fold)
                winner, table = select_grid_cell(
                    train, grid, metric,
                    tie_break=lambda r: (r["persistence"], r.get("theta_rev", 0),
                                         r.get("theta_opm_bps", 0)),
                )
                winners.append(winner)
                fold_rows.append({"test_year": fold.test_year, **winner})
                table["test_year"] = fold.test_year
                grid_tables.append(table)
                oos_frames.append(s2_signals(test, s2_params(winner, variant)).assign(fold=fold.test_year))
            modal = _modal_cell(winners)
        else:
            modal = (frozen or {})[variant]
            fold_rows.append({"test_year": "frozen", **modal})
            oos_frames.append(s2_signals(events, s2_params(modal, variant)))

        pooled = pd.concat(oos_frames, ignore_index=True) if oos_frames else pd.DataFrame()
        headline = s2_headline(pooled, S2_PRIMARY_H) if len(pooled) else {}
        robustness = {h: s2_headline(pooled, h) for h in (63, 189)} if len(pooled) else {}
        params = s2_params(modal, variant)
        state_rets = s2_state_returns(run, pooled, params) if len(pooled) else pd.Series(dtype=float)
        time_rets = pooled[f"ex_net_{S2_PRIMARY_H}"] if len(pooled) else pd.Series(dtype=float)
        monotonicity = None
        if variant == "V2":
            scored = events.dropna(subset=["s2_score_v2"])
            buckets = assign_buckets(scored, "s2_score_v2", 5)
            monotonicity = bucket_summary(
                scored.assign(bucket=buckets), "bucket", f"ex_net_{S2_PRIMARY_H}"
            )
        out["variants"][variant] = {
            "folds": fold_rows,
            "modal": modal,
            "headline": headline,
            "robustness": robustness,
            "grid_tables": pd.concat(grid_tables) if grid_tables else pd.DataFrame(),
            "pooled": pooled,
            "state_exit_mean": float(state_rets.mean()) if len(state_rets) else np.nan,
            "state_exit_n": int(len(state_rets)),
            "time_exit_mean": float(time_rets.mean()) if len(time_rets) else np.nan,
            "state_beats_time": bool(
                len(state_rets) and state_rets.mean() > time_rets.mean()
            ),
            "monotonicity": monotonicity,
        }
    out["frozen"] = {v: out["variants"][v]["modal"] for v in ("V1", "V2")}
    return out


# --------------------------------------------------------------------- S3


def s3_grid() -> List[dict]:
    return [
        {"consecutive_declines": c, "margin_compression_bps": tm,
         "cumulative_c2": cum}
        for c in S3_C_GRID for tm in S3_THETA_GRID for cum in (False, True)
    ]


def s3_params(cell: dict) -> S3Params:
    return S3Params(
        consecutive_declines=cell["consecutive_declines"],
        margin_compression_bps=cell["margin_compression_bps"],
        cumulative_c2=cell["cumulative_c2"],
    )


def s3_evaluate(run: MarketRun, frozen: Optional[dict] = None) -> dict:
    events = run.events
    folds = make_folds() if run.market == "us" else []
    grid = s3_grid()
    cell_events: Dict[str, pd.DataFrame] = {}
    for cell in grid:
        key = json.dumps(cell, sort_keys=True)
        confirmed, _ = s3_run(run.event_set, s3_params(cell))
        if not confirmed.empty:
            confirmed = run.engine.attach(confirmed, HORIZONS, run.price_fn).events
        cell_events[key] = confirmed

    def metric(train_avail_max: pd.Timestamp, cell: dict) -> float:
        confirmed = cell_events[json.dumps(cell, sort_keys=True)]
        if confirmed.empty:
            return float("nan")
        subset = confirmed[confirmed.availability <= train_avail_max]
        if subset.empty:
            return float("nan")
        means = cohort_stats(subset, f"ex_net_{S3_PRIMARY_H}").cohort_mean
        t_stat, _ = fama_macbeth(means)
        return t_stat

    fold_rows: List[dict] = []
    winners: List[dict] = []
    grid_tables: List[pd.DataFrame] = []
    oos_frames: List[pd.DataFrame] = []
    if folds:
        for fold in folds:
            winner, table = select_grid_cell(
                grid, grid,
                lambda _train, cell, _f=fold: metric(_f.train_end, cell),
                tie_break=lambda r: (r["consecutive_declines"],
                                     r["margin_compression_bps"],
                                     r["cumulative_c2"]),
                maximise=False,
            )
            winners.append(winner)
            fold_rows.append({"test_year": fold.test_year, **winner})
            table["test_year"] = fold.test_year
            grid_tables.append(table)
            confirmed = cell_events[json.dumps(winner, sort_keys=True)]
            oos_frames.append(
                confirmed[confirmed.availability.dt.year == fold.test_year]
                .assign(fold=fold.test_year)
            )
        modal = _modal_cell(winners)
    else:
        modal = frozen or {}
        fold_rows.append({"test_year": "frozen", **modal})
        oos_frames.append(cell_events[json.dumps(modal, sort_keys=True)])

    pooled = pd.concat(oos_frames, ignore_index=True) if oos_frames else pd.DataFrame()
    headline = {}
    if len(pooled):
        headline = s2_headline(pooled, S3_PRIMARY_H)  # same shape: mean/FM/per-year
        headline["robustness"] = {h: s2_headline(pooled, h) for h in (63, 189, 252)}
    avoidance = avoidance_sim(run.event_set, pooled, run.price_fn) if len(pooled) else pd.DataFrame()
    cried_wolf = float(avoidance.cried_wolf.mean()) if len(avoidance) else np.nan
    avoid_year = avoidance.groupby("year").avoidance_benefit.mean() if len(avoidance) else pd.Series(dtype=float)
    return {
        "folds": fold_rows,
        "modal": modal,
        "headline": headline,
        "grid_tables": pd.concat(grid_tables) if grid_tables else pd.DataFrame(),
        "pooled": pooled,
        "avoidance": avoidance,
        "avoidance_mean": float(avoidance.avoidance_benefit.mean()) if len(avoidance) else np.nan,
        "avoidance_per_year": avoid_year,
        "avoidance_positive_years": float((avoid_year > 0).mean()) if len(avoid_year) else np.nan,
        "cried_wolf_rate": cried_wolf,
        "n_censored": int(avoidance.censored.sum()) if len(avoidance) else 0,
    }


# -------------------------------------------------------------------- sims


def window_active_fn(score_col: str, horizon: int, price_fn):
    """Active-signal mask: entry <= day and fewer than h trading days since entry."""
    def fn(events: pd.DataFrame, day: pd.Timestamp) -> pd.Series:
        mask = pd.Series(False, index=events.index)
        eligible = events[events.entry_date <= day]
        for symbol, group in eligible.groupby("symbol"):
            table = price_fn(symbol)
            if table is None:
                continue
            pos_day = int(np.searchsorted(table.dates, np.datetime64(day), side="right")) - 1
            held = pos_day - group.entry_pos.to_numpy(dtype=int)
            mask.loc[group.index[(held >= 0) & (held < horizon)]] = True
        return mask
    return fn


def state_active_fn(exits: Dict[int, Tuple[pd.Timestamp, int]]):
    """Active while the S2 combination holds (entry <= day < state exit)."""
    def fn(events: pd.DataFrame, day: pd.Timestamp) -> pd.Series:
        mask = pd.Series(False, index=events.index)
        for idx, (exit_date, days) in exits.items():
            if events.loc[idx, "entry_date"] <= day and (pd.isna(exit_date) or day < exit_date):
                mask.loc[idx] = True
        return mask
    return fn


def refill_score_fn_factory(events: pd.DataFrame, score_col: str = "sue_cs"):
    by_symbol = {
        s: g.sort_values("availability")[["availability", score_col]].dropna()
        for s, g in events.groupby("symbol")
    }

    def fn(symbol: str, day: pd.Timestamp) -> float:
        frame = by_symbol.get(symbol)
        if frame is None:
            return float("nan")
        past = frame[frame.availability < day][score_col]
        return float(past.iloc[-1]) if len(past) else float("nan")

    return fn


def run_sims(run: MarketRun, s1: dict, s2: dict, s3: dict) -> dict:
    market = run.market
    start = US_SIM_START if market == "us" else INDIA_SIM_START
    end = US_SIM_END if market == "us" else None
    if end is None:
        from src.backtest.signals import EventBuilder as _EB

        cal = _EB(market, store=None).calendar()
        end = pd.Timestamp(cal.dates[-1])
    results: Dict[str, object] = {}

    # --- S1: top-bucket long book (modal primary-variant params).
    variant = s1["primary_variant"]
    modal = s1["variants"][variant]["modal"]
    pooled = s1["variants"][variant]["pooled"]
    top = pooled[pooled.side == "top"].copy()
    top["signal_score"] = top[s1_score_col(variant, modal.get("m"))]
    if len(top):
        active = window_active_fn("signal_score", S1_PRIMARY_H, run.price_fn)
        results["s1_unconstrained"] = unconstrained_sim(
            run.event_set, top, active, start, end, run.price_fn, name="s1_unconstrained")
        results["s1_overlay"], results["s1_overlay_baseline"] = overlay_sim(
            run.event_set, top, None, active, start, end, run.price_fn,
            run.sectors, refill_score_fn_factory(run.events), name="s1_overlay")

    # --- S2: V1 firing cohort, state-based activity (production variant, §4.3).
    modal2 = s2["variants"]["V1"]["modal"]
    firing = s2["variants"]["V1"]["pooled"].copy()
    if len(firing):
        firing["signal_score"] = firing["s2_score"]
        params2 = s2_params(modal2, "V1")
        exits = {}
        for event in firing.itertuples():
            row = run.event_set.series[event.symbol].iloc[int(event.series_idx)]
            exits[event.Index] = s2_state_exit(run.event_set, params2, row)
        active2 = state_active_fn(exits)
        results["s2_unconstrained"] = unconstrained_sim(
            run.event_set, firing, active2, start, end, run.price_fn, name="s2_unconstrained")
        results["s2_overlay"], results["s2_overlay_baseline"] = overlay_sim(
            run.event_set, firing, None, active2, start, end, run.price_fn,
            run.sectors, refill_score_fn_factory(run.events), name="s2_overlay")

    # --- S3: CONFIRMED exits + refill (no buy-side signals in this book).
    confirmed = s3["pooled"].copy()
    if len(confirmed):
        results["s3_overlay"], results["s3_overlay_baseline"] = overlay_sim(
            run.event_set, None, confirmed, None, start, end, run.price_fn,
            run.sectors, refill_score_fn_factory(run.events), name="s3_overlay")
    return results


# ------------------------------------------------------- scraped-date check


def scraped_sensitivity(primary: MarketRun, scraped: MarketRun, s1_frozen: dict) -> dict:
    """§1.2 side-by-side: uniform 45/60 assumption vs scraped actual dates."""
    scraped_events = scraped.events[scraped.events.availability_basis == "scraped"].copy()
    late = scraped_events[scraped_events.scraped_late]
    out = {
        "n_scraped": int(len(scraped_events)),
        "n_late": int(len(late)),
        "late_share": float(len(late) / len(scraped_events)) if len(scraped_events) else np.nan,
    }
    key = primary.events.merge(
        scraped_events[["symbol", "period_end"]], on=["symbol", "period_end"]
    )
    cell = s1_frozen["cs"]
    for label, frame in (("assumed", key), ("scraped", scraped_events)):
        if not len(frame):
            continue
        buckets = assign_buckets(frame, "sue_cs", primary.n_buckets)
        top = frame[(buckets == 0) & (frame.sue_cs >= cell["thr"])]
        means = cohort_stats(top, f"ex_net_{S1_PRIMARY_H}").cohort_mean if len(top) else pd.Series(dtype=float)
        t_stat, _ = fama_macbeth(means)
        out[f"s1_cs_top_{label}"] = {
            "n": int(len(top)),
            "mean": float(top[f"ex_net_{S1_PRIMARY_H}"].mean()) if len(top) else np.nan,
            "fm_t": t_stat,
        }
    key_nolate = key.merge(
        late[["symbol", "period_end"]], on=["symbol", "period_end"], how="left",
        indicator=True,
    )
    kept = key_nolate[key_nolate._merge == "left_only"].drop(columns="_merge")
    if len(kept):
        buckets = assign_buckets(kept, "sue_cs", primary.n_buckets)
        top = kept[(buckets == 0) & (kept.sue_cs >= cell["thr"])]
        out["s1_cs_top_assumed_excl_late"] = {
            "n": int(len(top)),
            "mean": float(top[f"ex_net_{S1_PRIMARY_H}"].mean()) if len(top) else np.nan,
        }
    return out


# -------------------------------------------------------------------- main


def run_market(market: str, frozen: Optional[dict] = None,
               rebuild: bool = False) -> Tuple[MarketRun, dict]:
    run = build_market(market, rebuild=rebuild)
    if market == "us":
        run.events = run.events[run.events.availability >= US_EVAL_START].reset_index(drop=True)

    s1 = s1_evaluate(run, (frozen or {}).get("s1"))
    s2 = s2_evaluate(run, (frozen or {}).get("s2"))
    s3 = s3_evaluate(run, (frozen or {}).get("s3"))
    sims = run_sims(run, s1, s2, s3)

    frozen_out = {
        "s1": s1["frozen"], "s2": s2["frozen"], "s3": s3["modal"],
    }
    run.results = {"s1": s1, "s2": s2, "s3": s3, "sims": sims, "frozen": frozen_out}
    return run, frozen_out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--us", action="store_true", help="run the US leg only")
    parser.add_argument("--india", action="store_true", help="run the India leg only")
    parser.add_argument("--rebuild", action="store_true", help="ignore cached event tables")
    parser.add_argument("--out", default=str(OUT_DIR))
    args = parser.parse_args()
    both = not (args.us or args.india)

    runs: Dict[str, MarketRun] = {}
    frozen: Optional[dict] = None
    if args.us or both:
        runs["us"], frozen = run_market("us", rebuild=args.rebuild)
    if args.india or both:
        india_run, _ = run_market("india", frozen=frozen, rebuild=args.rebuild)
        runs["india"] = india_run
        scraped_run = build_market("india", use_scraped=True, rebuild=args.rebuild)
        runs["india"].results["scraped"] = scraped_sensitivity(
            india_run, scraped_run, (frozen or runs["india"].results["frozen"])["s1"]
        )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_report(out, runs)
    logger.info("report written to %s", out / "report.md")


if __name__ == "__main__":
    main()
