"""Quarterly-earnings event tables and signal construction (S1/S2/S3).

Implements docs/earnings/backtest_spec.md on top of the Phase-1 data caches
under ``data/backtest/``. The output is an :class:`EventSet` per market: one
row per (symbol, quarterly results event) with every signal input computed
point-in-time, plus the enriched per-symbol quarterly series the stateful
signals (S2 state exit, S3 state machine) walk over.

Anti-lookahead contract (spec §7.6 — each rule has a unit test):

1. ``availability_date``: US = the filing date (``filed``); India =
   ``period_end + 45d`` (Q1-Q3) / ``+60d`` (fiscal Q4, taken as the March
   quarter — the overwhelming NSE convention; non-March fiscal years are a
   disclosed approximation), rolled to the next trading day, or the scraped
   announcement date in the subsample run.
2. ``entry_date``: first trading day STRICTLY AFTER availability that has a
   price (roll <= 5 benchmark trading days, else the event is dropped and
   counted). Entry price = that day's close. No announcement-day returns.
3. YoY matches are 4 quarters back in the company's own filing sequence with
   period ends 320-410 days apart (spec §2.3); anything else is missing.
4. All trailing statistics (SUE sigma/drift, persistence, deterioration
   components) use only quarters whose own availability is strictly before
   the event's availability.
5. US rows with ``filed - period_end > 120 days`` are dropped entirely
   (pre-XBRL comparatives — coverage.md landmine); India is unaffected.
6. Cleaning (spec §2.4): 1/99 cross-sectional winsorisation of every signal
   input within each monthly cohort BEFORE ranking or trailing statistics;
   time-series standardised measures hard-clipped to ±6; growth denominators
   use ``|x_{q-4}|`` and are treated as missing when the base sits in the
   cohort's bottom 1% of absolute bases. Every exclusion is counted.

Units (coverage.md landmine): US flows are absolute USD, India flows are Rs
crore and EPS is Rs. The two never appear in one ratio: SUE uses within-market
EPS deltas (unitless after division by sigma or price), OPM is a ratio, and
cross-sectional z-scores are computed within market-specific cohorts only.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.backtest.prices import BacktestPriceStore, india_yahoo_symbol
from src.logging_setup import get_logger

logger = get_logger(__name__)

BACKTEST_DIR = Path("data/backtest")

# Spec constants.
SUE_WINDOWS = (4, 6, 8)  # §3.1 grid m
SUE_CLIP = 6.0  # §2.4 hard clip for time-series standardised measures
MIN_SIGMA = 1e-6  # §3.1 constant-EPS guard
YOY_MIN_DAYS, YOY_MAX_DAYS = 320, 410  # §2.3
MAX_ENTRY_ROLL_DAYS = 5  # §2.2 trading days
US_MAX_FILED_LAG_DAYS = 120  # coverage.md landmine: pre-XBRL comparatives
INDIA_LAG_Q123, INDIA_LAG_Q4 = 45, 60  # §1.2 SEBI LODR
INDIA_FISCAL_Q4_MONTH = 3  # March; disclosed approximation for non-March FYs
WINSOR_PCT = 0.01  # §2.4
BASE_NOISE_PCT = 0.01  # §2.4 bottom-1% absolute-base rule
ANNOUNCE_MATCH_MIN_DAYS = 10  # scraped-date ↔ quarter matching window
ANNOUNCE_MATCH_MAX_DAYS = 120

US_FINANCIAL_SECTOR = "Financials"  # GICS 40, §2.5
INDIA_FINANCIAL_SECTOR = "Financial Services"  # NSE label, §2.5

BENCHMARKS = {"us": "SPY", "india": "^CRSLDX"}

EVENT_COLUMNS = [
    "symbol", "sector", "financial", "period_end", "availability",
    "availability_basis", "scraped_late", "cohort", "entry_date",
    "entry_price", "series_idx", "frame_deduced",
    "eps", "eps_yoy", "d_eps", "rev", "rev_yoy", "d_rev", "g_rev",
    "net_income", "np_decline", "opm", "opm_yoy", "d_opm_bps",
]


@dataclass
class EventSet:
    """Everything the signal evaluations need for one market."""

    market: str
    events: pd.DataFrame  # one row per (symbol, quarterly event)
    series: Dict[str, pd.DataFrame]  # enriched quarterly series per symbol
    exclusions: Counter = field(default_factory=Counter)
    n_symbols: int = 0

    @property
    def benchmark(self) -> str:
        return BENCHMARKS[self.market]


class PriceTable:
    """Numpy view of one symbol's (or the benchmark's) daily closes."""

    def __init__(self, frame: pd.DataFrame):
        self.dates = pd.DatetimeIndex(frame.index).normalize().values
        self.close = frame["Close"].to_numpy(dtype=float)
        volume = frame["Volume"] if "Volume" in frame else pd.Series(0.0, index=frame.index)
        self.volume = volume.fillna(0.0).to_numpy(dtype=float)

    def average_daily_turnover(self, before: pd.Timestamp, days: int = 126) -> Optional[float]:
        """Mean close×volume over the trailing ``days`` sessions before a date."""
        pos = int(np.searchsorted(self.dates, np.datetime64(before.normalize()), side="left"))
        lo = max(0, pos - days)
        if pos - lo < 20:
            return None
        return float(np.mean(self.close[lo:pos] * self.volume[lo:pos]))

    def entry_after(
        self, availability: pd.Timestamp, max_roll: int, calendar: "PriceTable"
    ) -> Optional[Tuple[pd.Timestamp, float, int]]:
        """First close strictly after ``availability``.

        Returns (date, price, position) or None when no price exists within
        ``max_roll`` calendar (benchmark) trading days — a halt/suspension
        drop per §2.2.
        """
        avail = np.datetime64(availability.normalize())
        pos = int(np.searchsorted(self.dates, avail, side="right"))
        if pos >= len(self.dates):
            return None
        entry = self.dates[pos]
        bench_pos = int(np.searchsorted(calendar.dates, avail, side="right"))
        bench_entry = int(np.searchsorted(calendar.dates, entry, side="left"))
        if bench_entry - bench_pos > max_roll:
            return None
        price = float(self.close[pos])
        if not np.isfinite(price) or price <= 0:
            return None
        return pd.Timestamp(entry), price, pos

    def price_at_or_before(self, day: pd.Timestamp) -> Optional[float]:
        pos = int(np.searchsorted(self.dates, np.datetime64(day.normalize()), side="right")) - 1
        if pos < 0:
            return None
        return float(self.close[pos])

    def forward(self, pos: int, horizon: int) -> Optional[Tuple[pd.Timestamp, float]]:
        """(exit date, close-to-close return) ``horizon`` trading days after pos."""
        exit_pos = pos + horizon
        if exit_pos >= len(self.close):
            return None
        base = self.close[pos]
        if base <= 0:
            return None
        return pd.Timestamp(self.dates[exit_pos]), float(self.close[exit_pos] / base - 1.0)


class EventBuilder:
    """Builds the point-in-time event table for one market."""

    def __init__(
        self,
        market: str,
        base_dir: Path = BACKTEST_DIR,
        store: Optional[BacktestPriceStore] = None,
        india_use_scraped_dates: bool = False,
    ):
        if market not in BENCHMARKS:
            raise ValueError(f"unknown market {market!r}")
        self.market = market
        self.base_dir = Path(base_dir)
        self.store = store or BacktestPriceStore(self.base_dir / "prices")
        self.india_use_scraped_dates = india_use_scraped_dates
        self.exclusions: Counter = Counter()
        self._prices: Dict[str, PriceTable] = {}
        self._calendar: Optional[PriceTable] = None

    # ------------------------------------------------------------------ IO

    def _price_key(self, symbol: str) -> str:
        return india_yahoo_symbol(symbol) if self.market == "india" else symbol

    def price_table(self, symbol: str) -> Optional[PriceTable]:
        key = self._price_key(symbol)
        if key not in self._prices:
            frame = self.store.load(key)
            self._prices[key] = PriceTable(frame) if frame is not None and len(frame) else None
        return self._prices[key]

    def calendar(self) -> PriceTable:
        if self._calendar is None:
            frame = self.store.load(BENCHMARKS[self.market])
            if frame is None or frame.empty:
                raise RuntimeError(f"benchmark {BENCHMARKS[self.market]} missing from price cache")
            self._calendar = PriceTable(frame)
        return self._calendar

    def _universe(self) -> pd.DataFrame:
        """symbol × sector for every cached quarterly file."""
        if self.market == "us":
            uni = pd.read_csv("data/sp500.csv", comment="#")
            uni = uni.rename(columns={"symbol": "symbol", "gics_sector": "sector"})
            uni["symbol"] = uni.symbol.str.upper()
            files = {p.stem.upper() for p in (self.base_dir / "us/quarterly").glob("*.csv")}
        else:
            uni = pd.read_csv("data/nifty500.csv", comment="#")
            uni = uni.rename(columns={"Symbol": "symbol", "Industry": "sector"})
            uni["symbol"] = uni.symbol.str.upper()
            files = {p.stem.upper() for p in (self.base_dir / "india/quarterly").glob("*.csv")}
        uni = uni[uni.symbol.isin(files)][["symbol", "sector"]].drop_duplicates("symbol")
        return uni.reset_index(drop=True)

    def _announcements(self, symbol: str) -> List:
        path = self.base_dir / "india/announcements" / f"{symbol}.csv"
        if not path.exists():
            return []
        frame = pd.read_csv(path, parse_dates=["date"])
        if "results_related" in frame:
            frame = frame[frame.results_related.astype(bool)]
        return sorted(d.date() for d in frame.date.dropna())

    # ---------------------------------------------------- availability dates

    def _india_availability(
        self, period_end: pd.Timestamp, announced: List
    ) -> Tuple[pd.Timestamp, str, bool]:
        """Returns (availability, basis, scraped_late)."""
        lag = INDIA_LAG_Q4 if period_end.month == INDIA_FISCAL_Q4_MONTH else INDIA_LAG_Q123
        assumed = period_end + pd.Timedelta(days=lag)
        basis = f"assumed{lag}"
        late = False
        if self.india_use_scraped_dates:
            lo = period_end + pd.Timedelta(days=ANNOUNCE_MATCH_MIN_DAYS)
            hi = period_end + pd.Timedelta(days=ANNOUNCE_MATCH_MAX_DAYS)
            matches = [d for d in announced if lo.date() <= d <= hi.date()]
            if matches:
                actual = pd.Timestamp(matches[0])
                late = actual > assumed
                return actual, "scraped", late
        # §1.2: roll a non-trading-day availability forward to the next
        # trading day (entry then lands strictly after it, §2.2).
        cal = self.calendar()
        pos = int(np.searchsorted(cal.dates, np.datetime64(assumed.normalize()), side="left"))
        if pos < len(cal.dates):
            assumed = pd.Timestamp(cal.dates[pos])
        return assumed, basis, late

    # ------------------------------------------------------- per-symbol load

    def _load_symbol(self, symbol: str, sector: str) -> Optional[pd.DataFrame]:
        if self.market == "us":
            path = self.base_dir / "us/quarterly" / f"{symbol}.csv"
            frame = pd.read_csv(path, parse_dates=["period_end", "filed"])
            frame = frame.dropna(subset=["filed"])
            lag = (frame.filed - frame.period_end).dt.days
            bad = lag > US_MAX_FILED_LAG_DAYS
            self.exclusions["us_pre_xbrl_lag_rows"] += int(bad.sum())
            frame = frame[~bad]
            frame["availability"] = frame.filed
            frame["availability_basis"] = "filed"
            frame["scraped_late"] = False
            frame["opm"] = frame.operating_income / frame.revenue
            frame = frame.rename(columns={"revenue": "rev", "eps_diluted": "eps"})
            if "frame_deduced" not in frame:
                frame["frame_deduced"] = False
        else:
            path = self.base_dir / "india/quarterly" / f"{symbol}.csv"
            frame = pd.read_csv(path, parse_dates=["period_end"])
            announced = self._announcements(symbol)
            avail = [self._india_availability(pe, announced) for pe in frame.period_end]
            frame["availability"] = [a[0] for a in avail]
            frame["availability_basis"] = [a[1] for a in avail]
            frame["scraped_late"] = [a[2] for a in avail]
            frame["opm"] = frame.opm_pct / 100.0
            frame = frame.rename(
                columns={"sales_cr": "rev", "net_profit_cr": "net_income", "eps_rs": "eps"}
            )
            frame["frame_deduced"] = False
        frame = frame.sort_values("period_end").drop_duplicates("period_end", keep="first")
        frame = frame[["period_end", "availability", "availability_basis", "scraped_late",
                       "rev", "net_income", "eps", "opm", "frame_deduced"]].reset_index(drop=True)
        frame["symbol"] = symbol
        frame["sector"] = sector
        frame["financial"] = sector in (US_FINANCIAL_SECTOR, INDIA_FINANCIAL_SECTOR)
        return frame

    # -------------------------------------------------------- derivations

    @staticmethod
    def _yoy_matches(period_ends: pd.Series, back: int = 4) -> List[Optional[int]]:
        """Index of the YoY partner ``back`` positions earlier, per §2.3."""
        out: List[Optional[int]] = []
        values = period_ends.values
        for i in range(len(values)):
            j = i - back
            if j < 0:
                out.append(None)
                continue
            days = (values[i] - values[j]).astype("timedelta64[D]").astype(int)
            lo, hi = (YOY_MIN_DAYS, YOY_MAX_DAYS) if back == 4 else (2 * YOY_MIN_DAYS - 20, 2 * YOY_MAX_DAYS + 10)
            out.append(j if lo <= days <= hi else None)
        return out

    def _derive(self, frame: pd.DataFrame) -> pd.DataFrame:
        """YoY deltas and per-quarter signal inputs for one symbol."""
        yoy = self._yoy_matches(frame.period_end, back=4)
        idx = np.arange(len(frame))

        def matched(col: str) -> np.ndarray:
            values = frame[col].to_numpy(dtype=float)
            out = np.full(len(frame), np.nan)
            for i, j in enumerate(yoy):
                if j is not None:
                    out[i] = values[j]
            return out

        frame["eps_yoy"] = matched("eps")
        frame["rev_yoy"] = matched("rev")
        frame["np_yoy"] = matched("net_income")
        frame["opm_yoy"] = matched("opm")
        frame["d_eps"] = frame.eps - frame.eps_yoy
        frame["d_rev"] = frame.rev - frame.rev_yoy
        frame["g_rev"] = (frame.rev - frame.rev_yoy) / frame.rev_yoy.abs()
        frame["np_decline"] = np.where(
            frame.net_income.notna() & frame.np_yoy.notna(),
            frame.net_income < frame.np_yoy,
            np.nan,
        )
        frame["d_opm_bps"] = (frame.opm - frame.opm_yoy) * 1e4

        # Two-year OPM match (q vs q-8) for the cumulative C2 variant. Spec is
        # silent on the date window here; sequence position plus a sanity band.
        back8 = self._yoy_matches(frame.period_end, back=8)
        opm_vals = frame.opm.to_numpy(dtype=float)
        frame["opm_q8"] = [opm_vals[j] if j is not None else np.nan for j in back8]
        frame["d_opm_8_bps"] = (frame.opm - frame.opm_q8) * 1e4
        frame["series_idx"] = idx
        return frame

    # ------------------------------------------------------ cohort cleaning

    def _clean_cohorts(self, events: pd.DataFrame) -> pd.DataFrame:
        """§2.4 winsorisation and the bottom-1% base rule, within cohorts."""
        events = events.copy()
        events["cohort"] = events.availability.dt.strftime("%Y-%m")

        def winsorise(series: pd.Series) -> pd.Series:
            series = series.astype(float)
            valid = series.dropna()
            if len(valid) < 5:
                return series
            lo, hi = valid.quantile(WINSOR_PCT), valid.quantile(1 - WINSOR_PCT)
            return series.clip(lo, hi)

        for col in ("d_eps", "d_rev", "g_rev", "d_opm_bps", "d_opm_8_bps"):
            events[col] = events.groupby("cohort")[col].transform(winsorise)

        # Bottom-1% absolute-base rule: growth off a near-zero base is noise.
        def base_rule(base: pd.Series) -> pd.Series:
            base = base.abs()
            valid = base.dropna()
            if len(valid) < 5:
                return pd.Series(True, index=base.index)
            floor = valid.quantile(BASE_NOISE_PCT)
            return base > floor

        keep = events.groupby("cohort")["rev_yoy"].transform(base_rule)
        dropped = int((~keep & events.g_rev.notna()).sum())
        self.exclusions["growth_base_noise"] += dropped
        events.loc[~keep, "g_rev"] = np.nan
        return events

    # ------------------------------------------------------- SUE measures

    def _sue_timeseries(self, series: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        """Trailing-m SUE variants per quarter, availability-guarded (§7.6.1).

        Computed on the full per-symbol SERIES (not the event rows) so halts
        and other event-level drops cannot shift the trailing windows. The m
        trailing deltas are the m most recent non-NaN YoY deltas whose own
        availability is strictly before the current quarter's (spec: no
        partial windows).
        """
        rows: List[dict] = []
        for symbol, frame in series.items():
            avails = frame.availability.values
            for stem, src in (("sue_ts", "d_eps"), ("rev_sue_ts", "d_rev")):
                if src not in frame:
                    continue
                deltas = frame[src].to_numpy(dtype=float)
                for m in SUE_WINDOWS:
                    plain = np.full(len(frame), np.nan)
                    drift = np.full(len(frame), np.nan)
                    for i in range(len(frame)):
                        hist = [
                            deltas[k]
                            for k in range(i)
                            if avails[k] < avails[i] and np.isfinite(deltas[k])
                        ][-m:]
                        if len(hist) < m or not np.isfinite(deltas[i]):
                            continue
                        sigma = float(np.std(hist, ddof=1))
                        if sigma < MIN_SIGMA:
                            continue
                        plain[i] = np.clip(deltas[i] / sigma, -SUE_CLIP, SUE_CLIP)
                        drift[i] = np.clip(
                            (deltas[i] - float(np.mean(hist))) / sigma,
                            -SUE_CLIP, SUE_CLIP,
                        )
                    for i in range(len(frame)):
                        if np.isfinite(plain[i]) or np.isfinite(drift[i]):
                            rows.append({
                                "symbol": symbol,
                                "period_end": frame.period_end.iloc[i],
                                f"{stem}_m{m}": plain[i],
                                f"{stem}_drift_m{m}": drift[i],
                            })
        if not rows:
            return pd.DataFrame(columns=["symbol", "period_end"])
        return (
            pd.DataFrame(rows)
            .groupby(["symbol", "period_end"], as_index=False)
            .first()
        )

    def _sue_cross_sectional(self, events: pd.DataFrame) -> pd.DataFrame:
        """Cohort z-scores of price-scaled surprises (§3.1, CS)."""
        events = events.copy()
        # d_eps/d_rev are already cohort-winsorised in place by _clean_cohorts.
        for stem, src in (("sue_cs", "d_eps"), ("rev_sue_cs", "d_rev")):
            scaled = events[src] / events.entry_price
            z = scaled.groupby(events.cohort).transform(
                lambda s: (s - s.mean()) / s.std(ddof=0) if s.notna().sum() >= 2 else np.nan
            )
            events[stem] = z.clip(-SUE_CLIP, SUE_CLIP)
        return events

    # ------------------------------------------------------------- build

    def build(self) -> EventSet:
        universe = self._universe()
        frames: List[pd.DataFrame] = []
        for row in universe.itertuples():
            frame = self._load_symbol(row.symbol, row.sector)
            if frame is None or frame.empty:
                self.exclusions["symbols_no_quarterly"] += 1
                continue
            frames.append(self._derive(frame))

        series = {f.symbol.iloc[0]: f.reset_index(drop=True) for f in frames}
        events = pd.concat(frames, ignore_index=True)

        # Entry dates and prices (§2.2).
        entries: List[Optional[Tuple[pd.Timestamp, float, int]]] = []
        calendar = self.calendar()
        for row in events.itertuples():
            table = self.price_table(row.symbol)
            if table is None:
                entries.append(None)
                self.exclusions["no_price_series"] += 1
                continue
            entry = table.entry_after(row.availability, MAX_ENTRY_ROLL_DAYS, calendar)
            if entry is None:
                self.exclusions["entry_halt_or_no_price"] += 1
            entries.append(entry)
        events["entry_date"] = [e[0] if e else pd.NaT for e in entries]
        events["entry_price"] = [e[1] if e else np.nan for e in entries]
        events["entry_pos"] = [e[2] if e else -1 for e in entries]
        events = events.dropna(subset=["entry_date"]).reset_index(drop=True)

        events = self._clean_cohorts(events)

        # Carry entry + winsorised columns back into the per-symbol series so
        # state machines, trailing SUE windows and S2 state exits all walk the
        # same point-in-time, cleaned view of each company's quarters.
        carry = events[[
            "symbol", "period_end", "entry_date", "entry_price", "entry_pos",
            "d_eps", "d_rev", "g_rev", "d_opm_bps", "d_opm_8_bps",
        ]]
        for symbol, frame in series.items():
            merged = frame.merge(
                carry[carry.symbol == symbol].drop(columns="symbol"),
                on="period_end", how="left", suffixes=("", "_e"),
            )
            for col in ("d_eps", "d_rev", "g_rev", "d_opm_bps", "d_opm_8_bps"):
                if f"{col}_e" in merged:
                    merged[col] = merged[f"{col}_e"].combine_first(merged[col])
                    merged = merged.drop(columns=f"{col}_e")
            series[symbol] = merged

        sue = self._sue_timeseries(series)
        events = events.merge(sue, on=["symbol", "period_end"], how="left")
        events = self._sue_cross_sectional(events)

        result = EventSet(
            market=self.market,
            events=events,
            series=series,
            exclusions=self.exclusions,
            n_symbols=len(series),
        )
        logger.info(
            "%s events built: %d events, %d symbols, exclusions=%s",
            self.market, len(events), len(series), dict(self.exclusions),
        )
        return result


# ---------------------------------------------------------------- S3 machine


@dataclass
class S3Params:
    consecutive_declines: int = 2  # c ∈ {2, 3}
    margin_compression_bps: float = 300.0  # θ_m ∈ {200, 300, 500}
    cumulative_c2: bool = False  # C2 variant: OPM_q − OPM_{q−8}


S3_STATES = ("NORMAL", "WATCH", "CONFIRMED")
CLEAR_CLEAN_QUARTERS = 2  # §5.2 CLEAR


def s3_components(
    frame: pd.DataFrame, params: S3Params
) -> pd.DataFrame:
    """Per-quarter C1/C2/C3 component flags for one symbol's series."""
    n = len(frame)
    np_decl = frame.np_decline.to_numpy(dtype=float)  # 1.0/0.0/NaN
    g_rev = frame.g_rev.to_numpy(dtype=float)
    d_opm = (frame.d_opm_8_bps if params.cumulative_c2 else frame.d_opm_bps).to_numpy(dtype=float)
    financial = bool(frame.financial.iloc[0])

    c1 = np.zeros(n, dtype=bool)
    c1_strict3 = np.zeros(n, dtype=bool)  # C1 at c=3 always tracked: it alone confirms (§5.2)
    c2 = np.zeros(n, dtype=bool)
    c3 = np.zeros(n, dtype=bool)
    for i in range(n):
        declines = np.zeros(3, dtype=bool)
        for c in (2, 3):
            window = np_decl[max(0, i - c + 1): i + 1]
            declines[c - 2] = len(window) == c and np.all(window == 1.0)
        c1[i] = declines[params.consecutive_declines - 2]
        c1_strict3[i] = declines[1]
        if not financial and np.isfinite(d_opm[i]):
            c2[i] = d_opm[i] <= -params.margin_compression_bps
        if i >= 2 and np.all(np.isfinite(g_rev[i - 2: i + 1])):
            steps_down = g_rev[i] < g_rev[i - 1] < g_rev[i - 2]
            c3[i] = steps_down or g_rev[i] < 0
        elif np.isfinite(g_rev[i]):
            c3[i] = g_rev[i] < 0
    return pd.DataFrame(
        {"c1": c1, "c1_strict3": c1_strict3, "c2": c2, "c3": c3},
        index=frame.index,
    )


def s3_state_machine(
    frame: pd.DataFrame, params: S3Params
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Walk one symbol's series; returns (state history, confirmed events).

    The tradeable event is the TRANSITION into CONFIRMED; the state then
    persists until CLEAR (two consecutive zero-component quarters) — no new
    event fires while already confirmed (spec §5.2, simpler reading noted).
    """
    comp = s3_components(frame, params)
    firing = comp[["c1", "c2", "c3"]].sum(axis=1)
    confirmed_cond = (firing >= 2) | comp.c1_strict3

    state = "NORMAL"
    clean = 0
    states: List[str] = []
    events: List[dict] = []
    current: Optional[dict] = None
    for i in range(len(frame)):
        n_fire = int(firing.iloc[i])
        if confirmed_cond.iloc[i]:
            if state != "CONFIRMED":
                current = {
                    "symbol": frame.symbol.iloc[i],
                    "period_end": frame.period_end.iloc[i],
                    "availability": frame.availability.iloc[i],
                    "entry_date": frame.entry_date.iloc[i],
                    "entry_price": frame.entry_price.iloc[i],
                    "entry_pos": frame.entry_pos.iloc[i],
                    "series_idx": i,
                    "c1": bool(comp.c1.iloc[i]),
                    "c2": bool(comp.c2.iloc[i]),
                    "c3": bool(comp.c3.iloc[i]),
                    "components_fired": n_fire,
                }
                events.append(current)
            state = "CONFIRMED"
            clean = 0
        elif n_fire == 1:
            if state != "CONFIRMED":
                state = "WATCH"
            clean = 0
        else:
            clean += 1
            if state == "CONFIRMED" and clean >= CLEAR_CLEAN_QUARTERS and current is not None:
                current["clear_period_end"] = frame.period_end.iloc[i]
                current["clear_availability"] = frame.availability.iloc[i]
                current["clear_entry_date"] = frame.entry_date.iloc[i]
                current["clear_entry_price"] = frame.entry_price.iloc[i]
                current["clear_entry_pos"] = frame.entry_pos.iloc[i]
                current["clear_quarters"] = i - int(current["series_idx"])
                current = None
                state = "NORMAL"
            elif state != "CONFIRMED":
                state = "NORMAL"
        states.append(state)

    history = frame[["symbol", "period_end", "availability"]].copy()
    history["state"] = states
    history["components_fired"] = firing
    history["c1"] = comp.c1
    history["c2"] = comp.c2
    history["c3"] = comp.c3
    return history, pd.DataFrame(events)


def s3_run(event_set: EventSet, params: S3Params) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """State machine across all symbols: returns (confirmed events, states)."""
    histories: List[pd.DataFrame] = []
    confirmed: List[pd.DataFrame] = []
    for symbol, frame in sorted(event_set.series.items()):
        history, events = s3_state_machine(frame, params)
        histories.append(history)
        if not events.empty:
            confirmed.append(events)
    states = pd.concat(histories, ignore_index=True)
    events = pd.concat(confirmed, ignore_index=True) if confirmed else pd.DataFrame()
    if not events.empty:
        events["cohort"] = events.availability.dt.strftime("%Y-%m")
    return events, states


# ------------------------------------------------------- S2 state evaluation


@dataclass
class S2Params:
    theta_rev: float = 0.05  # θ_rev ∈ {0, 0.05, 0.10}
    theta_opm_bps: float = 100.0  # θ_opm ∈ {0, 100, 200}
    persistence: int = 2  # k ∈ {1, 2, 3}
    variant: str = "V1"  # V1 AND-rule | V2 composite z (top quintile)


def s2_prepare(event_set: EventSet) -> pd.DataFrame:
    """Precomputes S2 columns once so grid cells evaluate vectorised.

    Adds: persist_k{1,2,3} (g_rev > 0 in each of the last k quarters),
    s2_z_rev / s2_z_opm (cohort z-scores), s2_score_v2 (composite; degrades
    to the available z with a flag when one leg is missing), s2_v2_quintile.
    Financials are NOT removed here — V1/V2 filters do that per call.
    """
    events = event_set.events.copy()
    for k in (1, 2, 3):
        events[f"persist_k{k}"] = False
    for symbol, group in events.groupby("symbol", sort=False):
        series_frame = event_set.series[symbol]
        idx = group.series_idx.to_numpy()
        flags = {k: np.zeros(len(group), dtype=bool) for k in (1, 2, 3)}
        for pos in range(len(group)):
            i = int(idx[pos])
            for k in (1, 2, 3):
                if i - k + 1 < 0:
                    continue
                window = series_frame.g_rev.iloc[i - k + 1: i + 1]
                flags[k][pos] = len(window) == k and bool((window > 0).all())
        for k in (1, 2, 3):
            events.loc[group.index, f"persist_k{k}"] = flags[k]

    def zscore(series: pd.Series) -> pd.Series:
        return series.groupby(events.cohort).transform(
            lambda s: (s - s.mean()) / s.std(ddof=0) if s.notna().sum() >= 2 else np.nan
        )

    events["s2_z_rev"] = zscore(events.g_rev)
    events["s2_z_opm"] = zscore(events.d_opm_bps)
    both = events.s2_z_rev.notna() & events.s2_z_opm.notna()
    score = pd.Series(np.nan, index=events.index)
    score[both] = events.s2_z_rev[both] + events.s2_z_opm[both]
    degraded = ~both & (events.s2_z_rev.notna() | events.s2_z_opm.notna())
    score[degraded] = (
        events.s2_z_rev.fillna(0) + events.s2_z_opm.fillna(0)
    )[degraded]
    events["s2_score_v2"] = score
    events["s2_v2_degraded"] = degraded
    ranked = events.dropna(subset=["s2_score_v2"])
    quintile = ranked.groupby("cohort").s2_score_v2.transform(
        lambda s: s.rank(method="first", ascending=False)
        <= max(1, int(np.ceil(len(s) / 5)))
    )
    events["s2_v2_quintile"] = False
    events.loc[ranked.index[quintile], "s2_v2_quintile"] = True
    return events


def s2_signals(prepared: pd.DataFrame, params: S2Params) -> pd.DataFrame:
    """Vectorised S2 firing filter over the s2_prepare output (§4)."""
    frame = prepared[~prepared.financial]
    frame = frame[frame[f"persist_k{params.persistence}"]]
    if params.variant == "V1":
        fires = (frame.g_rev >= params.theta_rev) & (
            frame.d_opm_bps >= params.theta_opm_bps
        )
        out = frame[fires].copy()
        out["s2_score"] = out.g_rev
    else:
        out = frame[frame.s2_v2_quintile].copy()
        out["s2_score"] = out.s2_score_v2
    return out


def s2_state_exit(
    event_set: EventSet, params: S2Params, entry: pd.Series, cap: int = 252
) -> Tuple[pd.Timestamp, int]:
    """First later announcement where the combination no longer holds (§4.3).

    Returns (exit entry_date, holding length in trading days capped at
    ``cap``). The V1 combination is checked on later quarters; V2 re-ranks
    within each later quarter's cohort — approximated by the persistence and
    score components recomputed per quarter (documented simplification).
    """
    symbol = entry.symbol
    series = event_set.series[symbol]
    start = int(entry.series_idx)
    row = series.iloc[start]
    entry_pos = int(row.entry_pos) if np.isfinite(row.entry_pos) else -1
    if entry_pos < 0:
        return pd.NaT, cap

    def holds(i: int) -> bool:
        window = series.g_rev.iloc[max(0, i - params.persistence + 1): i + 1]
        if len(window) < params.persistence or not bool((window > 0).all()):
            return False
        g = series.g_rev.iloc[i]
        d = series.d_opm_bps.iloc[i]
        if params.variant == "V1":
            return bool(g >= params.theta_rev and d >= params.theta_opm_bps)
        # V2: degrade to the AND of available positive z legs — simpler choice
        # (a quintile re-rank per later cohort is not meaningful ex-post for a
        # single name; noted in the report).
        return bool((np.isfinite(g) and g > 0) or (np.isfinite(d) and d > 0))

    table_days = cap
    exit_date = pd.NaT
    for i in range(start + 1, len(series)):
        later = series.iloc[i]
        if not np.isfinite(later.entry_pos):
            continue
        held_days = int(later.entry_pos) - entry_pos
        if held_days > cap:
            break
        if not holds(i):
            exit_date = later.entry_date
            table_days = held_days
            break
    return exit_date, table_days
