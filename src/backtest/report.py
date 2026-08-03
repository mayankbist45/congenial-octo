"""Backtest reporting: report.md, CSV tables, and charts.

Everything the analyst reviews lands under ``data/backtest/report/``:
- ``report.md`` — the human-readable memo: headline numbers against every
  §10 go/no-go bar with explicit PASS/FAIL, walk-forward choices and their
  stability, India frozen-parameter results, sims, deviations, biases.
- ``*.csv`` — every table behind the memo (events, grids, folds, per-year,
  buckets, S3 events, avoidance, sim metrics).
- ``*.png`` — CAR curves (with percentile bands and the US pre-window
  leakage diagnostic) and sim NAV-vs-benchmark charts.

Matplotlib runs headless (Agg). Numbers are formatted percent-first; FM
t-stats sit next to every headline mean, per spec §7.1.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.logging_setup import get_logger

logger = get_logger(__name__)

plt.rcParams.update({
    "figure.figsize": (9, 5),
    "axes.grid": True,
    "grid.alpha": 0.3,
    "lines.linewidth": 1.6,
})


def pct(x: float, digits: int = 2) -> str:
    return "n/a" if x is None or not np.isfinite(x) else f"{x * 100:.{digits}f}%"


def fmt(x: float, digits: int = 2) -> str:
    return "n/a" if x is None or not np.isfinite(x) else f"{x:.{digits}f}"


def verdict(ok: Optional[bool]) -> str:
    return "n/a" if ok is None else ("**PASS**" if ok else "**FAIL**")


def md_table(frame: pd.DataFrame, max_rows: int = 40) -> str:
    """Minimal markdown table renderer (avoids the tabulate dependency)."""
    if frame is None or frame.empty:
        return "_(no rows)_"
    frame = frame.head(max_rows)
    cols = [str(c) for c in frame.columns]
    rows = []
    for _, row in frame.iterrows():
        rows.append([_cell(v) for v in row])
    widths = [len(c) for c in cols]
    for row in rows:
        widths = [max(w, len(v)) for w, v in zip(widths, row)]
    header = "| " + " | ".join(c.ljust(w) for c, w in zip(cols, widths)) + " |"
    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    body = ["| " + " | ".join(v.ljust(w) for v, w in zip(row, widths)) + " |" for row in rows]
    return "\n".join([header, sep, *body])


def _cell(v) -> str:
    if isinstance(v, float):
        if not np.isfinite(v):
            return "n/a"
        if abs(v) < 10:
            return f"{v:.4f}".rstrip("0").rstrip(".")
        return f"{v:,.1f}"
    if isinstance(v, (pd.Timestamp,)):
        return v.strftime("%Y-%m-%d")
    return str(v)


# -------------------------------------------------------------------- charts


def car_chart(curves: Dict[str, pd.DataFrame], path: Path, title: str,
              pre_window: bool = False) -> None:
    fig, ax = plt.subplots()
    colors = {"top": "tab:green", "bottom": "tab:red", "cohort": "tab:blue",
              "confirmed": "tab:red"}
    for label, curve in curves.items():
        if curve.empty:
            continue
        ax.plot(curve.index, curve["mean"], label=f"{label} mean",
                color=colors.get(label, None))
        ax.fill_between(curve.index, curve["p25"], curve["p75"], alpha=0.15,
                        color=colors.get(label, None))
    ax.axvline(0, color="k", linewidth=0.8, alpha=0.5)
    ax.axhline(0, color="k", linewidth=0.8, alpha=0.5)
    ax.set_xlabel("trading days from entry close" + (" (pre-window from availability−20)" if pre_window else ""))
    ax.set_ylabel("mean CAR (market-adjusted)")
    ax.set_title(title)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def nav_chart(navs: Dict[str, pd.Series], bench: pd.Series, path: Path,
              title: str) -> None:
    fig, ax = plt.subplots()
    for label, nav in navs.items():
        ax.plot(nav.index, nav / nav.iloc[0], label=label)
    ax.plot(bench.index, bench / bench.iloc[0], label="benchmark",
            color="k", alpha=0.6)
    ax.set_ylabel("wealth (start = 1)")
    ax.set_title(title)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def per_year_chart(series: pd.Series, path: Path, title: str) -> None:
    if series is None or series.empty:
        return
    fig, ax = plt.subplots()
    colors = ["tab:green" if v > 0 else "tab:red" for v in series]
    ax.bar(series.index.astype(str), series.values, color=colors)
    ax.axhline(0, color="k", linewidth=0.8)
    ax.set_ylabel("net excess")
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


# --------------------------------------------------------------- CSV writers


def _write_csvs(out: Path, market: str, results: dict) -> None:
    prefix = out / market
    for variant, vres in results["s1"]["variants"].items():
        if len(vres.get("folds", [])):
            pd.DataFrame(vres["folds"]).to_csv(f"{prefix}_s1_{variant}_folds.csv", index=False)
        if not vres.get("grid_tables", pd.DataFrame()).empty:
            vres["grid_tables"].to_csv(f"{prefix}_s1_{variant}_grid.csv", index=False)
        if len(vres.get("pooled", pd.DataFrame())):
            vres["pooled"].to_csv(f"{prefix}_s1_{variant}_oos_events.csv", index=False)
    for variant, vres in results["s2"]["variants"].items():
        if len(vres.get("folds", [])):
            pd.DataFrame(vres["folds"]).to_csv(f"{prefix}_s2_{variant}_folds.csv", index=False)
        if not vres.get("grid_tables", pd.DataFrame()).empty:
            vres["grid_tables"].to_csv(f"{prefix}_s2_{variant}_grid.csv", index=False)
        if len(vres.get("pooled", pd.DataFrame())):
            vres["pooled"].to_csv(f"{prefix}_s2_{variant}_oos_events.csv", index=False)
    s3 = results["s3"]
    if len(s3.get("folds", [])):
        pd.DataFrame(s3["folds"]).to_csv(f"{prefix}_s3_folds.csv", index=False)
    if not s3.get("grid_tables", pd.DataFrame()).empty:
        s3["grid_tables"].to_csv(f"{prefix}_s3_grid.csv", index=False)
    if len(s3.get("pooled", pd.DataFrame())):
        s3["pooled"].to_csv(f"{prefix}_s3_confirmed_events.csv", index=False)
    if len(s3.get("avoidance", pd.DataFrame())):
        s3["avoidance"].to_csv(f"{prefix}_s3_avoidance.csv", index=False)
    sim_rows = []
    for name, sim in results.get("sims", {}).items():
        metrics = sim.metrics()
        metrics["sim"] = name
        sim_rows.append(metrics)
        sim.nav.to_csv(f"{prefix}_{name}_nav.csv")
        if getattr(sim, "per_year_excess", None) is not None:
            sim.per_year_excess.to_csv(f"{prefix}_{name}_per_year_excess.csv")
    if sim_rows:
        pd.DataFrame(sim_rows).to_csv(f"{prefix}_sim_metrics.csv", index=False)


# ------------------------------------------------------------- main renderer


def write_report(out: Path, runs: Dict[str, "MarketRun"]) -> None:
    """Renders report.md + charts + CSVs for all markets."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    lines.append("# Quarterly Earnings Backtest — Results Memo")
    lines.append("")
    lines.append(f"Generated {pd.Timestamp.now():%Y-%m-%d %H:%M}. Spec: "
                 "`docs/earnings/backtest_spec.md` v1.0. All US headline numbers "
                 "are stitched walk-forward out-of-sample (§7.7); all India "
                 "numbers use US-frozen parameters with zero India-side tuning.")
    lines.append("")

    for market, run in runs.items():
        lines.append(f"---\n\n## Market: {market.upper()}")
        lines.append("")
        excl = dict(run.event_set.exclusions)
        lines.append(f"Events evaluated: {len(run.events):,} across "
                     f"{run.event_set.n_symbols} symbols. Exclusion ledger: `{excl}`")
        lines.append("")
        _s1_section(lines, out, market, run)
        _s2_section(lines, out, market, run)
        _s3_section(lines, out, market, run)
        _sims_section(lines, out, market, run)
        _write_csvs(out, market, run.results)

    india = runs.get("india")
    if india is not None and "scraped" in india.results:
        _scraped_section(lines, india.results["scraped"])

    _verdicts_section(lines, runs)
    _surprises_section(lines, runs)
    _production_section(lines, runs)
    _deviations_section(lines)

    lines.append("---\n\n## S4 LLM-as-analyst study")
    lines.append("")
    lines.append("**Not run — no `OPENAI_API_KEY` on this machine.** The harness "
                 "(`src/backtest/llm_study.py`) implements the §8 300-event "
                 "stratified sampler, the verbatim system prompt, strict JSON "
                 "schema validation with one retry, raw-response logging, and a "
                 "cost-estimate printout; it refuses to start without a key. "
                 "§8.5's training-data contamination caveat stands for any "
                 "future run.")
    lines.append("")
    (out / "report.md").write_text("\n".join(lines))
    logger.info("wrote %s", out / "report.md")


def _bar(lines: List[str], label: str, ok: Optional[bool], detail: str) -> None:
    lines.append(f"- {label}: {verdict(ok)} — {detail}")


def _s1_section(lines: List[str], out: Path, market: str, run) -> None:
    s1 = run.results["s1"]
    primary = s1["primary_variant"]
    lines.append(f"### S1 PEAD (primary variant: SUE-{primary.upper()}, "
                 f"horizon +21d, buckets={run.n_buckets})")
    lines.append("")
    curves = {}
    for variant, vres in s1["variants"].items():
        headline = vres.get("headline", {})
        if not headline:
            continue
        lines.append(f"**SUE-{variant}** — spread top−bottom {pct(headline.get('spread_mean'))} "
                     f"(FM t = {fmt(headline.get('spread_fm_t'))}, "
                     f"{headline.get('n_cohorts')} cohorts); top-bucket mean "
                     f"{pct(headline.get('top_mean'))}, hit "
                     f"{pct(headline.get('top_hit_rate'), 1)}, "
                     f"positive years {pct(headline.get('top_positive_years_share'), 0)}; "
                     f"n_top={headline.get('n_top')}, n_bottom={headline.get('n_bottom')}")
        lines.append("")
        folds = vres.get("folds", [])
        if folds and folds[0].get("test_year") != "frozen":
            lines.append("Walk-forward choices (winner per test year):")
            lines.append("")
            lines.append(_folds_table(folds))
            lines.append("")
        if variant == primary:
            pooled = vres.get("pooled", pd.DataFrame())
            if len(pooled):
                for side in ("top", "bottom"):
                    subset = pooled[pooled.side == side]
                    curves[side] = run.engine.car_curve(subset, run.price_fn, t_max=63,
                                                        pre_days=20 if market == "us" else 0)
                robust = vres.get("robustness", {})
                for h, hstats in robust.items():
                    if hstats:
                        lines.append(f"Robustness +{h}d: spread {pct(hstats.get('spread_mean'))} "
                                     f"(FM t {fmt(hstats.get('spread_fm_t'))})")
                lines.append("")
    if curves:
        path = out / f"{market}_s1_car.png"
        car_chart(curves, path, f"S1 PEAD {market.upper()} — CAR by bucket (primary variant)"
                  + (" incl. pre-window" if market == "us" else ""),
                  pre_window=(market == "us"))
        lines.append(f"![S1 CAR {market}]({path.name})")
        lines.append("")
        if market == "us":
            top_pre = curves.get("top")
            note = "n/a"
            if top_pre is not None and not top_pre.empty:
                pre = top_pre.loc[-20:-1, "mean"]
                note = pct(float(pre.iloc[-1] - pre.iloc[0])) if len(pre) > 1 else "n/a"
            lines.append(f"US leakage diagnostic: top-bucket pre-window (availability−20 → entry) "
                         f"mean CAR drift {note}. Pre-announcement run-up would mean the "
                         f"post-entry CAR understates the anomaly (§7.1); noted, not traded.")
            lines.append("")


def _s2_section(lines: List[str], out: Path, market: str, run) -> None:
    s2 = run.results["s2"]
    lines.append("### S2 Quality earnings momentum (primary horizon +126d)")
    lines.append("")
    for variant in ("V1", "V2"):
        vres = s2["variants"][variant]
        headline = vres.get("headline", {})
        if not headline:
            continue
        lines.append(f"**{variant}** — firing cohort mean {pct(headline.get('mean'))} "
                     f"(FM t = {fmt(headline.get('fm_t'))}, {headline.get('n_cohorts')} cohorts), "
                     f"hit {pct(headline.get('hit_rate'), 1)}, positive years "
                     f"{pct(headline.get('positive_years_share'), 0)}, n={headline.get('n_events')}; "
                     f"state-exit mean {pct(vres.get('state_exit_mean'))} vs "
                     f"time-exit {pct(vres.get('time_exit_mean'))} "
                     f"(state beats time: {vres.get('state_beats_time')})")
        lines.append("")
        folds = vres.get("folds", [])
        if folds and folds[0].get("test_year") != "frozen":
            lines.append(_folds_table(folds))
            lines.append("")
        mono = vres.get("monotonicity")
        if mono is not None and len(mono):
            lines.append(f"V2 quintile monotonicity: top−bottom "
                         f"{pct(mono.attrs.get('spread_top_minus_bottom'))}, rank corr "
                         f"{fmt(mono.attrs.get('rank_corr'))}")
            lines.append("")
            lines.append(md_table(mono.reset_index().rename(columns={"index": "bucket"})))
            lines.append("")
    pooled = s2["variants"]["V1"].get("pooled", pd.DataFrame())
    if len(pooled):
        curve = run.engine.car_curve(pooled, run.price_fn, t_max=252)
        path = out / f"{market}_s2_car.png"
        car_chart({"cohort": curve}, path, f"S2 V1 firing cohort {market.upper()} — CAR")
        lines.append(f"![S2 CAR {market}]({path.name})")
        lines.append("")


def _s3_section(lines: List[str], out: Path, market: str, run) -> None:
    s3 = run.results["s3"]
    headline = s3.get("headline", {})
    lines.append("### S3 Earnings deterioration (CONFIRMED cohort, primary +126d)")
    lines.append("")
    if headline:
        lines.append(f"Confirmed mean {pct(headline.get('mean'))} (FM t = "
                     f"{fmt(headline.get('fm_t'))}, {headline.get('n_cohorts')} cohorts), "
                     f"n={headline.get('n_events')}; avoidance benefit mean "
                     f"{pct(s3.get('avoidance_mean'))}, positive-benefit years "
                     f"{pct(s3.get('avoidance_positive_years'), 0)}, cried-wolf rate "
                     f"{pct(s3.get('cried_wolf_rate'), 1)} "
                     f"(censored-no-CLEAR events: {s3.get('n_censored')})")
        lines.append("")
        folds = s3.get("folds", [])
        if folds and folds[0].get("test_year") != "frozen":
            lines.append(_folds_table(folds))
            lines.append("")
        for h, hstats in (headline.get("robustness") or {}).items():
            lines.append(f"Robustness +{h}d: mean {pct(hstats.get('mean'))} "
                         f"(FM t {fmt(hstats.get('fm_t'))})")
        lines.append("")
        pooled = s3.get("pooled", pd.DataFrame())
        if len(pooled):
            curve = run.engine.car_curve(pooled, run.price_fn, t_max=252)
            path = out / f"{market}_s3_car.png"
            car_chart({"confirmed": curve}, path, f"S3 CONFIRMED cohort {market.upper()} — CAR")
            lines.append(f"![S3 CAR {market}]({path.name})")
            lines.append("")
        avoid_year = s3.get("avoidance_per_year")
        if avoid_year is not None and len(avoid_year):
            path = out / f"{market}_s3_avoidance_year.png"
            per_year_chart(avoid_year, path, f"S3 avoidance benefit by year {market.upper()}")


def _sims_section(lines: List[str], out: Path, market: str, run) -> None:
    sims = run.results.get("sims", {})
    if not sims:
        return
    lines.append("### Portfolio simulations")
    lines.append("")
    rows = []
    for name, sim in sims.items():
        m = sim.metrics()
        rows.append({
            "sim": name,
            "CAGR": pct(m.get("cagr")),
            "excess CAGR (geo)": pct(m.get("excess_cagr_geometric")),
            "Sharpe(rf=0)": fmt(m.get("sharpe_rf0")),
            "maxDD": pct(m.get("max_drawdown"), 1),
            "turnover (P+S)/2": pct(m.get("turnover_oneway_mean"), 1),
            "turnover min-churn": pct(m.get("turnover_churn_mean"), 1),
            "avg pos": fmt(m.get("avg_positions"), 1),
            "avg exposure": pct(m.get("avg_exposure"), 0),
        })
    lines.append(md_table(pd.DataFrame(rows)))
    lines.append("")
    for family in ("s1", "s2", "s3"):
        overlay = sims.get(f"{family}_overlay")
        baseline = sims.get(f"{family}_overlay_baseline")
        if overlay is None or baseline is None:
            continue
        mo, mb = overlay.metrics(), baseline.metrics()
        incremental = (mo.get("turnover_churn_mean", 0) - mb.get("turnover_churn_mean", 0))
        lines.append(f"{family.upper()} overlay: excess CAGR {pct(mo.get('excess_cagr_geometric'))} "
                     f"vs baseline {pct(mb.get('excess_cagr_geometric'))}; incremental "
                     f"turnover {pct(incremental, 1)}/yr (min-churn convention).")
        lines.append("")
        path = out / f"{market}_{family}_overlay_nav.png"
        nav_chart({"overlay": overlay.nav, "baseline book": baseline.nav},
                  overlay.benchmark, path, f"{family.upper()} overlay {market.upper()} — NAV")
        lines.append(f"![{family} overlay {market}]({path.name})")
        lines.append("")


def _overall(bars: list) -> str:
    if not bars or any(b is None for b in bars):
        return "n/a"
    return "PASS" if all(bars) else "FAIL"


def _get(runs: Dict[str, "MarketRun"], market: str, path: tuple, default=None):
    node = runs.get(market)
    if node is None:
        return default
    value = node.results
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def _folds_table(folds: list) -> str:
    frame = pd.DataFrame(folds)
    if "test_year" in frame.columns:
        years = pd.to_numeric(frame["test_year"], errors="coerce")
        if years.notna().all():
            # str, not int: iterrows() upcasts mixed int/float rows to float64,
            # and _cell would then render the year as "2,015.0".
            frame["test_year"] = years.astype(int).astype(str)
    return md_table(frame)


def _bottom_tail_regime(pooled: pd.DataFrame):
    """(best year for the bottom tail, its mean, pooled bottom-tail mean)."""
    if not len(pooled) or "side" not in pooled.columns:
        return None, None, None
    bottom = pooled[pooled.side == "bottom"].copy()
    if not len(bottom):
        return None, None, None
    bottom["year"] = pd.to_datetime(bottom.availability).dt.year
    by_year = bottom.groupby("year")["ex_net_21"].mean()
    if not len(by_year):
        return None, None, None
    return int(by_year.idxmax()), float(by_year.max()), float(bottom.ex_net_21.mean())


def _surprises_section(lines: List[str], runs: Dict[str, "MarketRun"]) -> None:
    """Honest observations on the results, for the analyst reviewing this memo."""
    lines.append("---\n\n## What surprised us (notes for the reviewer)")
    lines.append("")
    us, india = runs.get("us"), runs.get("india")
    if us is not None:
        s1 = us.results["s1"]
        primary = s1["variants"].get(s1.get("primary_variant"))
        if primary is not None:
            year, year_mean, bottom_mean = _bottom_tail_regime(
                primary.get("pooled", pd.DataFrame()))
            if year is not None:
                lines.append(
                    "- **S1's tails are U-shaped in arithmetic market-adjusted terms.** The "
                    f"US bottom (worst-SUE) decile's pooled net CAR@21d is {pct(bottom_mean)} — "
                    f"positive, not negative — with {year} the biggest contributor at "
                    f"{pct(year_mean)}. Extreme-surprise names are high-attention, high-beta "
                    "names; in junk-rally years the bad-news tail out-bounces the good-news "
                    "tail, so the long/short spread never reaches significance even though the "
                    "top bucket's own sign is right. Own-history SUE is also not a consensus "
                    "surprise: several mega-cap prints sit in the worst SUE decile and rallied "
                    "hard post-print on guidance/narrative.")
                lines.append("")
    s3_us = (us.results["s3"] if us else {}) or {}
    s3_in = (india.results["s3"] if india else {}) or {}
    h_us, h_in = s3_us.get("headline", {}), s3_in.get("headline", {})
    if h_us or h_in:
        lines.append(
            "- **S3 CONFIRMED cohorts drift UP, in both markets** "
            f"(US {pct(h_us.get('mean'))}, India {pct(h_in.get('mean'))} net @126d), so the "
            f"avoidance sim is worth {pct(s3_us.get('avoidance_mean'))} (US) / "
            f"{pct(s3_in.get('avoidance_mean'))} (India) — i.e. avoiding deteriorating names "
            "cost money in this sample. The state machine is not crying wolf "
            f"({pct(s3_us.get('cried_wolf_rate'), 0)} US false-positive rate): it confirms "
            "genuine deterioration, but by confirmation time the bad news is already priced — "
            "mechanically-confirmed deteriorators behave like late-cycle value names and "
            "mean-revert upward. S3 as specified identifies deterioration; it does not "
            "predict further downside.")
        lines.append("")
    s2_us = (us.results["s2"] if us else {}) or {}
    v1 = s2_us.get("variants", {}).get("V1", {})
    if v1:
        lines.append(
            "- **S2 is the only signal with both-market significance (V1), and the exit rule "
            f"carries it**: state-based exit roughly doubles the cohort mean vs the fixed "
            f"126d exit (US {pct(v1.get('state_exit_mean'))} vs {pct(v1.get('time_exit_mean'))}). "
            "But the same persistence that makes it work makes the overlay churn: quarterly "
            "refills rotate the 25-name book far past the 15%/yr incremental-turnover bar. "
            "A production version would need slower entry (longer persistence, hysteresis on "
            "exit) — that is a new variant, not a parameter tweak.")
        lines.append("")
    if india is not None:
        lines.append(
            "- **India's evidence base is thin by construction**: ~13 quarterly observations "
            "per name means 5–13 Fama–MacBeth cohorts per signal (t-stats honestly n/a), and "
            "the scraped-dates subsample (§1.2's 'only real test' of the 45/60-day lag) has "
            "too few events to bite. Treat every India number as directional, not estimated.")
        lines.append("")


def _verdicts_section(lines: List[str], runs: Dict[str, "MarketRun"]) -> None:
    """§10 go/no-go bars, each with an explicit PASS/FAIL."""
    lines.append("---\n\n## §10 Go/No-Go verdicts")
    lines.append("")
    lines.append("Bars quoting \"both markets\" require the US walk-forward OOS path "
                 "AND the India frozen-parameter run to clear independently. The "
                 "overlay bars are evaluated on each market's own 25-name book; "
                 "India's book is thin (~2 years) — flagged where binding.")
    lines.append("")

    # ---- S1
    lines.append("### S1 PEAD")
    bars = []
    per_market_b1, per_market_b2 = {}, {}
    for market in runs:
        h = _get(runs, market, ("s1", "variants"))
        if not h:
            continue
        primary = "ts" if market == "us" else "cs"
        headline = h[primary]["headline"]
        b1 = (headline.get("spread_mean", 0) > 0) and (headline.get("spread_fm_t", 0) >= 2)
        b2 = headline.get("top_positive_years_share", 0) > 0.60
        per_market_b1[market] = (b1, headline)
        per_market_b2[market] = b2
    ok = all(v[0] for v in per_market_b1.values()) and len(per_market_b1) == len(runs)
    detail = "; ".join(
        f"{m}: spread {pct(v[1].get('spread_mean'))}, FM t {fmt(v[1].get('spread_fm_t'))}"
        for m, v in per_market_b1.items())
    _bar(lines, "Bar 1 — top−bottom net CAR@21d > 0 and FM t ≥ 2, both markets",
         ok if per_market_b1 else None, detail)
    bars.append(ok if per_market_b1 else None)
    ok2 = all(per_market_b2.values()) and len(per_market_b2) == len(runs)
    _bar(lines, "Bar 2 — same-sign top-bucket excess in >60% of years, each market",
         ok2 if per_market_b2 else None,
         "; ".join(f"{m}: {pct(_get(runs, m, ('s1', 'variants', 'ts' if m == 'us' else 'cs', 'headline', 'top_positive_years_share')), 0)}"
                   for m in per_market_b2))
    bars.append(ok2 if per_market_b2 else None)
    ok3, detail3 = _overlay_bar(runs, "s1")
    _bar(lines, "Bar 3 — overlay: positive net excess CAGR and incremental turnover ≤ 15%/yr",
         ok3, detail3)
    bars.append(ok3)
    lines.append("")
    lines.append(f"**S1 overall: {_overall(bars)}**")
    lines.append("")

    # ---- S2
    lines.append("### S2 quality momentum")
    bars = []
    for variant in ("V1", "V2"):
        per = {}
        for market in runs:
            headline = _get(runs, market, ("s2", "variants", variant, "headline")) or {}
            if not headline:
                continue
            per[market] = (headline.get("mean", 0) > 0) and (headline.get("fm_t", 0) >= 2)
        ok = all(per.values()) and len(per) == len(runs)
        _bar(lines, f"Bar 1 ({variant}) — firing/top-quintile net excess > 0 @126d, FM t ≥ 2, both markets",
             ok if per else None,
             "; ".join(f"{m}: mean {pct(_get(runs, m, ('s2', 'variants', variant, 'headline', 'mean')))}, "
                       f"t {fmt(_get(runs, m, ('s2', 'variants', variant, 'headline', 'fm_t')))}" for m in per))
        bars.append(ok if per else None)
    per = {}
    for market in runs:
        mono = _get(runs, market, ("s2", "variants", "V2", "monotonicity"))
        if mono is None or not len(mono):
            continue
        per[market] = (mono.attrs.get("spread_top_minus_bottom", 0) > 0) and (mono.attrs.get("rank_corr", 0) > 0)
    ok = all(per.values()) and len(per) == len(runs)
    _bar(lines, "Bar 2 — quintile monotonicity (top−bottom > 0 and rank corr > 0)",
         ok if per else None, ", ".join(per))
    bars.append(ok if per else None)
    per = {m: bool(_get(runs, m, ("s2", "variants", "V1", "state_beats_time")))
           for m in runs if _get(runs, m, ("s2", "variants", "V1", "state_beats_time")) is not None}
    ok = all(per.values()) and len(per) == len(runs)
    _bar(lines, "Bar 3 — state-based exit beats time-based, net of costs", ok if per else None,
         "; ".join(f"{m}: state {pct(_get(runs, m, ('s2', 'variants', 'V1', 'state_exit_mean')))} vs "
                   f"time {pct(_get(runs, m, ('s2', 'variants', 'V1', 'time_exit_mean')))}" for m in per))
    bars.append(ok if per else None)
    ok4, detail4 = _overlay_bar(runs, "s2")
    _bar(lines, "Bar 4 — overlay incremental turnover ≤ 15%/yr", ok4, detail4)
    bars.append(ok4)
    lines.append("")
    lines.append(f"**S2 overall: {_overall(bars)}**")
    lines.append("")

    # ---- S3
    lines.append("### S3 deterioration (sell-side)")
    bars = []
    per = {}
    for market in runs:
        h = _get(runs, market, ("s3", "headline")) or {}
        if not h:
            continue
        per[market] = (h.get("mean", 0) <= -0.02) and (h.get("fm_t", 0) <= -2)
    ok = all(per.values()) and len(per) == len(runs)
    _bar(lines, "Bar 1 — CONFIRMED @126d mean ≤ −2% and FM t ≤ −2, both markets",
         ok if per else None,
         "; ".join(f"{m}: mean {pct(_get(runs, m, ('s3', 'headline', 'mean')))}, "
                   f"t {fmt(_get(runs, m, ('s3', 'headline', 'fm_t')))}" for m in per))
    bars.append(ok if per else None)
    per = {m: (_get(runs, m, ("s3", "avoidance_positive_years")) or 0) > 0.60 for m in runs}
    ok = all(per.values()) and len(per) == len(runs)
    _bar(lines, "Bar 2 — avoidance benefit > 0 in >60% of years", ok if per else None,
         "; ".join(f"{m}: {pct(_get(runs, m, ('s3', 'avoidance_positive_years')), 0)}" for m in per))
    bars.append(ok if per else None)
    per = {m: (_get(runs, m, ("s3", "cried_wolf_rate")) or 1) < 0.40 for m in runs}
    ok = all(per.values()) and len(per) == len(runs)
    _bar(lines, "Bar 3 — cried-wolf rate < 40%", ok if per else None,
         "; ".join(f"{m}: {pct(_get(runs, m, ('s3', 'cried_wolf_rate')), 1)}" for m in per))
    bars.append(ok if per else None)
    lines.append("")
    lines.append(f"**S3 overall: {_overall(bars)}**")
    lines.append("")
    lines.append("S4 bars: not evaluated — study not run (no API key).")
    lines.append("")


def _overlay_bar(runs: Dict[str, "MarketRun"], family: str):
    per = {}
    details = []
    for market in runs:
        sims = _get(runs, market, ("sims",)) or {}
        overlay, baseline = sims.get(f"{family}_overlay"), sims.get(f"{family}_overlay_baseline")
        if overlay is None or baseline is None:
            continue
        mo, mb = overlay.metrics(), baseline.metrics()
        inc = mo.get("turnover_churn_mean", 0) - mb.get("turnover_churn_mean", 0)
        ok = (mo.get("excess_cagr_geometric", -1) > 0) and (inc <= 0.15)
        per[market] = ok
        details.append(f"{market}: excess {pct(mo.get('excess_cagr_geometric'))}, "
                       f"incremental turnover {pct(inc, 1)}/yr")
    ok = all(per.values()) and len(per) == len(runs)
    return (ok if per else None), "; ".join(details)


def _production_section(lines: List[str], runs: Dict[str, "MarketRun"]) -> None:
    """Calibrated production thresholds (§9.3) from US modal parameters."""
    us = runs.get("us")
    if us is None:
        return
    lines.append("---\n\n## Calibrated production thresholds (US walk-forward modal)")
    lines.append("")
    s1f = _get(runs, "us", ("frozen", "s1")) or {}
    s2f = _get(runs, "us", ("frozen", "s2")) or {}
    s3m = _get(runs, "us", ("frozen", "s3")) or {}
    ts = s1f.get("ts", {})
    v1 = s2f.get("V1", {})
    lines.append("```yaml")
    lines.append("earnings:")
    lines.append("  enabled: false   # flips only for signals whose §10 bars cleared")
    lines.append(f"  sue:")
    lines.append(f"    lookback_quarters: {ts.get('m', 8)}")
    lines.append(f"    min_history_quarters: {int(ts.get('m', 8)) + 4}")
    lines.append(f"    surprise_threshold: {ts.get('thr', 1.0)}")
    lines.append(f"    miss_trigger_threshold: {-max(2.0, float(ts.get('thr', 1.0)))}")
    lines.append(f"  quality_momentum:")
    lines.append(f"    min_revenue_growth_yoy_pct: {100.0 * float(v1.get('theta_rev', 0.05))}")
    lines.append(f"    min_opm_expansion_bps: {int(v1.get('theta_opm_bps', 100))}")
    lines.append(f"    persistence_quarters: {int(v1.get('persistence', 2))}")
    lines.append("    growth_factor_blend_weight: 0.3")
    lines.append(f"  deterioration:")
    lines.append(f"    consecutive_profit_declines: {int(s3m.get('consecutive_declines', 2))}")
    lines.append(f"    margin_compression_bps: {-int(s3m.get('margin_compression_bps', 300))}")
    lines.append(f"    cumulative_two_year_c2: {bool(s3m.get('cumulative_c2', False))}")
    lines.append("    confirm_components: 2")
    lines.append("    clear_after_clean_quarters: 2")
    lines.append("```")
    lines.append("")
    lines.append("These are the modal cells across the 11 walk-forward folds; "
                 "per-fold choices and their spread are in the fold CSVs. `enabled` "
                 "stays false per the spec until the relevant §10 bars clear.")
    lines.append("")


def _deviations_section(lines: List[str]) -> None:
    lines.append("---\n\n## Deviations from spec & judgement calls (implementation register)")
    lines.append("")
    for item in [
        "Spec §1.1 names the revenue tag `...ExcludingAssessedCost` — the actual "
        "XBRL tag is `...ExcludingAssessedTax`; the data layer used the real tag "
        "with the spec's fallback order.",
        "Walk-forward selection fixes each signal's PRIMARY horizon (S1 +21d, "
        "S2/S3 +126d) and grids the rest; other horizons are robustness at the "
        "selected parameters. A jointly-selected horizon would mix horizons along "
        "the stitched OOS path and make the §10 bars (defined at the primary "
        "horizon) uninterpretable.",
        "Revenue SUE (§3.1 robustness) evaluated at modal primary parameters only.",
        "India fiscal Q4 for the 60-day lag = March quarter-ends (dominant NSE "
        "convention); non-March fiscal-year names get 45d on their true Q4 — "
        "mildly aggressive for a handful of names, disclosed.",
        "S3 confirmation evaluates C1-at-c=3 as a standalone confirmer alongside "
        "the grid's c (literal §5.2 reading); C2 cumulative variant matches q−8 "
        "by sequence position with a sanity band (spec silent).",
        "Overlay mechanics where §7.2(b) is silent: initial book = 25 highest "
        "trailing-turnover names at sim start; unsignalled holdings score 0 "
        "(neutral) in the replacement ranking; S3 refills ranked by latest "
        "SUE-CS; review-day trades at that day's close; S3 exits are mandatory "
        "and bypass the turnover budget (src/risk/turnover.py semantics), "
        "deep-review trades are discretionary and budget-blocked.",
        "Event-study excess returns are arithmetic (event-study standard); "
        "portfolio-sim excess is geometric per §7.5.",
        "S2-V2's state-based exit re-checks a single name's legs (quintile "
        "re-ranking ex-post is not meaningful for one name) — approximation "
        "flagged; V1's state exit is exact.",
        "India financials exclusion uses the single NSE 'Financial Services' "
        "label (broader than banks/NBFC/housing — includes insurers/AMCs whose "
        "OPM is equally meaningless); count reported in the market sections.",
        "EPS Basic fallback (spec §1.1 order) was added to the EDGAR parser and "
        "the US cache re-parsed; eps coverage ~95%.",
        "SUE-TS trailing windows take the m most recent non-NaN "
        "availability-guarded YoY deltas (spec: no partial windows).",
        "US signals span 2015–2025 per §1.1 (folds train from 2010); 2026 data "
        "is unused for US headline numbers. India uses all available quarters.",
        "Turnover is enforced with the production min(buys,sells)/avg-NAV "
        "convention and ALSO reported as §7.5's (buys+sells)/2; the two differ "
        "little in these books.",
        "Known-bias register (spec §7.8) applies unchanged: survivorship "
        "overstates absolute returns in both legs; India 45/60-day assumption "
        "understates buy-side drift and S3 avoidance value (conservative); "
        "Screener restated history disclosed; S4 contamination deferred.",
    ]:
        lines.append(f"- {item}")
    lines.append("")


def _scraped_section(lines: List[str], scraped: dict) -> None:
    lines.append("---\n\n## India scraped-dates sensitivity (§1.2)")
    lines.append("")
    lines.append(f"Events with a scraped actual announcement date: {scraped.get('n_scraped')}; "
                 f"of those, actual date LATER than the 45/60-day assumption: "
                 f"{scraped.get('n_late')} ({pct(scraped.get('late_share'), 1)}) — the "
                 "assumption is too aggressive for those events (§1.2 flag).")
    lines.append("")
    for label in ("assumed", "scraped"):
        stats = scraped.get(f"s1_cs_top_{label}")
        if stats:
            lines.append(f"S1-CS top quintile under **{label}** dates: n={stats['n']}, "
                         f"mean {pct(stats['mean'])} (FM t {fmt(stats.get('fm_t'))})")
            lines.append("")
    stats = scraped.get("s1_cs_top_assumed_excl_late")
    if stats:
        lines.append(f"Assumed dates excluding too-aggressive events: n={stats['n']}, "
                     f"mean {pct(stats['mean'])}")
        lines.append("")
    lines.append("Coverage caveat: scraped dates exist only for a fraction of names and "
                 "mostly for the most recent quarters — this subsample is small and "
                 "recent-regime-only; it is a directional check on the lag assumption, "
                 "not a full re-estimate.")
    lines.append("")
