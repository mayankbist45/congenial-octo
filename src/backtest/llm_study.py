"""S4 LLM-as-analyst subsample study harness (spec §8).

This module is deliberately a HARNESS ONLY: it samples the 300-event
stratified subsample, builds the quarterly briefs, holds the verbatim system
prompt, validates the JSON schema with one retry, logs raw responses, and
prints a cost estimate. It REFUSES to run without OPENAI_API_KEY — there is
no key on this machine, so the study has not been executed; §8.5's training-
data contamination caveat applies to any future run.

Anti-lookahead (§7.6.6): a brief contains only quarters whose availability
date is strictly before the event's availability... with one exception — the
event quarter ITSELF is the subject of the brief, so the brief covers
quarters up to and including the event quarter, every one of which was
public by the event's availability date. No prices, no news, no peers.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from src.backtest.signals import EventSet
from src.logging_setup import get_logger

logger = get_logger(__name__)

SAMPLE_PER_MARKET = 150  # §8.1: 300 events = 150 US + 150 India
MIN_STRATUM_SIZE = 5
BRIEF_HISTORY_QUARTERS = 12

# Spec §8.3, verbatim — do not edit without amending the spec.
SYSTEM_PROMPT = """You are a senior fundamental equity analyst with 20 years of experience reading
quarterly results, first on the sell side and then at a long-only fund with a
low-turnover, thesis-driven process. You are reading one company's results
brief exactly as it stood on the day the results became public.

The brief below contains the company's recent quarterly financial history:
sales, operating profit, operating margin, net profit, and EPS, with
year-on-year changes. There are no analyst consensus estimates available; judge
the quarter against the company's own history and seasonality (compare each
quarter to the same quarter a year earlier).

Assess:
1. Whether the quarter is a beat, in line, or a miss relative to what the
   company's own trend and seasonality would have led a reasonable investor to
   expect.
2. The quality of the earnings: are profits backed by revenue growth and
   margins, or by one-offs, other income, or unsustainable margin levels?
3. Red flags: margin erosion, profit growth far ahead of revenue growth,
   deceleration, volatility, or anything that would make you distrust the
   numbers.
4. What this quarter does to the investment thesis for a long-term holder.

Hard rules:
- Use ONLY the numbers in this brief. Do not use any knowledge you have about
  this company, its industry, or events after the report date. If you recognise
  the company, pretend you do not.
- Never invent numbers. If the data is insufficient to judge, say so and lower
  your confidence.
- Be calibrated: most quarters are "in_line". Reserve "beat" and "miss" for
  quarters that clearly depart from the company's own trend.
- One paragraph of rationale, written the way you would write it in a fund's
  investment journal: specific numbers in, conclusion out.

Respond with a single JSON object and nothing else:
{
  "verdict": "beat" | "in_line" | "miss",
  "quality_of_earnings": <integer 0-10, where 10 = profit fully backed by
      revenue growth and stable-or-expanding margins, 0 = pure one-offs>,
  "red_flags": [<short strings; empty list if none>],
  "thesis_impact": "strengthens" | "intact" | "weakens" | "broken",
  "rationale": "<one paragraph>",
  "confidence": <float 0.0-1.0>
}"""

RETRY_REMINDER = "Return valid JSON only, matching the requested schema exactly."

VERDICTS = ("beat", "in_line", "miss")
THESIS_IMPACTS = ("strengthens", "intact", "weakens", "broken")

# Rough list-price USD per 1M tokens (input, output); labelled as an
# estimate in the printout — prices drift, check before a real run.
MODEL_PRICES = {
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}
EST_INPUT_TOKENS = 1600
EST_OUTPUT_TOKENS = 350


@dataclass
class Verdict:
    verdict: Optional[str]
    quality_of_earnings: Optional[int]
    red_flags: List[str]
    thesis_impact: Optional[str]
    rationale: Optional[str]
    confidence: Optional[float]
    failed: bool = False


def validate_payload(payload) -> Optional[Verdict]:
    """§8.4 strict schema validation; returns None on any deviation."""
    if not isinstance(payload, dict):
        return None
    keys = {"verdict", "quality_of_earnings", "red_flags", "thesis_impact",
            "rationale", "confidence"}
    if set(payload) != keys:
        return None
    if payload["verdict"] not in VERDICTS:
        return None
    if payload["thesis_impact"] not in THESIS_IMPACTS:
        return None
    if not isinstance(payload["red_flags"], list) or not all(
        isinstance(x, str) for x in payload["red_flags"]
    ):
        return None
    if not isinstance(payload["rationale"], str) or not payload["rationale"].strip():
        return None
    try:
        quality = int(round(float(payload["quality_of_earnings"])))
        confidence = float(payload["confidence"])
    except (TypeError, ValueError):
        return None
    return Verdict(
        verdict=payload["verdict"],
        quality_of_earnings=int(np.clip(quality, 0, 10)),
        red_flags=list(payload["red_flags"]),
        thesis_impact=payload["thesis_impact"],
        rationale=payload["rationale"].strip(),
        confidence=float(np.clip(confidence, 0.0, 1.0)),
    )


# ------------------------------------------------------------------- sampler


def stratified_sample(event_set: EventSet, sue_col: str,
                      n: int = SAMPLE_PER_MARKET) -> pd.DataFrame:
    """150 events stratified by year × SUE tercile × sector (§8.1).

    Strata smaller than 5 are dropped; allocation is proportional with
    largest-remainder rounding; picks are deterministic (evenly spaced by
    symbol/period order — no randomness anywhere in this project).
    """
    events = event_set.events.dropna(subset=[sue_col]).copy()
    events["year"] = events.availability.dt.year
    def tercile(group: pd.DataFrame) -> pd.Series:
        ranks = group[sue_col].rank(method="first")
        return pd.Series(np.minimum((ranks - 1) * 3 // len(group), 2), index=group.index)
    events["tercile"] = events.groupby("year", group_keys=False).apply(tercile)
    strata = events.groupby(["year", "tercile", "sector"])
    eligible = {k: g for k, g in strata if len(g) >= MIN_STRATUM_SIZE}
    if not eligible:
        return pd.DataFrame()
    total = sum(len(g) for g in eligible.values())
    quotas = {k: len(g) / total * n for k, g in eligible.items()}
    take = {k: int(np.floor(q)) for k, q in quotas.items()}
    remainder = n - sum(take.values())
    for k, _ in sorted(quotas.items(), key=lambda kv: -(kv[1] % 1))[:remainder]:
        take[k] += 1
    picks = []
    for key, group in eligible.items():
        ordered = group.sort_values(["symbol", "period_end"])
        k = min(take[key], len(ordered))
        idx = np.linspace(0, len(ordered) - 1, k).round().astype(int)
        picks.append(ordered.iloc[np.unique(idx)])
    return pd.concat(picks).sort_values(["availability", "symbol"])


# -------------------------------------------------------------------- briefs


def build_brief(event_set: EventSet, event: pd.Series) -> str:
    """The quarterly brief — the ONLY thing the LLM sees (§8.2)."""
    series = event_set.series[event.symbol]
    upto = series[series.period_end <= event.period_end].tail(BRIEF_HISTORY_QUARTERS)
    unit = "Rs crore" if event_set.market == "india" else "USD"
    lines = [
        f"Company: {event.symbol}",
        f"Sector: {event.sector}",
        f"Fiscal period under review: quarter ended "
        f"{event.period_end.strftime('%b %Y')}",
        f"(Flows in {unit}; EPS in {'Rs' if event_set.market == 'india' else 'USD'}; "
        "YoY = change vs the same quarter a year earlier)",
        "",
        "Quarter | Sales (YoY) | Op profit (YoY) | OPM % (ΔYoY bps) | "
        "Net profit (YoY) | EPS (YoY)",
    ]

    def fmt(value, pct=False, bps=False):
        if value is None or (isinstance(value, float) and not np.isfinite(value)):
            return "n/a"
        if pct:
            return f"{value * 100:+.1f}%"
        if bps:
            return f"{value:+.0f}"
        return f"{value:,.1f}"

    for row in upto.itertuples():
        g_rev = row.g_rev if np.isfinite(row.g_rev) else np.nan
        d_opm = row.d_opm_bps if np.isfinite(row.d_opm_bps) else np.nan
        d_eps = row.eps - row.eps_yoy if np.isfinite(row.eps) and np.isfinite(row.eps_yoy) else np.nan
        np_yoy = np.nan
        if np.isfinite(row.net_income) and np.isfinite(row.np_yoy) and abs(row.np_yoy) > 0:
            np_yoy = (row.net_income - row.np_yoy) / abs(row.np_yoy)
        lines.append(
            f"{row.period_end.strftime('%b %Y')} | "
            f"{fmt(row.rev)} ({fmt(g_rev, pct=True)}) | "
            f"{fmt(row.rev * row.opm if np.isfinite(row.opm) else np.nan)} | "
            f"{fmt(row.opm * 100 if np.isfinite(row.opm) else np.nan)}% "
            f"({fmt(d_opm, bps=True)}) | "
            f"{fmt(row.net_income)} ({fmt(np_yoy, pct=True)}) | "
            f"{fmt(row.eps)} ({fmt(d_eps)})"
        )
    return "\n".join(lines)


def mechanical_verdict(sue: float) -> str:
    if sue >= 1.0:
        return "beat"
    if sue <= -1.0:
        return "miss"
    return "in_line"


def cohens_kappa(labels_a: List[str], labels_b: List[str]) -> float:
    """Cohen's κ over two categorical label vectors (no sklearn needed)."""
    if len(labels_a) != len(labels_b) or not labels_a:
        return float("nan")
    labels = sorted(set(labels_a) | set(labels_b))
    n = len(labels_a)
    po = sum(a == b for a, b in zip(labels_a, labels_b)) / n
    pe = sum(
        (labels_a.count(x) / n) * (labels_b.count(x) / n) for x in labels
    )
    if pe >= 1.0:
        return float("nan")
    return (po - pe) / (1 - pe)


# ------------------------------------------------------------------ the run


def cost_estimate(model: str, n_events: int = 2 * SAMPLE_PER_MARKET) -> str:
    input_price, output_price = MODEL_PRICES.get(model, (1.00, 3.00))
    cost = n_events * (
        EST_INPUT_TOKENS / 1e6 * input_price + EST_OUTPUT_TOKENS / 1e6 * output_price
    )
    return (
        f"S4 cost estimate: {n_events} events × ~{EST_INPUT_TOKENS} in / "
        f"~{EST_OUTPUT_TOKENS} out tokens on {model} "
        f"(${input_price}/1M in, ${output_price}/1M out) ≈ ${cost:.2f}. "
        "Prices are rough list prices — verify before running."
    )


def require_api_key() -> None:
    """§8 harness must refuse clearly when no key is configured."""
    if os.environ.get("OPENAI_API_KEY"):
        return
    try:
        from src.config import get_config

        if get_config().secrets.has_llm_credentials:
            return
    except Exception:
        pass
    raise SystemExit(
        "S4 LLM study requires OPENAI_API_KEY (env or .env); none is set. "
        "The study has NOT been run — see §8.5's contamination caveat before "
        "interpreting any future results."
    )


def run_study(event_sets: Dict[str, EventSet], out_dir: Path,
              sue_cols: Dict[str, str], model: Optional[str] = None) -> pd.DataFrame:
    """Full S4 run. Only callable with an API key; logs every raw response."""
    require_api_key()
    from src.llm import LLMClient, extract_json

    client = LLMClient()
    model = model or client.model
    print(cost_estimate(model))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_log = out_dir / "s4_raw_responses.jsonl"

    rows = []
    for market, event_set in event_sets.items():
        sample = stratified_sample(event_set, sue_cols[market])
        logger.info("S4 %s: sampled %d events", market, len(sample))
        for event in sample.itertuples():
            brief = build_brief(event_set, event_set.events.loc[
                (event_set.events.symbol == event.symbol)
                & (event_set.events.period_end == event.period_end)
            ].iloc[0])
            verdict = None
            raw = ""
            for attempt in (0, 1):
                prompt = brief if attempt == 0 else f"{brief}\n\n{RETRY_REMINDER}"
                try:
                    raw = client.complete(SYSTEM_PROMPT, prompt, model=model,
                                          temperature=0.0)
                except Exception as exc:  # outage -> record failure, move on
                    logger.warning("S4 call failed for %s: %s", event.symbol, exc)
                    raw = f"__error__: {exc}"
                    break
                verdict = validate_payload(extract_json(raw))
                if verdict is not None:
                    break
            with raw_log.open("a") as fh:
                fh.write(json.dumps({
                    "market": market, "symbol": event.symbol,
                    "period_end": str(event.period_end.date()),
                    "availability": str(event.availability.date()),
                    "model": model, "raw": raw,
                }) + "\n")
            sue = getattr(event, sue_cols[market])
            rows.append({
                "market": market, "symbol": event.symbol,
                "period_end": event.period_end, "availability": event.availability,
                "sue": sue, "mechanical_verdict": mechanical_verdict(sue),
                "llm_verdict": verdict.verdict if verdict else None,
                "quality_of_earnings": verdict.quality_of_earnings if verdict else None,
                "thesis_impact": verdict.thesis_impact if verdict else None,
                "confidence": verdict.confidence if verdict else None,
                "red_flags": "; ".join(verdict.red_flags) if verdict else None,
                "rationale": verdict.rationale if verdict else None,
                "failed": verdict is None,
            })
    results = pd.DataFrame(rows)
    results.to_csv(out_dir / "s4_results.csv", index=False)
    n_failed = int(results.failed.sum()) if len(results) else 0
    logger.info("S4 complete: %d/%d schema failures", n_failed, len(results))
    return results
