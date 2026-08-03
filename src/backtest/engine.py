"""Historical backtesting engine for persona strategies.

Simulates the daily fund cycle over a historical period using rule-based
decision logic (no LLM calls). This keeps backtests deterministic, fast,
and free of API costs while still exercising the scoring and trigger engines.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from src.config import Config, get_persona_config
from src.logging_setup import get_logger
from src.market_data.indicators import compute_snapshot
from src.market_data.prices import PriceService
from src.models import (
    Action,
    FactorScores,
    FundamentalSnapshot,
    PortfolioState,
    Position,
    Severity,
    TechnicalSnapshot,
    Trigger,
)
from src.quant.scoring import ScoringEngine
from src.risk.triggers import TriggerEngine
from src.universe import Universe, load_universe

logger = get_logger(__name__)


@dataclass
class BacktestResult:
    persona_id: str
    start_date: date
    end_date: date
    total_return_pct: float
    annualised_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: Optional[float]
    sortino_ratio: Optional[float]
    total_trades: int
    turnover_pct: float
    win_rate_pct: Optional[float]
    daily_returns: List[float] = field(default_factory=list)
    nav_series: List[Tuple[date, float]] = field(default_factory=list)


def _trading_days(frames: Dict[str, pd.DataFrame], start: date, end: date) -> List[date]:
    """Union of all available trading dates across all price frames."""
    all_dates: set = set()
    for frame in frames.values():
        idx = frame.index
        for ts in idx:
            d = ts.date() if hasattr(ts, "date") else ts
            if start <= d <= end:
                all_dates.add(d)
    return sorted(all_dates)


def _compute_technicals_as_of(
    frames: Dict[str, pd.DataFrame], symbols: List[str], as_of: date
) -> Dict[str, TechnicalSnapshot]:
    """Compute technical snapshots using only data up to *as_of*."""
    out: Dict[str, TechnicalSnapshot] = {}
    for symbol in symbols:
        frame = frames.get(symbol)
        if frame is None:
            continue
        sliced = frame.loc[frame.index <= pd.Timestamp(as_of)]
        if len(sliced) < 20:
            continue
        payload = compute_snapshot(sliced, symbol)
        out[symbol] = TechnicalSnapshot(**payload)
    return out


class BacktestEngine:
    """Simulate the full daily cycle over a historical period.

    For each trading day in the range:
    1. Load historical prices up to that date
    2. Run the scoring engine with those prices
    3. Run the trigger engine against current positions
    4. Apply rule-based decisions (no LLM)
    5. Simulate order execution at next day's open
    6. Record NAV
    """

    def __init__(
        self,
        config: Config,
        start_date: date,
        end_date: date,
        initial_capital: Optional[float] = None,
    ):
        self._config = config
        self._start_date = start_date
        self._end_date = end_date
        self._initial_capital = initial_capital or config.secrets.initial_capital
        self._universe: Universe = load_universe(config)
        self._price_service = PriceService(config)
        self._scoring_engine = ScoringEngine(config, self._universe)
        self._trigger_engine = TriggerEngine(config)

        self._min_buy_score = float(config.get("quant.min_buy_score", 0.5) or 0.5)
        self._target_positions = int(config.get("fund.target_positions", 25) or 25)
        self._cash_buffer_pct = float(config.get("fund.min_cash_buffer_pct", 5.0) or 5.0)
        self._max_position_pct = float(config.get("risk.max_position_pct", 8.0) or 8.0)

    def run(self) -> BacktestResult:
        """Execute the backtest and return performance metrics."""
        logger.info(
            "Starting backtest for persona=%s from %s to %s (capital=%.0f)",
            self._config.persona_id,
            self._start_date,
            self._end_date,
            self._initial_capital,
        )

        frames = self._price_service.cached_history()
        if not frames:
            symbols = self._universe.symbols
            frames = self._price_service.load_history(symbols, days=900)

        if not frames:
            logger.error("No price data available for backtesting")
            return self._empty_result()

        days = _trading_days(frames, self._start_date, self._end_date)
        if len(days) < 5:
            logger.error(
                "Insufficient trading days (%d) in range %s to %s",
                len(days), self._start_date, self._end_date,
            )
            return self._empty_result()

        logger.info("Backtesting over %d trading days", len(days))

        portfolio = PortfolioState(
            as_of=days[0], cash=self._initial_capital, positions=[]
        )
        nav_series: List[Tuple[date, float]] = []
        total_traded_value = 0.0
        trades_executed = 0
        winning_trades = 0
        closed_trades = 0

        # Pre-load a static fundamentals approximation from cached data.
        # Real backtesting would use point-in-time fundamentals; here we use the
        # latest cached snapshot as a simplification.
        fundamentals = self._load_cached_fundamentals()

        for day_idx, today in enumerate(days):
            portfolio = PortfolioState(
                as_of=today,
                cash=portfolio.cash,
                positions=portfolio.positions,
                realised_pnl_to_date=portfolio.realised_pnl_to_date,
            )

            # Update position prices to today's close.
            self._update_prices(portfolio, frames, today)
            nav_series.append((today, portfolio.total_value))

            # Score only periodically (weekly) to keep backtest fast.
            run_scoring = day_idx % 5 == 0
            scores: Dict[str, FactorScores] = {}
            if run_scoring:
                technicals = _compute_technicals_as_of(
                    frames, self._universe.symbols, today
                )
                if technicals and fundamentals:
                    scores = self._scoring_engine.score(fundamentals, technicals)

            # Evaluate triggers against current positions.
            triggers: List[Trigger] = []
            if portfolio.positions:
                pos_technicals = _compute_technicals_as_of(
                    frames, portfolio.symbols, today
                )
                triggers = self._trigger_engine.evaluate(
                    portfolio=portfolio,
                    scores=scores,
                    fundamentals=fundamentals,
                    technicals=pos_technicals,
                )

            # Rule-based decisions.
            sell_value, buy_value, n_trades, wins, closes = self._execute_rules(
                portfolio, triggers, scores, frames, today, days, day_idx
            )
            total_traded_value += sell_value + buy_value
            trades_executed += n_trades
            winning_trades += wins
            closed_trades += closes

        # Compute performance metrics.
        return self._compute_metrics(
            nav_series, total_traded_value, trades_executed,
            winning_trades, closed_trades,
        )

    def _empty_result(self) -> BacktestResult:
        return BacktestResult(
            persona_id=self._config.persona_id,
            start_date=self._start_date,
            end_date=self._end_date,
            total_return_pct=0.0,
            annualised_return_pct=0.0,
            max_drawdown_pct=0.0,
            sharpe_ratio=None,
            sortino_ratio=None,
            total_trades=0,
            turnover_pct=0.0,
            win_rate_pct=None,
        )

    def _load_cached_fundamentals(self) -> Dict[str, FundamentalSnapshot]:
        """Load whatever fundamentals are cached on disk (no network calls)."""
        import json
        cache_path = self._config.cache_dir / "fundamentals.json"
        if not cache_path.exists():
            logger.warning("No cached fundamentals found at %s", cache_path)
            return {}

        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:
            logger.warning("Could not read fundamentals cache: %s", exc)
            return {}

        from dataclasses import fields as dc_fields
        valid_fields = {f.name for f in dc_fields(FundamentalSnapshot)}
        out: Dict[str, FundamentalSnapshot] = {}
        for symbol, record in raw.items():
            payload = {k: v for k, v in record.items() if k in valid_fields}
            payload["symbol"] = symbol
            payload.pop("as_of", None)
            try:
                out[symbol] = FundamentalSnapshot(**payload)
            except (TypeError, ValueError):
                continue
        logger.info("Loaded %d fundamental snapshots for backtest", len(out))
        return out

    def _update_prices(
        self, portfolio: PortfolioState, frames: Dict[str, pd.DataFrame], as_of: date
    ) -> None:
        """Update each position's last_price and peak_price to the close on *as_of*."""
        for position in portfolio.positions:
            frame = frames.get(position.symbol)
            if frame is None:
                continue
            sliced = frame.loc[frame.index <= pd.Timestamp(as_of)]
            if sliced.empty or "Close" not in sliced.columns:
                continue
            close = float(sliced["Close"].iloc[-1])
            position.last_price = close
            if position.peak_price is None or close > position.peak_price:
                position.peak_price = close

    def _execute_rules(
        self,
        portfolio: PortfolioState,
        triggers: List[Trigger],
        scores: Dict[str, FactorScores],
        frames: Dict[str, pd.DataFrame],
        today: date,
        days: List[date],
        day_idx: int,
    ) -> Tuple[float, float, int, int, int]:
        """Apply rule-based trading logic. Returns (sell_value, buy_value, trades, wins, closes)."""
        sell_value = 0.0
        buy_value = 0.0
        trades = 0
        wins = 0
        closes = 0

        # Get next day's open for execution price (or use today's close as proxy).
        next_day = days[day_idx + 1] if day_idx + 1 < len(days) else None

        # 1. Execute MANDATORY triggers.
        for trigger in triggers:
            if trigger.severity != Severity.MANDATORY:
                continue
            if trigger.suggested_action in (Action.SELL, Action.TRIM):
                pnl = self._sell_position(
                    portfolio, trigger.symbol, trigger.suggested_quantity, frames, next_day or today
                )
                if pnl is not None:
                    sell_value += abs(pnl[0])
                    trades += 1
                    closes += 1
                    if pnl[1] > 0:
                        wins += 1

        # 2. REVIEW triggers: sell if score is below min_buy_score.
        for trigger in triggers:
            if trigger.severity != Severity.REVIEW:
                continue
            if trigger.suggested_action not in (Action.SELL, Action.TRIM):
                continue
            score = scores.get(trigger.symbol)
            if score and score.composite < self._min_buy_score:
                pnl = self._sell_position(
                    portfolio, trigger.symbol, trigger.suggested_quantity, frames, next_day or today
                )
                if pnl is not None:
                    sell_value += abs(pnl[0])
                    trades += 1
                    closes += 1
                    if pnl[1] > 0:
                        wins += 1

        # 3. Buy if cash is sufficient and candidates exist.
        if scores and day_idx % 5 == 0:
            cash_threshold = self._initial_capital * (self._cash_buffer_pct / 100.0)
            available_cash = portfolio.cash - cash_threshold
            if available_cash > 0 and len(portfolio.positions) < self._target_positions:
                candidates = self._scoring_engine.buy_candidates(
                    scores, exclude=portfolio.symbols
                )
                position_size = portfolio.total_value / max(self._target_positions, 1)
                max_buy = min(position_size, available_cash)

                for candidate in candidates[:3]:
                    if max_buy < 10000:
                        break
                    if len(portfolio.positions) >= self._target_positions:
                        break
                    price = self._get_execution_price(
                        candidate.symbol, frames, next_day or today
                    )
                    if price is None or price <= 0:
                        continue
                    quantity = int(max_buy / price)
                    if quantity < 1:
                        continue
                    cost = quantity * price
                    portfolio.cash -= cost
                    portfolio.positions.append(Position(
                        symbol=candidate.symbol,
                        quantity=quantity,
                        avg_cost=price,
                        last_price=price,
                        sector=self._universe.sector_of(candidate.symbol),
                        entry_date=today,
                        peak_price=price,
                    ))
                    buy_value += cost
                    trades += 1
                    available_cash -= cost
                    max_buy = min(position_size, available_cash)

        return sell_value, buy_value, trades, wins, closes

    def _sell_position(
        self,
        portfolio: PortfolioState,
        symbol: str,
        quantity: Optional[int],
        frames: Dict[str, pd.DataFrame],
        exec_date: date,
    ) -> Optional[Tuple[float, float]]:
        """Sell shares. Returns (trade_value, pnl) or None."""
        position = portfolio.get(symbol)
        if position is None:
            return None

        qty = min(quantity or position.quantity, position.quantity)
        price = self._get_execution_price(symbol, frames, exec_date)
        if price is None:
            price = position.last_price

        trade_value = qty * price
        pnl = (price - position.avg_cost) * qty

        if qty >= position.quantity:
            portfolio.positions = [p for p in portfolio.positions if p.symbol != symbol]
        else:
            position.quantity -= qty

        portfolio.cash += trade_value
        portfolio.realised_pnl_to_date += pnl
        return (trade_value, pnl)

    def _get_execution_price(
        self, symbol: str, frames: Dict[str, pd.DataFrame], exec_date: date
    ) -> Optional[float]:
        """Get the open price on exec_date as execution price, fallback to close."""
        frame = frames.get(symbol)
        if frame is None:
            return None
        sliced = frame.loc[frame.index <= pd.Timestamp(exec_date)]
        if sliced.empty:
            return None
        last_row = sliced.iloc[-1]
        if "Open" in sliced.columns and not math.isnan(last_row.get("Open", float("nan"))):
            return float(last_row["Open"])
        if "Close" in sliced.columns:
            return float(last_row["Close"])
        return None

    def _compute_metrics(
        self,
        nav_series: List[Tuple[date, float]],
        total_traded_value: float,
        total_trades: int,
        winning_trades: int,
        closed_trades: int,
    ) -> BacktestResult:
        """Derive performance metrics from the NAV series."""
        if len(nav_series) < 2:
            return self._empty_result()

        navs = [nav for _, nav in nav_series]
        start_nav = navs[0]
        end_nav = navs[-1]

        total_return_pct = (end_nav / start_nav - 1.0) * 100.0

        start_d = nav_series[0][0]
        end_d = nav_series[-1][0]
        years = max((end_d - start_d).days / 365.25, 0.01)
        annualised = ((end_nav / start_nav) ** (1.0 / years) - 1.0) * 100.0

        # Daily returns.
        daily_returns: List[float] = []
        for i in range(1, len(navs)):
            if navs[i - 1] > 0:
                daily_returns.append(navs[i] / navs[i - 1] - 1.0)

        # Max drawdown.
        peak = navs[0]
        max_dd = 0.0
        for nav in navs:
            if nav > peak:
                peak = nav
            dd = (nav / peak - 1.0) * 100.0
            if dd < max_dd:
                max_dd = dd

        # Sharpe and Sortino (assuming 6% risk-free for INR).
        sharpe: Optional[float] = None
        sortino: Optional[float] = None
        if daily_returns:
            import numpy as np
            dr = np.array(daily_returns)
            mean_daily = float(np.mean(dr))
            std_daily = float(np.std(dr, ddof=1)) if len(dr) > 1 else 0.0
            rf_daily = 0.06 / 252.0

            if std_daily > 0:
                sharpe = (mean_daily - rf_daily) / std_daily * math.sqrt(252)

            downside = dr[dr < rf_daily] - rf_daily
            if len(downside) > 1:
                downside_std = float(np.std(downside, ddof=1))
                if downside_std > 0:
                    sortino = (mean_daily - rf_daily) / downside_std * math.sqrt(252)

        # Turnover.
        avg_nav = sum(navs) / len(navs)
        turnover_pct = (total_traded_value / 2.0) / avg_nav * 100.0 if avg_nav > 0 else 0.0

        win_rate: Optional[float] = None
        if closed_trades > 0:
            win_rate = winning_trades / closed_trades * 100.0

        return BacktestResult(
            persona_id=self._config.persona_id,
            start_date=start_d,
            end_date=end_d,
            total_return_pct=round(total_return_pct, 2),
            annualised_return_pct=round(annualised, 2),
            max_drawdown_pct=round(max_dd, 2),
            sharpe_ratio=round(sharpe, 3) if sharpe is not None else None,
            sortino_ratio=round(sortino, 3) if sortino is not None else None,
            total_trades=total_trades,
            turnover_pct=round(turnover_pct, 1),
            win_rate_pct=round(win_rate, 1) if win_rate is not None else None,
            daily_returns=daily_returns,
            nav_series=nav_series,
        )
