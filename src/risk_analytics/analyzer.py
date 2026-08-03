"""Portfolio risk analyzer.

Assembles the factor-model outputs into the single risk profile the decision
layer reads, and raises warnings when a configured risk limit is breached.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

import pandas as pd

from src.config import Config, get_config
from src.logging_setup import get_logger
from src.market_data.prices import PriceService
from src.models import (
    FundamentalSnapshot,
    PortfolioRiskProfile,
    PortfolioState,
    RiskMetrics,
    TechnicalSnapshot,
)
from src.risk_analytics.factor_model import (
    active_share_pct,
    build_industry_factors,
    decompose_active_risk,
    build_style_factors,
    correlation_stats,
    daily_returns,
    decompose_portfolio_risk,
    effective_positions,
    industry_factor_key,
    portfolio_risk_contributions,
    regress,
    tracking_error_pct,
)
from src.risk_analytics.market_regime import assess_market_regime
from src.universe import Universe, load_universe

logger = get_logger(__name__)


class RiskAnalyzer:
    """Computes the fund's risk profile from price history and holdings."""

    def __init__(
        self,
        config: Optional[Config] = None,
        price_service: Optional[PriceService] = None,
        universe: Optional[Universe] = None,
    ):
        self._config = config or get_config()
        self._prices = price_service or PriceService(self._config)
        self._universe = universe or load_universe(self._config)

    def analyse(
        self,
        portfolio: PortfolioState,
        fundamentals: Dict[str, FundamentalSnapshot],
        technicals: Dict[str, TechnicalSnapshot],
        universe_symbols: Optional[List[str]] = None,
    ) -> PortfolioRiskProfile:
        profile = PortfolioRiskProfile(as_of=portfolio.as_of)

        weights = portfolio.weights()
        profile.sector_weights = portfolio.sector_weights()

        benchmark_prices = self._prices.benchmark_series()
        if benchmark_prices is None or len(benchmark_prices) < 60:
            profile.warnings.append(
                "Benchmark history unavailable; beta and tracking error skipped"
            )
            benchmark_returns = None
        else:
            benchmark_returns = daily_returns(benchmark_prices)

        # Style factors need breadth, so they are built from the whole universe
        # slice we already have priced, not just the holdings.
        factor_symbols = universe_symbols or list(technicals.keys())
        price_series: Dict[str, pd.Series] = {}
        for symbol in set(factor_symbols) | set(portfolio.symbols):
            series = self._prices.close_series(symbol)
            if series is not None and len(series) > 60:
                price_series[symbol] = series

        profile.market_regime = assess_market_regime(
            benchmark_prices, price_series, technicals
        )
        profile.warnings.extend(profile.market_regime.warnings)

        if not portfolio.positions:
            profile.warnings.append("Portfolio is empty; no risk to decompose")
            return profile

        style_factors = build_style_factors(price_series, fundamentals, technicals)
        industry_factors = build_industry_factors(
            price_series,
            {symbol: self._universe.sector_of(symbol) for symbol in price_series},
        )

        holdings_returns = pd.DataFrame(
            {
                symbol: daily_returns(price_series[symbol])
                for symbol in portfolio.symbols
                if symbol in price_series
            }
        )

        # --- Per-position regressions ---
        per_position: Dict[str, RiskMetrics] = {}
        for position in portfolio.positions:
            symbol = position.symbol
            metrics = RiskMetrics(symbol=symbol)

            technical = technicals.get(symbol)
            if technical and technical.avg_daily_turnover:
                metrics.pct_of_adv = (
                    position.market_value / technical.avg_daily_turnover * 100.0
                )
                metrics.days_to_liquidate = (
                    position.market_value
                    / (technical.avg_daily_turnover * 0.20)
                )

            if symbol in holdings_returns.columns and benchmark_returns is not None:
                # A stock is regressed on the styles plus its own industry, so
                # sector co-movement is not mistaken for stock-specific risk.
                factors = style_factors
                own_industry = industry_factor_key(position.sector)
                if own_industry in industry_factors.columns:
                    factors = pd.concat(
                        [style_factors, industry_factors[[own_industry]]], axis=1
                    )
                result = regress(holdings_returns[symbol], benchmark_returns, factors)
                if result is not None:
                    metrics.beta = round(result.beta, 3)
                    metrics.r_squared = round(result.r_squared, 3)
                    metrics.idiosyncratic_vol = round(
                        result.idiosyncratic_vol_annual, 2
                    )
                    metrics.total_vol = round(result.total_vol_annual, 2)
                    metrics.systematic_vol = round(result.systematic_vol_annual, 2)
                    metrics.idio_share_pct = round(result.idio_share_pct, 1)
                    metrics.style_exposures = {
                        k: round(v, 3) for k, v in result.style_betas.items()
                    }

            per_position[symbol] = metrics

        profile.per_position = per_position

        # --- Risk contributions ---
        contributions, marginal, portfolio_vol = portfolio_risk_contributions(
            weights, holdings_returns
        )
        for symbol, value in contributions.items():
            if symbol in per_position:
                per_position[symbol].risk_contribution_pct = round(value, 2)
        for symbol, value in marginal.items():
            if symbol in per_position:
                per_position[symbol].marginal_risk = round(value, 4)
        if portfolio_vol > 0:
            profile.portfolio_vol_annualised = round(portfolio_vol, 2)

        # --- Portfolio beta: weighted average of position betas ---
        beta_weight = 0.0
        beta_sum = 0.0
        for symbol, weight in weights.items():
            beta = per_position.get(symbol, RiskMetrics(symbol)).beta
            if beta is not None:
                beta_sum += beta * weight
                beta_weight += weight
        if beta_weight > 0:
            profile.portfolio_beta = round(beta_sum / beta_weight, 3)

        # --- Style tilts: weighted average style exposure ---
        tilts: Dict[str, float] = {}
        for symbol, weight in weights.items():
            exposures = per_position.get(symbol, RiskMetrics(symbol)).style_exposures
            for factor, value in exposures.items():
                tilts[factor] = tilts.get(factor, 0.0) + value * weight / 100.0
        profile.style_tilts = {k: round(v, 3) for k, v in tilts.items()}

        # --- Benchmark-relative measures and risk decomposition ---
        if benchmark_returns is not None and not holdings_returns.empty:
            invested = sum(weights.values())
            if invested > 0:
                weight_series = pd.Series(
                    {s: w / invested for s, w in weights.items()}
                )
                common = [
                    s for s in weight_series.index if s in holdings_returns.columns
                ]
                if common:
                    # Reconstruct the portfolio's own return series, then treat
                    # it as a single asset for the factor decomposition.
                    portfolio_returns = (
                        holdings_returns[common] * weight_series[common]
                    ).sum(axis=1)

                    error = tracking_error_pct(portfolio_returns, benchmark_returns)
                    if error is not None:
                        profile.tracking_error_pct = round(error, 2)

                    # The portfolio is regressed on the styles plus every
                    # industry it holds, so a sector bet shows up as factor
                    # risk rather than hiding in the residual.
                    portfolio_factors = style_factors
                    held_industries = [
                        key
                        for key in dict.fromkeys(
                            industry_factor_key(s) for s in profile.sector_weights
                        )
                        if key in industry_factors.columns
                    ]
                    if held_industries:
                        portfolio_factors = pd.concat(
                            [style_factors, industry_factors[held_industries]], axis=1
                        )

                    decomposition = decompose_portfolio_risk(
                        portfolio_returns, benchmark_returns, portfolio_factors
                    )
                    if decomposition is not None:
                        total_vol, factor_share, specific_share = decomposition
                        profile.portfolio_vol_annualised = round(total_vol, 2)
                        profile.factor_risk_share_pct = round(factor_share, 1)
                        profile.idio_risk_share_pct = round(specific_share, 1)

                    # The split that matters for judging stock selection.
                    active = decompose_active_risk(
                        portfolio_returns, benchmark_returns, portfolio_factors
                    )
                    if active is not None:
                        error, active_factor, selection = active
                        profile.tracking_error_pct = round(error, 2)
                        profile.active_factor_share_pct = round(active_factor, 1)
                        profile.active_selection_share_pct = round(selection, 1)

        profile.active_share_pct = round(
            active_share_pct(weights, self._benchmark_weights()), 1
        )

        # --- Concentration and correlation ---
        sorted_weights = sorted(weights.values(), reverse=True)
        profile.concentration_top5_pct = round(sum(sorted_weights[:5]), 2)
        profile.concentration_top10_pct = round(sum(sorted_weights[:10]), 2)
        effective = effective_positions(sorted_weights)
        if effective is not None:
            profile.effective_positions = round(effective, 1)

        average_correlation, max_correlation = correlation_stats(
            weights, holdings_returns
        )
        if average_correlation is not None:
            profile.avg_pairwise_correlation = round(average_correlation, 3)
        if max_correlation is not None:
            profile.max_pairwise_correlation = round(max_correlation, 3)

        profile.warnings.extend(self._check_limits(profile, weights))

        logger.info(
            "Risk profile: beta=%s vol=%s%% TE=%s%% (selection %s%% / factor %s%%) "
            "active_share=%s%% eff_positions=%s",
            profile.portfolio_beta,
            profile.portfolio_vol_annualised,
            profile.tracking_error_pct,
            profile.active_selection_share_pct,
            profile.active_factor_share_pct,
            profile.active_share_pct,
            profile.effective_positions,
        )
        return profile

    def _benchmark_weights(self) -> Dict[str, float]:
        """Approximate benchmark weights.

        The fund does not license index weights, so the Nifty 500 is treated as
        equally weighted for active share. That understates active share for a
        large-cap-tilted portfolio, so the figure is read as a floor rather than
        a precise measure.
        """
        symbols = self._universe.symbols
        if not symbols:
            return {}
        weight = 100.0 / len(symbols)
        return {symbol: weight for symbol in symbols}

    def _check_limits(
        self, profile: PortfolioRiskProfile, weights: Dict[str, float]
    ) -> List[str]:
        warnings: List[str] = []
        risk = self._config.risk

        max_beta = risk.get("max_portfolio_beta")
        min_beta = risk.get("min_portfolio_beta")
        if profile.portfolio_beta is not None:
            if max_beta is not None and profile.portfolio_beta > float(max_beta):
                warnings.append(
                    f"Portfolio beta {profile.portfolio_beta} exceeds limit {max_beta}"
                )
            if min_beta is not None and profile.portfolio_beta < float(min_beta):
                warnings.append(
                    f"Portfolio beta {profile.portfolio_beta} below floor {min_beta}"
                )

        max_te = risk.get("max_tracking_error_pct")
        if (
            max_te is not None
            and profile.tracking_error_pct is not None
            and profile.tracking_error_pct > float(max_te)
        ):
            warnings.append(
                f"Tracking error {profile.tracking_error_pct}% exceeds limit {max_te}%"
            )

        min_idio = risk.get("min_idiosyncratic_risk_share_pct")
        if (
            min_idio is not None
            and profile.active_selection_share_pct is not None
            and profile.active_selection_share_pct < float(min_idio)
        ):
            warnings.append(
                f"Only {profile.active_selection_share_pct}% of active risk comes "
                f"from stock selection (floor {min_idio}%); the rest is factor and "
                f"sector tilts, which the fund is not paid to take"
            )

        max_contribution = risk.get("max_single_position_risk_contribution_pct")
        if max_contribution is not None:
            for symbol, metrics in profile.per_position.items():
                if (
                    metrics.risk_contribution_pct is not None
                    and metrics.risk_contribution_pct > float(max_contribution)
                ):
                    warnings.append(
                        f"{symbol} contributes {metrics.risk_contribution_pct}% of "
                        f"portfolio risk (limit {max_contribution}%)"
                    )

        max_adv = self._config.get("universe.max_position_pct_of_adv")
        if max_adv is not None:
            for symbol, metrics in profile.per_position.items():
                if metrics.pct_of_adv is not None and metrics.pct_of_adv > float(max_adv) * 20:
                    warnings.append(
                        f"{symbol} position is {metrics.pct_of_adv:.0f}% of average "
                        f"daily turnover; exiting would take "
                        f"{metrics.days_to_liquidate:.1f} days"
                    )

        if profile.max_pairwise_correlation is not None and profile.max_pairwise_correlation > 0.9:
            warnings.append(
                f"Two holdings are {profile.max_pairwise_correlation:.0%} correlated: "
                f"they are effectively one position"
            )

        return warnings
