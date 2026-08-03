"""Portfolio-level simulation (spec §7.2): the cost/turnover reality check.

Three sims, all share-based with exact cash accounting (a unit test proves
cash + positions reconcile to the last cent):

1. ``unconstrained_sim`` — §7.2(a): monthly rebalance to equal weight across
   every name with an active signal; cash earns 0% when nothing is active.
   Raw net-of-cost alpha, no fund constraints.
2. ``overlay_sim`` — §7.2(b): a 25-name book mirroring the fund's rules
   (3.8% target weight, 5% cash buffer, 25% sector cap, quarterly deep
   reviews on the config cadence, 75%/yr turnover budget enforced and
   measured exactly as src/risk/turnover.py does: churn = min(purchases,
   sales) / average net assets over a trailing 365 days; only discretionary
   trades are budget-blocked — S3 risk exits are mandatory and always fire).
   Buys happen only at deep reviews; S3 CONFIRMED sells happen immediately
   at the signal's entry close. A no-signal baseline book with the same
   initial holdings measures incremental turnover.
3. ``avoidance_sim`` — §5.3.2: per S3 CONFIRMED event, sell at the entry
   close, hold cash until CLEAR, rebuy at CLEAR's entry close. Benefit =
   -(market-adjusted return signal→CLEAR) - round-trip cost vs hold-through.

Spec-silent choices (all logged in the report): sim trades execute at the
rebalance/review day's CLOSE using only signals with availability strictly
before that day; the initial overlay book is the 25 highest-turnover names
as of the sim start; replacement candidates are ranked by the active
signal's own value (S1/S2) or latest SUE-CS (S3 refill); holdings score 0
when they have no recent signal of the overlay's family; §7.5's one-way
turnover ((buys+sells)/2 / avg NAV) is reported alongside the production
min-convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.backtest.signals import EventSet, PriceTable
from src.logging_setup import get_logger

logger = get_logger(__name__)

SIDE_COST = {"us": 0.00025, "india": 0.0015}  # §7.3 per side
TARGET_WEIGHT = 0.038  # (100 - 5% cash) / 25, §7.2(b)
N_POSITIONS = 25
SECTOR_CAP = 0.25
TURNOVER_BUDGET_PCT = 75.0
DEEP_REVIEW_MONTHS = (1, 4, 7, 10)  # config cadence.deep_review_months


# --------------------------------------------------------------------- ledger


@dataclass
class Trade:
    date: pd.Timestamp
    symbol: str
    side: str  # "buy" | "sell"
    value: float  # gross of cost
    cost: float
    discretionary: bool = True


class Ledger:
    """Cash + share positions with per-side transaction costs."""

    def __init__(self, cash: float, side_cost: float):
        self.cash = float(cash)
        self.side_cost = side_cost
        self.positions: Dict[str, float] = {}  # symbol -> shares
        self.trades: List[Trade] = []

    def buy(self, day: pd.Timestamp, symbol: str, value: float, price: float,
            discretionary: bool = True) -> float:
        """Spend ``value`` (gross) buying at ``price``; returns shares added."""
        if value <= 0 or price <= 0:
            return 0.0
        cost = value * self.side_cost
        total = value + cost
        if total > self.cash:
            value = max(0.0, (self.cash - 1e-9) / (1.0 + self.side_cost))
            cost = value * self.side_cost
            total = value + cost
            if value <= 0:
                return 0.0
        shares = value / price
        self.cash -= total
        self.positions[symbol] = self.positions.get(symbol, 0.0) + shares
        self.trades.append(Trade(day, symbol, "buy", value, cost, discretionary))
        return shares

    def sell(self, day: pd.Timestamp, symbol: str, shares: float, price: float,
             discretionary: bool = True) -> float:
        """Sell ``shares`` at ``price``; returns gross proceeds."""
        held = self.positions.get(symbol, 0.0)
        shares = min(shares, held)
        if shares <= 0 or price <= 0:
            return 0.0
        value = shares * price
        cost = value * self.side_cost
        self.cash += value - cost
        remaining = held - shares
        if remaining <= 1e-12:
            self.positions.pop(symbol, None)
        else:
            self.positions[symbol] = remaining
        self.trades.append(Trade(day, symbol, "sell", value, cost, discretionary))
        return value

    def sell_all(self, day: pd.Timestamp, symbol: str, price: float,
                 discretionary: bool = True) -> float:
        return self.sell(day, symbol, self.positions.get(symbol, 0.0), price,
                         discretionary)

    def market_value(self, price_fn: Callable[[str], Optional[float]]) -> float:
        total = 0.0
        for symbol, shares in self.positions.items():
            price = price_fn(symbol)
            if price is not None:
                total += shares * price
        return total

    def nav(self, price_fn: Callable[[str], Optional[float]]) -> float:
        return self.cash + self.market_value(price_fn)


# ------------------------------------------------------------- metric helpers


@dataclass
class SimResult:
    name: str
    nav: pd.Series  # daily NAV
    benchmark: pd.Series  # benchmark close aligned to nav index
    trades: List[Trade]
    avg_positions: float
    avg_exposure: float
    extra: Dict[str, float] = field(default_factory=dict)

    @property
    def returns(self) -> pd.Series:
        return self.nav.pct_change().dropna()

    def _annual_turnover(self) -> Tuple[pd.Series, pd.Series]:
        """(buys+sells)/2 and min(buys,sells) per calendar year, gross values."""
        if not self.trades:
            empty = pd.Series(dtype=float)
            return empty, empty
        frame = pd.DataFrame(
            {"date": [t.date for t in self.trades],
             "buy": [t.value if t.side == "buy" else 0.0 for t in self.trades],
             "sell": [t.value if t.side == "sell" else 0.0 for t in self.trades]}
        )
        yearly = frame.groupby(frame.date.dt.year)[["buy", "sell"]].sum()
        avg_nav = self.nav.groupby(self.nav.index.year).mean()
        half = (yearly.buy + yearly.sell) / 2 / avg_nav
        churn = yearly[["buy", "sell"]].min(axis=1) / avg_nav
        return half, churn

    def metrics(self) -> Dict[str, float]:
        nav = self.nav.dropna()
        if len(nav) < 2:
            return {}
        years = (nav.index[-1] - nav.index[0]).days / 365.25
        cagr = float((nav.iloc[-1] / nav.iloc[0]) ** (1 / max(years, 1e-9)) - 1)
        bench = self.benchmark.reindex(nav.index).ffill()
        wealth_ratio = (nav / nav.iloc[0]) / (bench / bench.iloc[0])
        excess_cagr = float(wealth_ratio.iloc[-1] ** (1 / max(years, 1e-9)) - 1)
        rets = nav.pct_change().dropna()
        sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else float("nan")
        drawdown = (nav / nav.cummax() - 1).min()
        half, churn = self._annual_turnover()
        per_year_excess = (
            nav.groupby(nav.index.year).last().pct_change()
            - bench.groupby(bench.index.year).last().pct_change()
        )
        out = {
            "cagr": cagr,
            "excess_cagr_geometric": excess_cagr,
            "sharpe_rf0": sharpe,
            "max_drawdown": float(drawdown),
            "avg_positions": self.avg_positions,
            "avg_exposure": self.avg_exposure,
            "turnover_oneway_mean": float(half.mean()) if len(half) else 0.0,
            "turnover_churn_mean": float(churn.mean()) if len(churn) else 0.0,
            "years": years,
        }
        out.update(self.extra)
        self.per_year_excess = per_year_excess.dropna()
        self.annual_turnover_half = half
        self.annual_turnover_churn = churn
        return out


class DailyPrices:
    """Point-in-time price access for sims (at-or-before close)."""

    def __init__(self, price_table_fn: Callable[[str], Optional[PriceTable]]):
        self._tables = price_table_fn

    def at(self, symbol: str, day: pd.Timestamp) -> Optional[float]:
        table = self._tables(symbol)
        if table is None:
            return None
        return table.price_at_or_before(day)


def trading_days(event_set: EventSet, start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    """Benchmark trading calendar between two dates (inclusive)."""
    from src.backtest.signals import EventBuilder

    cal = EventBuilder(event_set.market, store=None).calendar()
    days = pd.DatetimeIndex(cal.dates)
    return days[(days >= start.normalize()) & (days <= end.normalize())]


def deep_review_days(days: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """First trading day of each deep-review month (config cadence)."""
    series = pd.Series(days, index=days)
    firsts = series.groupby([days.year, days.month]).first()
    return pd.DatetimeIndex(d for d in firsts if d.month in DEEP_REVIEW_MONTHS)


def month_starts(days: pd.DatetimeIndex) -> pd.DatetimeIndex:
    series = pd.Series(days, index=days)
    return pd.DatetimeIndex(series.groupby([days.year, days.month]).first())


# ------------------------------------------------------- unconstrained sim (a)


def unconstrained_sim(
    event_set: EventSet,
    events: pd.DataFrame,
    active_fn: Callable[[pd.DataFrame, pd.Timestamp], pd.Series],
    start: pd.Timestamp,
    end: pd.Timestamp,
    price_table_fn: Callable[[str], Optional[PriceTable]],
    initial_capital: float = 1_000_000.0,
    name: str = "unconstrained",
) -> SimResult:
    """Monthly equal-weight rebalance across names with an active signal."""
    days = trading_days(event_set, start, end)
    rebalances = set(month_starts(days))
    ledger = Ledger(initial_capital, SIDE_COST[event_set.market])
    prices = DailyPrices(price_table_fn)
    nav_series: Dict[pd.Timestamp, float] = {}
    exposure: List[float] = []
    positions_count: List[int] = []

    events = events.sort_values("entry_date")
    for day in days:
        if day in rebalances:
            active = events[active_fn(events, day)]
            targets = sorted(active.symbol.unique())
            # Sell everything not targeted; resize to equal weight at close.
            close_prices = {s: prices.at(s, day) for s in
                            set(ledger.positions) | set(targets)}
            for symbol in list(ledger.positions):
                if symbol not in targets and close_prices.get(symbol):
                    ledger.sell_all(day, symbol, close_prices[symbol])
            nav_now = ledger.nav(lambda s: close_prices.get(s) or prices.at(s, day))
            weight = 1.0 / len(targets) if targets else 0.0
            for symbol in targets:
                price = close_prices.get(symbol)
                if not price:
                    continue
                current = ledger.positions.get(symbol, 0.0) * price
                target_value = nav_now * weight
                if current > target_value * 1.001:
                    ledger.sell(day, symbol, (current - target_value) / price, price)
                elif current < target_value * 0.999:
                    ledger.buy(day, symbol, target_value - current, price)
        nav = ledger.nav(lambda s: prices.at(s, day))
        invested = nav - ledger.cash
        exposure.append(invested / nav if nav else 0.0)
        positions_count.append(len(ledger.positions))
        nav_series[day] = nav

    nav = pd.Series(nav_series).sort_index()
    bench = _benchmark_series(event_set, nav.index, price_table_fn)
    return SimResult(
        name=name, nav=nav, benchmark=bench, trades=ledger.trades,
        avg_positions=float(np.mean(positions_count)) if positions_count else 0.0,
        avg_exposure=float(np.mean(exposure)) if exposure else 0.0,
    )


def _benchmark_series(event_set: EventSet, index: pd.DatetimeIndex,
                      price_table_fn) -> pd.Series:
    table = price_table_fn(event_set.benchmark)
    if table is None:
        return pd.Series(np.nan, index=index)
    values = [table.price_at_or_before(d) for d in index]
    return pd.Series(values, index=index, dtype=float).ffill()


# ------------------------------------------------------------- overlay sim (b)


@dataclass
class OverlayConfig:
    n_positions: int = N_POSITIONS
    target_weight: float = TARGET_WEIGHT
    sector_cap: float = SECTOR_CAP
    budget_pct: float = TURNOVER_BUDGET_PCT


class BudgetTracker:
    """Trailing-365d churn = min(buys, sells)/avg NAV, as src/risk/turnover.py."""

    def __init__(self, budget_pct: float = TURNOVER_BUDGET_PCT):
        self.budget_pct = budget_pct
        self._entries: List[Tuple[pd.Timestamp, float, float]] = []  # day, buy, sell
        self._navs: List[Tuple[pd.Timestamp, float]] = []

    def record_day(self, day: pd.Timestamp, nav: float) -> None:
        self._navs.append((day, nav))

    def record_trades(self, day: pd.Timestamp, trades: Sequence[Trade]) -> None:
        buy = sum(t.value for t in trades if t.side == "buy")
        sell = sum(t.value for t in trades if t.side == "sell")
        self._entries.append((day, buy, sell))

    def would_exceed(self, day: pd.Timestamp, trade_value: float) -> bool:
        window_start = day - pd.Timedelta(days=365)
        buys = sum(b for d, b, _ in self._entries if d > window_start)
        sells = sum(s for d, _, s in self._entries if d > window_start)
        navs = [n for d, n in self._navs if d > window_start]
        avg_nav = float(np.mean(navs)) if navs else 0.0
        if avg_nav <= 0:
            return False
        churn = min(buys, sells) + trade_value
        return (churn / avg_nav * 100.0) > self.budget_pct


def overlay_sim(
    event_set: EventSet,
    buy_events: Optional[pd.DataFrame],
    sell_events: Optional[pd.DataFrame],
    buy_active_fn: Optional[Callable[[pd.DataFrame, pd.Timestamp], pd.Series]],
    start: pd.Timestamp,
    end: pd.Timestamp,
    price_table_fn: Callable[[str], Optional[PriceTable]],
    sectors: Dict[str, str],
    refill_score_fn: Optional[Callable[[str, pd.Timestamp], float]] = None,
    initial_capital: float = 1_000_000.0,
    run_baseline: bool = True,
    name: str = "overlay",
) -> Tuple[SimResult, Optional[SimResult]]:
    """25-name fund book with signal overlay; returns (overlay, baseline)."""
    days = trading_days(event_set, start, end)
    reviews = set(deep_review_days(days))
    prices = DailyPrices(price_table_fn)

    # Initial book: 25 highest trailing turnover names as of start.
    liquidity = []
    for symbol in sorted(event_set.series):
        table = price_table_fn(symbol)
        if table is None:
            continue
        to = table.average_daily_turnover(days[0])
        if to is not None:
            liquidity.append((to, symbol))
    liquidity.sort(reverse=True)
    initial = [s for _, s in liquidity[: N_POSITIONS]]
    if len(initial) < N_POSITIONS:
        logger.warning("overlay: only %d liquid names at start", len(initial))

    sell_events = sell_events if sell_events is not None else pd.DataFrame()
    buy_events = buy_events if buy_events is not None else pd.DataFrame()

    def run(with_signals: bool) -> Ledger:
        ledger = Ledger(initial_capital, SIDE_COST[event_set.market])
        budget = BudgetTracker()
        refill_queue: List[str] = []  # symbols sold on S3, cash parked until review
        day0 = days[0]
        for i, symbol in enumerate(initial):
            price = prices.at(symbol, day0)
            if price:
                ledger.buy(day0, symbol, initial_capital * TARGET_WEIGHT, price)
        sold_flags: Dict[str, pd.Timestamp] = {}

        for day in days:
            day_trades: List[Trade] = []

            # 1) S3 immediate exits (mandatory — never budget-blocked).
            if with_signals and not sell_events.empty:
                fired = sell_events[sell_events.entry_date == day]
                for event in fired.itertuples():
                    if event.symbol in ledger.positions:
                        price = prices.at(event.symbol, day)
                        if price:
                            n0 = len(ledger.trades)
                            ledger.sell_all(day, event.symbol, price, discretionary=False)
                            refill_queue.append(event.symbol)
                            sold_flags[event.symbol] = day
                            day_trades.extend(ledger.trades[n0:])

            # 2) Quarterly deep review: signal-driven replacements + rebalance.
            if day in reviews:
                nav_now = ledger.nav(lambda s: prices.at(s, day))
                sector_nav = _sector_values(ledger, prices, day, sectors)

                if with_signals and buy_active_fn is not None and not buy_events.empty:
                    active = buy_events[buy_active_fn(buy_events, day)]
                    # The evaluation layer always normalises the family's signal
                    # strength into a ``signal_score`` column before calling.
                    cand_scores = active.groupby("symbol").signal_score.max()
                    # Holdings ranked weakest-first by their own latest signal
                    # score (no recent signal => 0, neutral).
                    held_scores = {
                        s: _latest_score(buy_events, s, day) for s in ledger.positions
                    }
                    candidates = [
                        (score, symbol)
                        for symbol, score in cand_scores.sort_values(ascending=False).items()
                        if symbol not in ledger.positions and symbol not in refill_queue
                    ]
                    holdings = sorted(held_scores.items(), key=lambda kv: kv[1])
                    for score, symbol in candidates:
                        if not holdings:
                            break
                        weakest, weakest_score = holdings[0]  # (symbol, score), weakest first
                        if score <= weakest_score:
                            break
                        price_buy = prices.at(symbol, day)
                        price_sell = prices.at(weakest, day)
                        if not price_buy or not price_sell:
                            continue
                        if not _sector_ok(sector_nav, nav_now, sectors, symbol, weakest):
                            continue
                        value = ledger.positions[weakest] * price_sell
                        if budget.would_exceed(day, value):
                            break  # discretionary budget spent; stop replacing
                        n0 = len(ledger.trades)
                        ledger.sell_all(day, weakest, price_sell)
                        ledger.buy(day, symbol, value, price_buy)
                        day_trades.extend(ledger.trades[n0:])
                        holdings.pop(0)
                        sector_nav = _sector_values(ledger, prices, day, sectors)

                # Refill S3 vacancies with the best non-signalled candidate.
                if with_signals and refill_queue:
                    while refill_queue and len(ledger.positions) < N_POSITIONS:
                        refill_queue.pop(0)
                        target_value = ledger.nav(lambda s: prices.at(s, day)) * TARGET_WEIGHT
                        if ledger.cash < target_value:
                            break
                        choice = _pick_refill(
                            event_set, ledger, day, refill_score_fn, sectors,
                            sector_nav, sold_flags,
                            ledger.nav(lambda s: prices.at(s, day)),
                        )
                        if choice is None:
                            break
                        price = prices.at(choice, day)
                        if price and not budget.would_exceed(day, target_value):
                            n0 = len(ledger.trades)
                            ledger.buy(day, choice, target_value, price)
                            day_trades.extend(ledger.trades[n0:])
                            sector_nav = _sector_values(ledger, prices, day, sectors)

                # Drift rebalance toward 3.8% targets (discretionary).
                nav_now = ledger.nav(lambda s: prices.at(s, day))
                for symbol in list(ledger.positions):
                    price = prices.at(symbol, day)
                    if not price:
                        continue
                    current = ledger.positions[symbol] * price
                    target_value = nav_now * TARGET_WEIGHT
                    gap = abs(current - target_value)
                    if gap < nav_now * 0.004:  # ignore <0.4% NAV adjustments
                        continue
                    if budget.would_exceed(day, gap):
                        continue
                    n0 = len(ledger.trades)
                    if current > target_value:
                        ledger.sell(day, symbol, (current - target_value) / price, price)
                    else:
                        ledger.buy(day, symbol, gap, price)
                    day_trades.extend(ledger.trades[n0:])

            nav = ledger.nav(lambda s: prices.at(s, day))
            budget.record_day(day, nav)
            if day_trades:
                budget.record_trades(day, day_trades)
            yield day, ledger, nav

    def execute(with_signals: bool, label: str) -> SimResult:
        nav_series: Dict[pd.Timestamp, float] = {}
        exposures: List[float] = []
        counts: List[int] = []
        final_ledger: Optional[Ledger] = None
        for day, ledger, nav in run(with_signals):
            nav_series[day] = nav
            exposures.append((nav - ledger.cash) / nav if nav else 0.0)
            counts.append(len(ledger.positions))
            final_ledger = ledger
        nav = pd.Series(nav_series).sort_index()
        bench = _benchmark_series(event_set, nav.index, price_table_fn)
        return SimResult(
            name=label, nav=nav, benchmark=bench,
            trades=final_ledger.trades if final_ledger else [],
            avg_positions=float(np.mean(counts)) if counts else 0.0,
            avg_exposure=float(np.mean(exposures)) if exposures else 0.0,
        )

    overlay = execute(True, name)
    baseline = execute(False, f"{name}_baseline") if run_baseline else None
    return overlay, baseline


def _sector_values(ledger: Ledger, prices: DailyPrices, day: pd.Timestamp,
                   sectors: Dict[str, str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for symbol, shares in ledger.positions.items():
        price = prices.at(symbol, day)
        if price:
            sector = sectors.get(symbol, "Unknown")
            out[sector] = out.get(sector, 0.0) + shares * price
    return out


def _sector_ok(sector_nav: Dict[str, float], nav: float, sectors: Dict[str, str],
               buy_symbol: str, sell_symbol: Optional[str] = None) -> bool:
    values = dict(sector_nav)
    if sell_symbol:
        sector = sectors.get(sell_symbol, "Unknown")
        values[sector] = max(0.0, values.get(sector, 0.0) - nav * TARGET_WEIGHT)
    sector = sectors.get(buy_symbol, "Unknown")
    proposed = (values.get(sector, 0.0) + nav * TARGET_WEIGHT) / max(nav, 1e-9)
    return proposed <= SECTOR_CAP + 1e-9


def _latest_score(events: pd.DataFrame, symbol: str, day: pd.Timestamp) -> float:
    """A holding's own most recent signal value strictly before ``day``."""
    col = "s2_score" if "s2_score" in events else "signal_score"
    if col not in events:
        return 0.0
    past = events[(events.symbol == symbol) & (events.entry_date <= day)][col].dropna()
    return float(past.iloc[-1]) if len(past) else 0.0


def _pick_refill(event_set: EventSet, ledger: Ledger, day: pd.Timestamp,
                 score_fn, sectors, sector_nav, sold_flags,
                 nav: float) -> Optional[str]:
    """Best non-held, non-signalled refill candidate at a deep review."""
    if score_fn is None:
        return None
    scored = []
    for symbol in event_set.series:
        if symbol in ledger.positions or symbol in sold_flags:
            continue
        score = score_fn(symbol, day)
        if np.isfinite(score):
            scored.append((score, symbol))
    scored.sort(reverse=True)
    for _, symbol in scored:
        if _sector_ok(sector_nav, nav, sectors, symbol):
            return symbol
    return None


# ------------------------------------------------------------ avoidance sim


def avoidance_sim(
    event_set: EventSet,
    s3_events: pd.DataFrame,
    price_table_fn: Callable[[str], Optional[PriceTable]],
) -> pd.DataFrame:
    """§5.3.2 per-event avoidance benefit; columns documented in the report."""
    from src.backtest.signals import EventBuilder

    bench = EventBuilder(event_set.market, store=None).calendar()
    cost = SIDE_COST[event_set.market] * 2  # sell + rebuy
    rows = []
    for event in s3_events.itertuples():
        table = price_table_fn(event.symbol)
        if table is None or not np.isfinite(event.entry_pos) or event.entry_pos < 0:
            continue
        start_price = table.close[int(event.entry_pos)]
        clear_pos = getattr(event, "clear_entry_pos", -1)
        censored = not (isinstance(clear_pos, (int, float)) and clear_pos >= 0)
        end_pos = int(clear_pos) if not censored else len(table.close) - 1
        if end_pos <= int(event.entry_pos):
            continue
        end_price = table.close[end_pos]
        start_date = pd.Timestamp(table.dates[int(event.entry_pos)])
        end_date = pd.Timestamp(table.dates[end_pos])
        b0 = bench.price_at_or_before(start_date)
        b1 = bench.price_at_or_before(end_date)
        if b0 is None or b1 is None:
            continue
        excess = (end_price / start_price - 1.0) - (b1 / b0 - 1.0)
        benefit = -excess - cost
        rows.append({
            "symbol": event.symbol,
            "availability": event.availability,
            "entry_date": event.entry_date,
            "clear_entry_date": getattr(event, "clear_entry_date", pd.NaT),
            "censored": censored,
            "clear_quarters": getattr(event, "clear_quarters", np.nan),
            "excess_signal_to_clear": excess,
            "avoidance_benefit": benefit,
            "year": event.availability.year,
            "cried_wolf": bool(
                not censored
                and getattr(event, "clear_quarters", 99) <= 2
                and excess >= 0
            ),
        })
    return pd.DataFrame(rows)
