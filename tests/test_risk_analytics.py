"""Risk decomposition tests using synthetic return series.

Synthetic data lets us assert the maths rather than the market: a portfolio
built entirely from one factor must show almost no specific risk, and a
portfolio of independent names must show mostly specific risk.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.models import (
    FundamentalSnapshot,
    MarketRegimeSnapshot,
    PortfolioState,
    Position,
    TechnicalSnapshot,
)
from src.risk_analytics.analyzer import RiskAnalyzer
from src.risk_analytics.factor_model import (
    active_share_pct,
    correlation_stats,
    decompose_active_risk,
    decompose_portfolio_risk,
    effective_positions,
    portfolio_risk_contributions,
    regress,
    tracking_error_pct,
)
from src.risk_analytics.market_regime import assess_market_regime

DAYS = 500


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(42)


@pytest.fixture
def benchmark(rng: np.random.Generator) -> pd.Series:
    index = pd.date_range("2024-01-01", periods=DAYS, freq="B")
    return pd.Series(rng.normal(0.0004, 0.010, DAYS), index=index)


def test_regression_recovers_known_beta(benchmark: pd.Series, rng) -> None:
    """A stock built as 1.5x benchmark plus noise must regress to beta 1.5."""
    noise = pd.Series(rng.normal(0, 0.004, DAYS), index=benchmark.index)
    asset = 1.5 * benchmark + noise

    result = regress(asset, benchmark)

    assert result is not None
    assert result.beta == pytest.approx(1.5, abs=0.05)
    assert result.r_squared > 0.8
    assert result.idiosyncratic_vol_annual > 0


def test_pure_factor_portfolio_has_little_specific_risk(
    benchmark: pd.Series, rng
) -> None:
    """A position that is only market exposure carries almost no idio risk."""
    asset = 1.0 * benchmark + pd.Series(
        rng.normal(0, 0.0002, DAYS), index=benchmark.index
    )

    decomposition = decompose_portfolio_risk(asset, benchmark)

    assert decomposition is not None
    _total, factor_share, specific_share = decomposition
    assert factor_share > 90.0
    assert specific_share < 10.0


def test_independent_portfolio_is_mostly_specific_risk(
    benchmark: pd.Series, rng
) -> None:
    """Returns unrelated to the benchmark are almost entirely specific risk."""
    asset = pd.Series(rng.normal(0, 0.015, DAYS), index=benchmark.index)

    decomposition = decompose_portfolio_risk(asset, benchmark)

    assert decomposition is not None
    _total, factor_share, specific_share = decomposition
    assert specific_share > 90.0
    assert factor_share < 10.0


def test_risk_contributions_sum_to_one_hundred(benchmark: pd.Series, rng) -> None:
    returns = pd.DataFrame(
        {
            "A": 1.0 * benchmark + rng.normal(0, 0.006, DAYS),
            "B": 0.8 * benchmark + rng.normal(0, 0.008, DAYS),
            "C": 1.2 * benchmark + rng.normal(0, 0.010, DAYS),
        },
        index=benchmark.index,
    )
    weights = {"A": 50.0, "B": 30.0, "C": 20.0}

    contributions, marginal, portfolio_vol = portfolio_risk_contributions(
        weights, returns
    )

    assert sum(contributions.values()) == pytest.approx(100.0, abs=0.01)
    assert portfolio_vol > 0
    assert set(marginal) == {"A", "B", "C"}


def test_larger_position_in_volatile_name_carries_more_risk(
    benchmark: pd.Series, rng
) -> None:
    """Risk contribution must exceed weight for the most volatile holding."""
    returns = pd.DataFrame(
        {
            "CALM": 0.5 * benchmark + rng.normal(0, 0.002, DAYS),
            "WILD": 1.5 * benchmark + rng.normal(0, 0.030, DAYS),
        },
        index=benchmark.index,
    )
    weights = {"CALM": 50.0, "WILD": 50.0}

    contributions, _marginal, _vol = portfolio_risk_contributions(weights, returns)

    assert contributions["WILD"] > 80.0
    assert contributions["CALM"] < 20.0


def test_tracking_error_is_zero_for_index_replication(benchmark: pd.Series) -> None:
    assert tracking_error_pct(benchmark, benchmark) == pytest.approx(0.0, abs=1e-9)


def test_tracking_error_grows_with_active_deviation(
    benchmark: pd.Series, rng
) -> None:
    active = benchmark + pd.Series(rng.normal(0, 0.005, DAYS), index=benchmark.index)
    error = tracking_error_pct(active, benchmark)
    assert error is not None and error > 5.0


def test_active_share_bounds() -> None:
    identical = {"A": 50.0, "B": 50.0}
    assert active_share_pct(identical, identical) == pytest.approx(0.0)

    disjoint = active_share_pct({"A": 100.0}, {"B": 100.0})
    assert disjoint == pytest.approx(100.0)


def test_effective_positions_reflects_concentration() -> None:
    assert effective_positions([25.0, 25.0, 25.0, 25.0]) == pytest.approx(4.0)
    # One dominant position behaves like far fewer holdings.
    concentrated = effective_positions([70.0, 10.0, 10.0, 10.0])
    assert concentrated is not None and concentrated < 2.2


def test_correlation_stats_detect_duplicate_bets(benchmark: pd.Series, rng) -> None:
    shared = 1.0 * benchmark + rng.normal(0, 0.003, DAYS)
    returns = pd.DataFrame(
        {
            "TWIN_A": shared + rng.normal(0, 0.0005, DAYS),
            "TWIN_B": shared + rng.normal(0, 0.0005, DAYS),
            "OTHER": pd.Series(rng.normal(0, 0.012, DAYS), index=benchmark.index),
        },
        index=benchmark.index,
    )

    average, maximum = correlation_stats(
        {"TWIN_A": 40.0, "TWIN_B": 40.0, "OTHER": 20.0}, returns
    )

    assert maximum is not None and maximum > 0.9
    assert average is not None and average < maximum


def test_regression_returns_none_without_enough_history(benchmark: pd.Series) -> None:
    short = benchmark.head(20)
    assert regress(short, short) is None


def test_active_risk_attributes_sector_bet_to_factors(
    benchmark: pd.Series, rng
) -> None:
    """A portfolio whose active return IS a sector factor is a factor bet."""
    sector_factor = pd.Series(rng.normal(0, 0.008, DAYS), index=benchmark.index)
    factors = pd.DataFrame({"ind_it": sector_factor}, index=benchmark.index)

    # The portfolio is the benchmark plus a large, pure sector tilt.
    portfolio = benchmark + 1.0 * sector_factor + rng.normal(0, 0.0005, DAYS)

    result = decompose_active_risk(portfolio, benchmark, factors)

    assert result is not None
    tracking_error, factor_share, selection_share = result
    assert tracking_error > 5.0
    assert factor_share > 85.0
    assert selection_share < 15.0


def test_active_risk_attributes_stock_picking_to_selection(
    benchmark: pd.Series, rng
) -> None:
    """Active return unrelated to any factor is genuine security selection."""
    sector_factor = pd.Series(rng.normal(0, 0.008, DAYS), index=benchmark.index)
    factors = pd.DataFrame({"ind_it": sector_factor}, index=benchmark.index)

    # Active return is independent noise: pure stock picking, no factor tilt.
    portfolio = benchmark + pd.Series(
        rng.normal(0, 0.006, DAYS), index=benchmark.index
    )

    result = decompose_active_risk(portfolio, benchmark, factors)

    assert result is not None
    _tracking_error, factor_share, selection_share = result
    assert selection_share > 85.0
    assert factor_share < 15.0


def test_active_risk_without_factors_is_all_selection(benchmark: pd.Series, rng) -> None:
    portfolio = benchmark + pd.Series(
        rng.normal(0, 0.005, DAYS), index=benchmark.index
    )
    result = decompose_active_risk(portfolio, benchmark, None)

    assert result is not None
    _error, factor_share, selection_share = result
    assert factor_share == 0.0
    assert selection_share == 100.0


def test_index_replication_has_no_active_risk(benchmark: pd.Series) -> None:
    result = decompose_active_risk(benchmark, benchmark, None)
    assert result is not None
    tracking_error, _factor, _selection = result
    assert tracking_error == pytest.approx(0.0, abs=1e-9)


def _prices_from_returns(returns: pd.Series, start: float = 100.0) -> pd.Series:
    return start * (1.0 + returns).cumprod()


def _market_series(kind: str) -> tuple[pd.Series, dict[str, pd.Series]]:
    index = pd.date_range("2024-01-01", periods=360, freq="B")
    if kind == "calm":
        benchmark_returns = pd.Series(
            np.r_[np.sin(np.arange(260)) * 0.004, np.full(100, 0.0008)],
            index=index,
        )
        names = {
            f"S{i}": _prices_from_returns(
                pd.Series(
                    np.r_[
                        np.sin(np.arange(260) + i) * 0.004,
                        np.full(100, 0.001 + i * 0.00002),
                    ],
                    index=index,
                )
            )
            for i in range(8)
        }
    else:
        common = np.r_[np.full(260, 0.0005), np.tile([-0.035, 0.020], 50)]
        benchmark_returns = pd.Series(common, index=index)
        names = {
            f"S{i}": _prices_from_returns(
                pd.Series(common + (i * 0.00001), index=index)
            )
            for i in range(8)
        }
    return _prices_from_returns(benchmark_returns), names


def test_market_regime_detects_calm_market() -> None:
    benchmark_prices, price_series = _market_series("calm")

    regime = assess_market_regime(benchmark_prices, price_series)

    assert regime.regime == "calm"
    assert regime.score is not None and regime.score < 40.0
    assert set(regime.components) == {
        "volatility",
        "drawdown",
        "breadth",
        "correlation",
    }


def test_market_regime_detects_stressed_market() -> None:
    benchmark_prices, price_series = _market_series("stressed")

    regime = assess_market_regime(benchmark_prices, price_series)

    assert regime.regime == "stressed"
    assert regime.score is not None and regime.score >= 65.0
    assert regime.warnings


def test_market_regime_is_unknown_without_enough_inputs() -> None:
    index = pd.date_range("2026-01-01", periods=30, freq="B")
    benchmark_prices = pd.Series(np.linspace(100.0, 101.0, len(index)), index=index)

    regime = assess_market_regime(benchmark_prices, {})

    assert regime.regime == "unknown"
    assert regime.score is None
    assert "fewer than three inputs" in regime.warnings[0]


class FakePriceService:
    def __init__(self, benchmark_prices, price_series):
        self._benchmark_prices = benchmark_prices
        self._price_series = price_series

    def benchmark_series(self):
        return self._benchmark_prices

    def close_series(self, symbol):
        return self._price_series.get(symbol)


class FakeUniverse:
    def __init__(self, symbols):
        self.symbols = symbols

    def sector_of(self, _symbol):
        return "Information Technology"


def test_risk_analyzer_populates_market_regime_and_warnings() -> None:
    benchmark_prices, price_series = _market_series("stressed")
    symbols = list(price_series)
    portfolio = PortfolioState(
        as_of=benchmark_prices.index[-1].date(),
        cash=100_000.0,
        positions=[
            Position(
                symbol=symbols[0],
                quantity=10,
                avg_cost=100.0,
                last_price=100.0,
                sector="Information Technology",
            )
        ],
    )
    technicals = {
        symbol: TechnicalSnapshot(symbol=symbol, above_sma_200=False)
        for symbol in symbols
    }
    fundamentals = {symbol: FundamentalSnapshot(symbol=symbol) for symbol in symbols}

    profile = RiskAnalyzer(
        price_service=FakePriceService(benchmark_prices, price_series),
        universe=FakeUniverse(symbols),
    ).analyse(portfolio, fundamentals, technicals, universe_symbols=symbols)

    assert isinstance(profile.market_regime, MarketRegimeSnapshot)
    assert profile.market_regime.regime == "stressed"
    assert any("Market regime is stressed" in warning for warning in profile.warnings)
