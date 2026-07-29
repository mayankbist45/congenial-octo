"""Factor-model risk decomposition.

This is a simplified Barra-style model. Each stock's daily return is regressed
on the benchmark plus style factors, splitting return variance into:

- a systematic (common factor) component, driven by market and style exposures
- an idiosyncratic (specific) component, unique to the company

The split is the number that matters for a stock picker. A long-only fund wants
its active risk concentrated in idiosyncratic risk, because that is what stock
selection skill is paid for; risk sitting in unintended factor bets is risk the
manager is taking without meaning to.

Factors are built from the universe itself, so no commercial factor data licence
is needed:

- style factors, as long-short spread portfolios:
  size (small minus large by market cap), value (cheap minus expensive by
  earnings yield), momentum (winners minus losers by 12-month return) and
  quality (high minus low by return on equity)
- industry factors, as each sector's return minus the universe return

Industry factors are essential rather than optional. Without them, the shared
movement of several holdings in the same sector has nowhere to go but the
residual, so a portfolio of five IT companies would be reported as mostly
stock-specific risk when it is in fact one large sector bet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.logging_setup import get_logger
from src.models import FundamentalSnapshot, TechnicalSnapshot

logger = get_logger(__name__)

TRADING_DAYS = 252
# A year of daily observations balances responsiveness against estimation noise.
DEFAULT_WINDOW = 252
MIN_OBSERVATIONS = 60

STYLE_FACTORS = ("size", "value", "momentum", "quality")


@dataclass
class Regression:
    """Result of regressing one asset on the factor set."""

    beta: float
    style_betas: Dict[str, float]
    alpha: float
    r_squared: float
    residual_vol_daily: float
    total_vol_daily: float
    observations: int

    @property
    def idiosyncratic_vol_annual(self) -> float:
        return self.residual_vol_daily * math.sqrt(TRADING_DAYS) * 100.0

    @property
    def total_vol_annual(self) -> float:
        return self.total_vol_daily * math.sqrt(TRADING_DAYS) * 100.0

    @property
    def systematic_vol_annual(self) -> float:
        systematic_variance = max(
            self.total_vol_daily**2 - self.residual_vol_daily**2, 0.0
        )
        return math.sqrt(systematic_variance) * math.sqrt(TRADING_DAYS) * 100.0

    @property
    def idio_share_pct(self) -> float:
        """Share of total variance that is company specific."""
        total_variance = self.total_vol_daily**2
        if total_variance <= 0:
            return 0.0
        return min(self.residual_vol_daily**2 / total_variance * 100.0, 100.0)


def daily_returns(prices: pd.Series) -> pd.Series:
    return prices.astype(float).pct_change().dropna()


def build_style_factors(
    price_frames: Dict[str, pd.Series],
    fundamentals: Dict[str, FundamentalSnapshot],
    technicals: Dict[str, TechnicalSnapshot],
    window: int = DEFAULT_WINDOW,
) -> pd.DataFrame:
    """Construct daily style factor returns as long-short spread portfolios.

    Each factor is the equal-weighted return of the top tercile minus the bottom
    tercile on the ranking metric. Rankings are taken as of today, so these are
    current-exposure factors rather than point-in-time rebalanced portfolios -
    an acceptable simplification for measuring today's risk profile.
    """
    returns = pd.DataFrame(
        {
            symbol: daily_returns(series).tail(window)
            for symbol, series in price_frames.items()
            if series is not None and len(series) > MIN_OBSERVATIONS
        }
    ).dropna(how="all")

    if returns.empty or returns.shape[1] < 20:
        logger.info(
            "Not enough breadth (%d names) to build style factors",
            returns.shape[1] if not returns.empty else 0,
        )
        return pd.DataFrame(index=returns.index)

    rankings: Dict[str, Dict[str, float]] = {factor: {} for factor in STYLE_FACTORS}
    for symbol in returns.columns:
        fundamental = fundamentals.get(symbol)
        technical = technicals.get(symbol)
        if fundamental is None or technical is None:
            continue

        if fundamental.market_cap:
            # Negated so a high score means small cap, matching the SMB sign.
            rankings["size"][symbol] = -math.log(max(fundamental.market_cap, 1.0))
        if fundamental.pe_ratio and fundamental.pe_ratio > 0:
            # Earnings yield: high is cheap.
            rankings["value"][symbol] = 1.0 / fundamental.pe_ratio
        if technical.return_12m is not None:
            rankings["momentum"][symbol] = technical.return_12m
        if fundamental.roe_pct is not None:
            rankings["quality"][symbol] = fundamental.roe_pct

    factors: Dict[str, pd.Series] = {}
    for factor, scores in rankings.items():
        if len(scores) < 20:
            continue
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        tercile = max(len(ordered) // 3, 5)
        top = [s for s, _ in ordered[:tercile] if s in returns.columns]
        bottom = [s for s, _ in ordered[-tercile:] if s in returns.columns]
        if len(top) < 5 or len(bottom) < 5:
            continue
        factors[factor] = returns[top].mean(axis=1) - returns[bottom].mean(axis=1)

    frame = pd.DataFrame(factors).dropna(how="all")
    logger.info(
        "Built %d style factor(s) from %d names: %s",
        frame.shape[1],
        returns.shape[1],
        ", ".join(frame.columns) or "none",
    )
    return frame


def build_industry_factors(
    price_frames: Dict[str, pd.Series],
    sector_of: Dict[str, str],
    window: int = DEFAULT_WINDOW,
    min_names_per_sector: int = 4,
) -> pd.DataFrame:
    """Build one factor per sector: sector return minus universe return.

    Subtracting the universe return keeps the industry factor roughly orthogonal
    to the market factor, so the regression can separate "this stock moves with
    the market" from "this stock moves with its industry".
    """
    returns = pd.DataFrame(
        {
            symbol: daily_returns(series).tail(window)
            for symbol, series in price_frames.items()
            if series is not None and len(series) > MIN_OBSERVATIONS
        }
    ).dropna(how="all")

    if returns.empty or returns.shape[1] < 20:
        return pd.DataFrame(index=returns.index if not returns.empty else None)

    universe_return = returns.mean(axis=1)

    members: Dict[str, List[str]] = {}
    for symbol in returns.columns:
        sector = sector_of.get(symbol)
        if sector:
            members.setdefault(sector, []).append(symbol)

    factors: Dict[str, pd.Series] = {}
    for sector, symbols in members.items():
        if len(symbols) < min_names_per_sector:
            continue
        key = "ind_" + "_".join(sector.lower().split())[:28]
        factors[key] = returns[symbols].mean(axis=1) - universe_return

    frame = pd.DataFrame(factors).dropna(how="all")
    logger.info(
        "Built %d industry factor(s) covering %d sectors",
        frame.shape[1],
        len(members),
    )
    return frame


def industry_factor_key(sector: str) -> str:
    return "ind_" + "_".join(sector.lower().split())[:28]


def regress(
    asset_returns: pd.Series,
    benchmark_returns: pd.Series,
    style_returns: Optional[pd.DataFrame] = None,
) -> Optional[Regression]:
    """Regress asset returns on the benchmark plus optional style factors."""
    frame = pd.DataFrame({"asset": asset_returns, "benchmark": benchmark_returns})
    style_columns: List[str] = []
    if style_returns is not None and not style_returns.empty:
        for column in style_returns.columns:
            frame[column] = style_returns[column]
            style_columns.append(column)

    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    if len(frame) < MIN_OBSERVATIONS:
        return None

    # Drop predictors that are constant or duplicated: collinear columns make
    # the least-squares solution explode into meaningless huge coefficients.
    usable: List[str] = []
    for column in ["benchmark"] + style_columns:
        series = frame[column]
        if series.std() <= 1e-12:
            continue
        if any(series.corr(frame[existing]) > 0.999 for existing in usable):
            continue
        usable.append(column)

    if "benchmark" not in usable:
        return None

    y = frame["asset"].to_numpy()
    design = np.column_stack(
        [np.ones(len(frame))] + [frame[c].to_numpy() for c in usable]
    )

    # Apple's Accelerate BLAS raises spurious floating-point flags on ordinary
    # matrix products, so numpy's warnings here are noise. Correctness is
    # enforced by the explicit finiteness checks instead.
    with np.errstate(all="ignore"):
        # rcond truncates near-zero singular values so a rank-deficient design
        # yields a stable minimum-norm solution instead of overflowing.
        try:
            coefficients, _, _, _ = np.linalg.lstsq(design, y, rcond=1e-10)
        except np.linalg.LinAlgError:
            return None

        if not np.all(np.isfinite(coefficients)):
            logger.debug("Discarding regression with non-finite coefficients")
            return None

        fitted = design @ coefficients
        residuals = y - fitted

        total_variance = float(np.var(y, ddof=1))
        residual_variance = float(np.var(residuals, ddof=1))

    if not (math.isfinite(total_variance) and math.isfinite(residual_variance)):
        return None

    style_columns = [c for c in usable if c != "benchmark"]
    r_squared = (
        1.0 - residual_variance / total_variance if total_variance > 0 else 0.0
    )

    benchmark_index = usable.index("benchmark") + 1
    return Regression(
        beta=float(coefficients[benchmark_index]),
        style_betas={
            name: float(coefficients[usable.index(name) + 1])
            for name in style_columns
        },
        alpha=float(coefficients[0]) * TRADING_DAYS * 100.0,
        r_squared=max(min(r_squared, 1.0), 0.0),
        residual_vol_daily=math.sqrt(max(residual_variance, 0.0)),
        total_vol_daily=math.sqrt(max(total_variance, 0.0)),
        observations=len(frame),
    )


def portfolio_risk_contributions(
    weights: Dict[str, float],
    returns: pd.DataFrame,
) -> Tuple[Dict[str, float], Dict[str, float], float]:
    """Decompose portfolio volatility into per-position contributions.

    Contribution to risk is ``w_i * (Sigma w)_i / portfolio_variance``, the
    standard Euler decomposition. Unlike raw weights it accounts for how a
    position covaries with the rest of the book, which is why two positions of
    equal size can carry very different amounts of the portfolio's risk.
    """
    symbols = [s for s in weights if s in returns.columns]
    if len(symbols) < 2:
        return {}, {}, 0.0

    aligned = returns[symbols].dropna()
    if len(aligned) < MIN_OBSERVATIONS:
        return {}, {}, 0.0

    weight_vector = np.array([weights[s] for s in symbols], dtype=float)
    total_weight = weight_vector.sum()
    if total_weight <= 0:
        return {}, {}, 0.0
    weight_vector = weight_vector / total_weight

    # See the note in ``regress`` about spurious Accelerate BLAS warnings.
    with np.errstate(all="ignore"):
        covariance = aligned.cov().to_numpy() * TRADING_DAYS
        portfolio_variance = float(weight_vector @ covariance @ weight_vector)
        marginal = covariance @ weight_vector

    if not math.isfinite(portfolio_variance) or portfolio_variance <= 0:
        return {}, {}, 0.0

    contributions = weight_vector * marginal

    contribution_pct = {
        symbol: float(contributions[index] / portfolio_variance * 100.0)
        for index, symbol in enumerate(symbols)
    }
    marginal_risk = {
        symbol: float(marginal[index] / math.sqrt(portfolio_variance))
        for index, symbol in enumerate(symbols)
    }
    portfolio_vol_pct = math.sqrt(portfolio_variance) * 100.0

    return contribution_pct, marginal_risk, portfolio_vol_pct


def decompose_portfolio_risk(
    portfolio_returns: pd.Series,
    benchmark_returns: pd.Series,
    style_returns: Optional[pd.DataFrame] = None,
) -> Optional[Tuple[float, float, float]]:
    """Split total portfolio risk into factor and specific components.

    Regressing the realised portfolio return series on the same factor set used
    for individual stocks gives the decomposition directly. This is preferred
    over summing position-level specific variances, which would assume position
    residuals are uncorrelated - false precisely when several holdings share an
    industry, the case a stock picker most needs to see.

    Returns ``(total_vol_pct, factor_share_pct, specific_share_pct)``.

    Note that for any long-only equity portfolio this split is dominated by
    market beta, so the specific share of *total* risk is naturally small. To
    judge stock-picking skill use :func:`decompose_active_risk` instead.
    """
    result = regress(portfolio_returns, benchmark_returns, style_returns)
    if result is None:
        return None

    specific_share = result.idio_share_pct
    return (
        result.total_vol_annual,
        max(100.0 - specific_share, 0.0),
        specific_share,
    )


def decompose_active_risk(
    portfolio_returns: pd.Series,
    benchmark_returns: pd.Series,
    factor_returns: Optional[pd.DataFrame] = None,
) -> Optional[Tuple[float, float, float]]:
    """Split active (benchmark-relative) risk into factor bets and stock picking.

    This is the decomposition that answers the question a stock picker cares
    about: of the risk I am taking *against my benchmark*, how much comes from
    deliberate or accidental factor and sector tilts, and how much from the
    individual names I chose?

    Active return is already benchmark-relative, so the benchmark is not a
    predictor here. Whatever the style and industry factors explain is factor
    risk; the residual is genuine security selection.

    Returns ``(tracking_error_pct, factor_share_pct, selection_share_pct)``.
    """
    frame = pd.DataFrame(
        {"portfolio": portfolio_returns, "benchmark": benchmark_returns}
    )
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    if len(frame) < MIN_OBSERVATIONS:
        return None

    active = frame["portfolio"] - frame["benchmark"]
    tracking_error = float(active.std() * math.sqrt(TRADING_DAYS) * 100.0)

    if factor_returns is None or factor_returns.empty:
        # With no factor model, all active risk is attributed to selection.
        return tracking_error, 0.0, 100.0

    aligned = factor_returns.reindex(active.index)
    usable = [c for c in aligned.columns if aligned[c].notna().sum() >= MIN_OBSERVATIONS]
    if not usable:
        return tracking_error, 0.0, 100.0

    combined = pd.concat([active.rename("active"), aligned[usable]], axis=1)
    combined = combined.replace([np.inf, -np.inf], np.nan).dropna()
    if len(combined) < MIN_OBSERVATIONS:
        return tracking_error, 0.0, 100.0

    predictors = [c for c in usable if combined[c].std() > 1e-12]
    if not predictors:
        return tracking_error, 0.0, 100.0

    y = combined["active"].to_numpy()
    design = np.column_stack(
        [np.ones(len(combined))] + [combined[c].to_numpy() for c in predictors]
    )

    # See the note in ``regress`` about spurious Accelerate BLAS warnings.
    with np.errstate(all="ignore"):
        try:
            coefficients, _, _, _ = np.linalg.lstsq(design, y, rcond=1e-10)
        except np.linalg.LinAlgError:
            return tracking_error, 0.0, 100.0
        if not np.all(np.isfinite(coefficients)):
            return tracking_error, 0.0, 100.0
        residuals = y - design @ coefficients
        total_variance = float(np.var(y, ddof=1))
        residual_variance = float(np.var(residuals, ddof=1))

    if (
        not math.isfinite(total_variance)
        or not math.isfinite(residual_variance)
        or total_variance <= 0
    ):
        return tracking_error, 0.0, 100.0

    # Adjust for the degrees of freedom the factors consume, so adding factors
    # cannot inflate the explained share by construction.
    observations = len(combined)
    predictor_count = len(predictors)
    denominator = observations - predictor_count - 1
    if denominator <= 0:
        selection_share = max(residual_variance / total_variance, 0.0) * 100.0
    else:
        adjusted_r_squared = 1.0 - (residual_variance * (observations - 1)) / (
            total_variance * denominator
        )
        selection_share = (1.0 - max(min(adjusted_r_squared, 1.0), 0.0)) * 100.0

    selection_share = max(min(selection_share, 100.0), 0.0)
    return tracking_error, 100.0 - selection_share, selection_share


def tracking_error_pct(
    portfolio_returns: pd.Series, benchmark_returns: pd.Series
) -> Optional[float]:
    """Annualised standard deviation of active (excess) returns."""
    frame = pd.DataFrame(
        {"portfolio": portfolio_returns, "benchmark": benchmark_returns}
    ).dropna()
    if len(frame) < MIN_OBSERVATIONS:
        return None
    active = frame["portfolio"] - frame["benchmark"]
    return float(active.std() * math.sqrt(TRADING_DAYS) * 100.0)


def active_share_pct(
    portfolio_weights: Dict[str, float], benchmark_weights: Dict[str, float]
) -> float:
    """Half the sum of absolute weight differences versus the benchmark.

    100% means no overlap with the index; 0% means the portfolio is the index.
    """
    symbols = set(portfolio_weights) | set(benchmark_weights)
    total = sum(
        abs(portfolio_weights.get(s, 0.0) - benchmark_weights.get(s, 0.0))
        for s in symbols
    )
    return min(total / 2.0, 100.0)


def correlation_stats(
    weights: Dict[str, float], returns: pd.DataFrame
) -> Tuple[Optional[float], Optional[float]]:
    """Average and maximum pairwise correlation among holdings.

    High values mean apparent diversification is illusory: several positions are
    really the same bet.
    """
    symbols = [s for s in weights if s in returns.columns]
    if len(symbols) < 2:
        return None, None

    aligned = returns[symbols].dropna()
    if len(aligned) < MIN_OBSERVATIONS:
        return None, None

    matrix = aligned.corr().to_numpy()
    upper = matrix[np.triu_indices_from(matrix, k=1)]
    upper = upper[~np.isnan(upper)]
    if upper.size == 0:
        return None, None
    return float(np.mean(upper)), float(np.max(upper))


def effective_positions(weights: Sequence[float]) -> Optional[float]:
    """Inverse Herfindahl index: how many equally-weighted positions this is.

    A 25-name portfolio where one name is 40% behaves like far fewer than 25
    positions, and this number says how many.
    """
    array = np.array([w for w in weights if w > 0], dtype=float)
    if array.size == 0:
        return None
    shares = array / array.sum()
    herfindahl = float(np.sum(shares**2))
    return 1.0 / herfindahl if herfindahl > 0 else None
