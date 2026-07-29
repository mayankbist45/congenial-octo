"""Multi-factor screening engine.

Implements the funnel a professional stock picker uses: score the whole universe
on factors with a clear economic rationale, apply hard fundamental gates that no
score can override, then rank. The output is a shortlist, not a decision - the
risk engine and decision layer take it from there.

Factor design follows the research consensus:

- Quality (ROE, ROCE, margins, cash conversion, low leverage) captures durable
  businesses and is the highest-weighted factor for a long-horizon fund.
- Value (P/E, P/B, PEG, EV/EBITDA, dividend yield) guards against overpaying.
- Growth (revenue and earnings growth) separates compounders from value traps.
- Momentum (6 and 12 month returns) is a well-documented cross-sectional effect
  and, more practically, keeps the fund from buying falling knives.
- Trend (position versus the 200-day moving average) is a timing overlay only.

Metrics are winsorized then z-scored cross-sectionally, so a name is judged
against its peers on the day rather than against an absolute threshold.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.config import Config, get_config
from src.logging_setup import get_logger
from src.models import FactorScores, FundamentalSnapshot, TechnicalSnapshot
from src.universe import Universe, load_universe

logger = get_logger(__name__)


@dataclass(frozen=True)
class MetricSpec:
    """One input to a factor score."""

    name: str
    factor: str
    extract: Callable[[FundamentalSnapshot, TechnicalSnapshot], Optional[float]]
    # True when a lower raw value is better (valuation multiples, leverage).
    lower_is_better: bool = False
    weight: float = 1.0
    # Values outside this range are treated as data errors and dropped.
    valid_min: Optional[float] = None
    valid_max: Optional[float] = None


def _f(name: str) -> Callable[[FundamentalSnapshot, TechnicalSnapshot], Optional[float]]:
    return lambda fundamental, _technical: getattr(fundamental, name, None)


def _t(name: str) -> Callable[[FundamentalSnapshot, TechnicalSnapshot], Optional[float]]:
    return lambda _fundamental, technical: getattr(technical, name, None)


def _price_to_sma200(
    _fundamental: FundamentalSnapshot, technical: TechnicalSnapshot
) -> Optional[float]:
    if not technical.last_price or not technical.sma_200:
        return None
    return (technical.last_price / technical.sma_200 - 1.0) * 100.0


METRIC_SPECS: Tuple[MetricSpec, ...] = (
    # --- Quality ---
    MetricSpec("roe_pct", "quality", _f("roe_pct"), weight=1.5, valid_min=-100, valid_max=200),
    MetricSpec("roce_pct", "quality", _f("roce_pct"), weight=1.5, valid_min=-100, valid_max=200),
    MetricSpec(
        "operating_margin_pct", "quality", _f("operating_margin_pct"),
        weight=1.0, valid_min=-100, valid_max=100,
    ),
    MetricSpec(
        "profit_margin_pct", "quality", _f("profit_margin_pct"),
        weight=0.75, valid_min=-100, valid_max=100,
    ),
    MetricSpec(
        "debt_to_equity", "quality", _f("debt_to_equity"),
        lower_is_better=True, weight=1.0, valid_min=0, valid_max=10,
    ),
    MetricSpec(
        "cash_conversion", "quality", _f("cash_conversion"),
        weight=0.75, valid_min=-200, valid_max=400,
    ),
    # --- Value ---
    MetricSpec(
        "pe_ratio", "value", _f("pe_ratio"),
        lower_is_better=True, weight=1.5, valid_min=0.5, valid_max=300,
    ),
    MetricSpec(
        "price_to_book", "value", _f("price_to_book"),
        lower_is_better=True, weight=1.0, valid_min=0.05, valid_max=60,
    ),
    MetricSpec(
        "peg_ratio", "value", _f("peg_ratio"),
        lower_is_better=True, weight=1.0, valid_min=0.05, valid_max=15,
    ),
    MetricSpec(
        "ev_to_ebitda", "value", _f("ev_to_ebitda"),
        lower_is_better=True, weight=0.75, valid_min=0.5, valid_max=100,
    ),
    MetricSpec(
        "dividend_yield", "value", _f("dividend_yield"),
        weight=0.5, valid_min=0, valid_max=25,
    ),
    # --- Growth ---
    MetricSpec(
        "revenue_growth_pct", "growth", _f("revenue_growth_pct"),
        weight=1.25, valid_min=-90, valid_max=200,
    ),
    MetricSpec(
        "earnings_growth_pct", "growth", _f("earnings_growth_pct"),
        weight=1.25, valid_min=-95, valid_max=300,
    ),
    # --- Momentum ---
    MetricSpec("return_6m", "momentum", _t("return_6m"), weight=1.0, valid_min=-95, valid_max=400),
    MetricSpec("return_12m", "momentum", _t("return_12m"), weight=1.0, valid_min=-95, valid_max=600),
    MetricSpec(
        "distance_from_52w_high", "momentum", _t("distance_from_52w_high"),
        weight=0.5, valid_min=-100, valid_max=5,
    ),
    # --- Trend ---
    MetricSpec("price_vs_sma200", "trend", _price_to_sma200, weight=1.0, valid_min=-90, valid_max=200),
)

FACTOR_NAMES = ("quality", "value", "growth", "momentum", "trend")


def winsorized_zscore(series: pd.Series, clip: float = 3.0) -> pd.Series:
    """Z-score with outliers pulled to the clip boundary.

    Winsorizing before scaling stops one bad data point (a 900% ROE from a tiny
    equity base) from compressing everyone else toward zero.
    """
    values = series.astype(float)
    valid = values.dropna()
    if len(valid) < 5:
        return pd.Series(np.nan, index=series.index)

    # Clip to the 1st/99th percentile before computing moments.
    lower, upper = valid.quantile(0.01), valid.quantile(0.99)
    trimmed = values.clip(lower=lower, upper=upper)

    mean = trimmed.mean()
    std = trimmed.std()
    if std is None or std == 0 or math.isnan(std):
        return pd.Series(0.0, index=series.index).where(values.notna())

    return ((trimmed - mean) / std).clip(lower=-clip, upper=clip)


class ScoringEngine:
    """Scores and ranks the universe."""

    def __init__(self, config: Optional[Config] = None, universe: Optional[Universe] = None):
        self._config = config or get_config()
        self._universe = universe or load_universe(self._config)
        weights = self._config.get("quant.factor_weights", {}) or {}
        self._weights: Dict[str, float] = {
            name: float(weights.get(name, 0.0)) for name in FACTOR_NAMES
        }
        self._clip = float(self._config.get("quant.zscore_clip", 3.0) or 3.0)

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------
    def _check_gates(
        self,
        symbol: str,
        fundamental: FundamentalSnapshot,
        technical: TechnicalSnapshot,
    ) -> List[str]:
        """Hard fundamental gates. Any failure makes a name unbuyable."""
        gates = self._config.get("quant.gates", {}) or {}
        failures: List[str] = []

        max_de = gates.get("max_debt_to_equity")
        # Banks and NBFCs are leveraged by construction; debt/equity is not a
        # meaningful quality signal for them, so the gate does not apply.
        is_lender = self._universe.sector_of(symbol) == "Financial Services"
        if (
            max_de is not None
            and not is_lender
            and fundamental.debt_to_equity is not None
            and fundamental.debt_to_equity > float(max_de)
        ):
            failures.append(
                f"debt/equity {fundamental.debt_to_equity:.2f} > {max_de}"
            )

        min_roe = gates.get("min_roe_pct")
        if (
            min_roe is not None
            and fundamental.roe_pct is not None
            and fundamental.roe_pct < float(min_roe)
        ):
            failures.append(f"ROE {fundamental.roe_pct:.1f}% < {min_roe}%")

        max_pe = gates.get("max_pe")
        if max_pe is not None and fundamental.pe_ratio is not None:
            if fundamental.pe_ratio > float(max_pe):
                failures.append(f"P/E {fundamental.pe_ratio:.1f} > {max_pe}")
            elif fundamental.pe_ratio <= 0:
                failures.append("P/E not positive (loss making)")

        min_growth = gates.get("min_revenue_growth_pct")
        if (
            min_growth is not None
            and fundamental.revenue_growth_pct is not None
            and fundamental.revenue_growth_pct < float(min_growth)
        ):
            failures.append(
                f"revenue growth {fundamental.revenue_growth_pct:.1f}% < {min_growth}%"
            )

        # Liquidity: the fund must be able to exit without moving the price.
        min_turnover = self._config.get("universe.min_avg_daily_turnover_inr")
        if (
            min_turnover is not None
            and technical.avg_daily_turnover is not None
            and technical.avg_daily_turnover < float(min_turnover)
        ):
            failures.append(
                f"avg daily turnover Rs {technical.avg_daily_turnover / 1e7:.1f}cr "
                f"below Rs {float(min_turnover) / 1e7:.1f}cr floor"
            )

        if not fundamental.is_usable:
            failures.append("insufficient fundamental data")

        return failures

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------
    def score(
        self,
        fundamentals: Dict[str, FundamentalSnapshot],
        technicals: Dict[str, TechnicalSnapshot],
    ) -> Dict[str, FactorScores]:
        """Score every symbol present in both inputs."""
        symbols = sorted(set(fundamentals) & set(technicals))
        if not symbols:
            logger.warning("Nothing to score: no overlap of fundamentals and prices")
            return {}

        # Build the raw metric matrix.
        raw = pd.DataFrame(index=symbols, columns=[s.name for s in METRIC_SPECS], dtype=float)
        for spec in METRIC_SPECS:
            column: List[Optional[float]] = []
            for symbol in symbols:
                value = spec.extract(fundamentals[symbol], technicals[symbol])
                if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
                    column.append(np.nan)
                    continue
                value = float(value)
                if spec.valid_min is not None and value < spec.valid_min:
                    column.append(np.nan)
                elif spec.valid_max is not None and value > spec.valid_max:
                    column.append(np.nan)
                else:
                    column.append(value)
            raw[spec.name] = column

        coverage = raw.notna().mean()
        thin = coverage[coverage < 0.4]
        if not thin.empty:
            logger.info(
                "Thin metric coverage (<40%%): %s",
                ", ".join(f"{k} {v:.0%}" for k, v in thin.items()),
            )

        # Z-score each metric, flipping sign where lower is better.
        zscores = pd.DataFrame(index=symbols, dtype=float)
        for spec in METRIC_SPECS:
            z = winsorized_zscore(raw[spec.name], clip=self._clip)
            zscores[spec.name] = -z if spec.lower_is_better else z

        # Aggregate into factors as a weighted mean over available metrics, so
        # a name with one missing input is not penalised to zero.
        factor_frame = pd.DataFrame(index=symbols, dtype=float)
        for factor in FACTOR_NAMES:
            specs = [s for s in METRIC_SPECS if s.factor == factor]
            if not specs:
                factor_frame[factor] = 0.0
                continue
            weighted_sum = pd.Series(0.0, index=symbols)
            weight_total = pd.Series(0.0, index=symbols)
            for spec in specs:
                column = zscores[spec.name]
                present = column.notna()
                weighted_sum = weighted_sum.add(column.fillna(0.0) * spec.weight, fill_value=0.0)
                weight_total = weight_total.add(present.astype(float) * spec.weight, fill_value=0.0)
            factor_frame[factor] = (weighted_sum / weight_total.replace(0.0, np.nan)).fillna(0.0)

        composite = pd.Series(0.0, index=symbols)
        for factor in FACTOR_NAMES:
            composite = composite + factor_frame[factor] * self._weights.get(factor, 0.0)

        # Rank: 1 is the most attractive name.
        ranks = composite.rank(ascending=False, method="min")
        percentiles = composite.rank(pct=True) * 100.0

        results: Dict[str, FactorScores] = {}
        for symbol in symbols:
            failures = self._check_gates(
                symbol, fundamentals[symbol], technicals[symbol]
            )
            contributing = {
                spec.name: float(raw.at[symbol, spec.name])
                for spec in METRIC_SPECS
                if not pd.isna(raw.at[symbol, spec.name])
            }
            results[symbol] = FactorScores(
                symbol=symbol,
                quality=round(float(factor_frame.at[symbol, "quality"]), 4),
                value=round(float(factor_frame.at[symbol, "value"]), 4),
                growth=round(float(factor_frame.at[symbol, "growth"]), 4),
                momentum=round(float(factor_frame.at[symbol, "momentum"]), 4),
                trend=round(float(factor_frame.at[symbol, "trend"]), 4),
                composite=round(float(composite[symbol]), 4),
                rank=int(ranks[symbol]),
                percentile=round(float(percentiles[symbol]), 2),
                passes_gates=not failures,
                gate_failures=failures,
                contributing_metrics=contributing,
            )

        eligible = sum(1 for s in results.values() if s.passes_gates)
        logger.info(
            "Scored %d symbols; %d pass all fundamental gates", len(results), eligible
        )
        return results

    # ------------------------------------------------------------------
    # Shortlisting
    # ------------------------------------------------------------------
    def buy_candidates(
        self,
        scores: Dict[str, FactorScores],
        exclude: Optional[Sequence[str]] = None,
        limit: int = 20,
    ) -> List[FactorScores]:
        """Top-ranked names that clear every gate and the minimum score."""
        excluded = {s.upper() for s in (exclude or [])}
        min_score = float(self._config.get("quant.min_buy_score", 0.5) or 0.5)

        candidates = [
            score
            for symbol, score in scores.items()
            if symbol not in excluded
            and score.passes_gates
            and score.composite >= min_score
        ]
        candidates.sort(key=lambda s: s.composite, reverse=True)

        logger.info(
            "Buy candidates: %d names above composite %.2f (showing top %d)",
            len(candidates),
            min_score,
            min(limit, len(candidates)),
        )
        return candidates[:limit]

    def weakest_holdings(
        self, scores: Dict[str, FactorScores], holdings: Sequence[str]
    ) -> List[FactorScores]:
        """Current holdings ordered worst-first, for opportunity-cost checks."""
        held = [scores[s.upper()] for s in holdings if s.upper() in scores]
        held.sort(key=lambda s: s.composite)
        return held
