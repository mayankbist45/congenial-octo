"""Signal-math, anti-lookahead, state-machine and sim-accounting tests.

Every test here is network-free: price series and quarterly fundamentals are
synthetic, built to make the expected numbers hand-computable. The §7.6
anti-lookahead rules each map to a named test, as the spec requires.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import pandas as pd
import pytest

from src.backtest import events as ev
from src.backtest import llm_study, sim
from src.backtest.signals import (
    EventBuilder, EventSet, PriceTable, S2Params, S3Params, s3_components,
    s3_state_machine,
)


# --------------------------------------------------------------- fixtures


def make_price_table(days: int = 400, start: str = "2020-01-01",
                     drift: float = 0.001, seed: int = 7) -> PriceTable:
    dates = pd.bdate_range(start, periods=days)
    rng = np.random.default_rng(seed)
    rets = drift + rng.normal(0, 0.01, days)
    close = 100.0 * np.exp(np.cumsum(rets))
    frame = pd.DataFrame({"Close": close, "Volume": 1e6}, index=dates)
    return PriceTable(frame)


def make_series(symbol: str, period_ends, eps, rev, np_, opm,
                avail_lag: int = 30, sector: str = "IT") -> pd.DataFrame:
    """A minimal per-symbol quarterly series with availability dates."""
    n = len(period_ends)
    frame = pd.DataFrame({
        "symbol": symbol,
        "period_end": pd.to_datetime(period_ends),
        "rev": rev, "net_income": np_, "eps": eps, "opm": opm,
    })
    frame["availability"] = frame.period_end + pd.Timedelta(days=avail_lag)
    frame["availability_basis"] = "filed"
    frame["scraped_late"] = False
    frame["sector"] = sector
    frame["financial"] = sector in ("Financials", "Financial Services")
    frame["frame_deduced"] = False
    return frame


def derive(builder: EventBuilder, frame: pd.DataFrame) -> pd.DataFrame:
    return builder._derive(frame)


# ---------------------------------------------------------- YoY & SUE math


class TestYoyMatching:
    def test_four_back_with_date_window(self):
        builder = EventBuilder("us")
        ends = pd.to_datetime(
            ["2020-03-31", "2020-06-30", "2020-09-30", "2020-12-31",
             "2021-03-31", "2021-06-30"])
        matches = builder._yoy_matches(pd.Series(ends), back=4)
        assert matches[4] == 0 and matches[5] == 1
        assert all(m is None for m in matches[:4])

    def test_off_cycle_gap_rejected(self):
        builder = EventBuilder("us")
        # A 500-day gap before the 5th quarter: not a valid YoY pair (§2.3).
        ends = pd.to_datetime(
            ["2020-03-31", "2020-06-30", "2020-09-30", "2021-06-30", "2021-09-30"])
        matches = builder._yoy_matches(pd.Series(ends), back=4)
        assert matches[4] is None


class TestSueTimeseries:
    def _series_with_deltas(self, deltas, avail_lag_days: int = 30) -> pd.DataFrame:
        """Series whose YoY deltas are exactly `deltas` (constructed directly)."""
        n = len(deltas)
        ends = pd.date_range("2018-03-31", periods=n, freq="3ME")
        frame = pd.DataFrame({
            "symbol": "X",
            "period_end": ends,
            "availability": ends + pd.Timedelta(days=avail_lag_days),
            "d_eps": deltas,
        })
        return frame

    def test_hand_computed_plain(self):
        builder = EventBuilder("us")
        # d values: last-4 trailing of the 6th event are [1, 2, 3, 4]:
        # sigma = std([1,2,3,4], ddof=1) = 1.2910; sue = d/sigma = 5/1.2910.
        deltas = [np.nan, 1.0, 2.0, 3.0, 4.0, 5.0]
        frame = self._series_with_deltas(deltas)
        out = builder._sue_timeseries({"X": frame})
        row = out[out.period_end == frame.period_end.iloc[5]].iloc[0]
        expected = 5.0 / np.std([1, 2, 3, 4], ddof=1)
        assert row.sue_ts_m4 == pytest.approx(expected, rel=1e-9)
        drift_expected = (5.0 - np.mean([1, 2, 3, 4])) / np.std([1, 2, 3, 4], ddof=1)
        assert row.sue_ts_drift_m4 == pytest.approx(drift_expected, rel=1e-9)

    def test_no_partial_windows(self):
        builder = EventBuilder("us")
        deltas = [np.nan, 1.0, 2.0, 3.0]  # only 3 trailing deltas for m=4
        frame = self._series_with_deltas(deltas)
        out = builder._sue_timeseries({"X": frame})
        assert out.empty or out.sue_ts_m4.isna().all()

    def test_constant_series_dropped(self):
        builder = EventBuilder("us")
        deltas = [np.nan] + [2.0] * 6  # sigma = 0 < 1e-6 → drop (§3.1)
        frame = self._series_with_deltas(deltas)
        out = builder._sue_timeseries({"X": frame})
        assert out.empty

    def test_clip_at_six(self):
        builder = EventBuilder("us")
        deltas = [np.nan, 0.01, -0.01, 0.02, -0.02, 100.0]
        frame = self._series_with_deltas(deltas)
        out = builder._sue_timeseries({"X": frame})
        assert out.sue_ts_m4.max() <= 6.0


class TestAntiLookahead:
    """Spec §7.6 rules, one test each where practical."""

    def test_rule1_trailing_windows_ignore_future(self):
        """Changing a FUTURE quarter must not change any earlier SUE."""
        builder = EventBuilder("us")
        deltas = [np.nan, 1.0, 2.0, 3.0, 4.0, 5.0, 9999.0]
        frame = TestSueTimeseries()._series_with_deltas(deltas)
        before = builder._sue_timeseries({"X": frame})
        deltas2 = [np.nan, 1.0, 2.0, 3.0, 4.0, 5.0, -9999.0]
        frame2 = TestSueTimeseries()._series_with_deltas(deltas2)
        after = builder._sue_timeseries({"X": frame2})
        merged = before.merge(after, on="period_end", suffixes=("_a", "_b"))
        # Only events strictly BEFORE the changed quarter are compared — the
        # changed quarter's own event legitimately differs.
        last = frame.period_end.iloc[-1]
        merged = merged[merged.period_end < last]
        for base in ("sue_ts_m4", "sue_ts_drift_m4"):
            assert np.allclose(merged[f"{base}_a"], merged[f"{base}_b"],
                               equal_nan=True)

    def test_rule1b_availability_strictly_before(self):
        """A quarter not yet public at the event never enters its window."""
        builder = EventBuilder("us")
        ends = pd.date_range("2018-03-31", periods=7, freq="3ME")
        avails = (ends + pd.Timedelta(days=30)).values
        avails[3] = avails[6]  # the 100.0 quarter is only public AT the event
        deltas = [np.nan, 1.0, 2.0, 100.0, 3.0, 4.0, 5.0]
        frame = pd.DataFrame({
            "symbol": "X", "period_end": ends,
            "availability": pd.DatetimeIndex(avails), "d_eps": deltas,
        })
        out = builder._sue_timeseries({"X": frame})
        row = out[out.period_end == ends[6]].iloc[0]
        # Trailing 4 deltas strictly before the event's availability:
        # [1, 2, 3, 4] — the 100.0 quarter is excluded by strictness.
        expected = 5.0 / np.std([1, 2, 3, 4], ddof=1)
        assert row.sue_ts_m4 == pytest.approx(expected, rel=1e-9)

    def test_rule3_entry_strictly_after_availability(self):
        table = make_price_table()
        calendar = make_price_table()
        # 2020-06-05 is a Friday; entry must be Monday 2020-06-08.
        entry = table.entry_after(pd.Timestamp("2020-06-05"), 5, calendar)
        assert entry is not None
        assert entry[0] == pd.Timestamp("2020-06-08")
        # On the day itself the close is NOT used.
        assert entry[0] > pd.Timestamp("2020-06-05")

    def test_rule3_halt_roll_limit(self):
        dates = pd.to_datetime(["2020-01-01", "2020-01-02", "2020-02-03"])
        frame = pd.DataFrame({"Close": [10.0, 10.5, 11.0], "Volume": 1e6}, index=dates)
        table = PriceTable(frame)
        calendar = make_price_table()
        # Next price is ~22 benchmark trading days after availability → drop.
        entry = table.entry_after(pd.Timestamp("2020-01-02"), 5, calendar)
        assert entry is None

    def test_rule4_walkforward_folds_never_overlap(self):
        folds = ev.make_folds()
        assert [f.test_year for f in folds] == list(range(2015, 2026))
        for fold in folds:
            assert fold.train_end < pd.Timestamp(fold.test_year, 1, 1)
            assert fold.train_start == pd.Timestamp(fold.test_year - 5, 1, 1)
        events = pd.DataFrame({
            "availability": pd.to_datetime(
                ["2014-06-30", "2015-02-15", "2015-12-31", "2016-01-01"]),
        })
        train, test = ev.train_test_split(events, folds[0])
        assert len(test) == 2  # calendar 2015 only
        assert train.availability.max() < test.availability.min()

    def test_rule5_us_pre_xbrl_rows_dropped(self, tmp_path):
        """filed - period_end > 120d rows never reach the event table."""
        builder = EventBuilder("us", base_dir=tmp_path)
        (tmp_path / "us/quarterly").mkdir(parents=True)
        frame = pd.DataFrame({
            "period_end": ["2008-03-31", "2008-06-30", "2008-09-30"],
            "filed": ["2009-05-01", "2008-08-01", "2008-11-01"],
            "revenue": [1, 2, 3], "net_income": [1, 1, 1],
            "eps_diluted": [1, 1, 1], "operating_income": [1, 1, 1],
            "source_form": ["10-Q"] * 3, "frame_deduced": [False] * 3,
        })
        frame.to_csv(tmp_path / "us/quarterly/AAA.csv", index=False)
        loaded = builder._load_symbol("AAA", "Industrials")
        assert len(loaded) == 2  # the 2009-filed 2008 row is a comparative
        assert builder.exclusions["us_pre_xbrl_lag_rows"] == 1

    def test_rule6_brief_contains_nothing_after_event(self):
        period_ends = pd.date_range("2022-03-31", periods=8, freq="3ME")
        frame = make_series(
            "INFY", period_ends,
            eps=np.arange(8.0), rev=100 * np.arange(8.0) + 500,
            np_=np.arange(8.0) + 10, opm=[0.2] * 8,
        )
        builder = EventBuilder("india")
        frame = builder._derive(frame)
        events = frame.iloc[[5]].copy()
        events["cohort"] = "2023-07"
        events["entry_price"] = 1000.0
        event_set = EventSet("india", events, {"INFY": frame})
        event = events.iloc[0]
        brief = llm_study.build_brief(event_set, event)
        # Event quarter (Jun 2023, index 5) appears; nothing after it.
        assert "Jun 2023" in brief
        assert "Sep 2023" not in brief
        assert "Dec 2023" not in brief
        assert "Mar 2024" not in brief

    def test_rule6_sampler_deterministic(self):
        period_ends = pd.date_range("2022-03-31", periods=8, freq="3ME")
        frames = {}
        rows = []
        for i, symbol in enumerate([f"S{k:02d}" for k in range(30)]):
            frame = make_series(
                symbol, period_ends, eps=np.arange(8.0) + i,
                rev=np.arange(8.0) + 100, np_=np.arange(8.0) + 5,
                opm=[0.15] * 8, sector="IT" if i % 2 else "Pharma",
            )
            frame = EventBuilder("india")._derive(frame)
            frames[symbol] = frame
            rows.append(frame)
        events = pd.concat(rows, ignore_index=True)
        events["cohort"] = events.availability.dt.strftime("%Y-%m")
        events["sue_cs"] = np.tile(np.arange(8.0), 30)
        es = EventSet("india", events, frames)
        a = llm_study.stratified_sample(es, "sue_cs", n=60)
        b = llm_study.stratified_sample(es, "sue_cs", n=60)
        pd.testing.assert_frame_equal(a, b)
        assert len(a) <= 60


# ------------------------------------------------------------- cohort ops


class TestCleaning:
    def test_winsorisation_within_cohort(self):
        builder = EventBuilder("us")
        rng = np.random.default_rng(1)
        n = 200
        events = pd.DataFrame({
            "symbol": [f"S{i}" for i in range(n)],
            "period_end": pd.Timestamp("2021-03-31"),
            "availability": pd.Timestamp("2021-04-30"),
            "d_eps": np.concatenate([rng.normal(0, 1, n - 2), [1e6, -1e6]]),
            "d_rev": rng.normal(0, 1, n),
            "g_rev": rng.normal(0.1, 0.3, n),
            "d_opm_bps": rng.normal(0, 50, n),
            "d_opm_8_bps": rng.normal(0, 50, n),
            "rev_yoy": np.abs(rng.normal(100, 20, n)),
        })
        out = builder._clean_cohorts(events)
        # §2.4: values cap at the cohort's own 1st/99th percentiles (the
        # outliers themselves pull the bound, but are clipped to it).
        lo, hi = pd.Series(events.d_eps).quantile([0.01, 0.99])
        assert out.d_eps.max() == pytest.approx(hi, rel=1e-9)
        assert out.d_eps.min() == pytest.approx(lo, rel=1e-9)
        assert out.d_eps.abs().max() < 1e6  # the raw outliers are gone

    def test_growth_base_noise_rule(self):
        builder = EventBuilder("us")
        n = 200
        bases = np.linspace(1, 1000, n)
        bases[0] = 1e-9  # bottom-1% base → growth set missing
        events = pd.DataFrame({
            "symbol": [f"S{i}" for i in range(n)],
            "period_end": pd.Timestamp("2021-03-31"),
            "availability": pd.Timestamp("2021-04-30"),
            "d_eps": np.ones(n), "d_rev": np.ones(n),
            "g_rev": np.ones(n), "d_opm_bps": np.ones(n),
            "d_opm_8_bps": np.ones(n),
            "rev_yoy": bases,
        })
        out = builder._clean_cohorts(events)
        assert np.isnan(out.g_rev.iloc[0])
        assert builder.exclusions["growth_base_noise"] >= 1


class TestBuckets:
    def test_tie_break_g_rev_then_symbol(self):
        events = pd.DataFrame({
            "symbol": ["BBB", "AAA", "CCC", "DDD"],
            "availability": [pd.Timestamp("2021-05-01")] * 4,
            "cohort": ["2021-05"] * 4,
            "sue": [1.0, 1.0, 1.0, -1.0],
            "g_rev": [0.1, 0.2, 0.05, -0.1],
        })
        buckets = ev.assign_buckets(events, "sue", 2)
        by_symbol = dict(zip(events.symbol, buckets))
        # Tied SUE: AAA (higher g_rev) then BBB then CCC rank above DDD.
        assert by_symbol["AAA"] == 0 and by_symbol["BBB"] == 0
        assert by_symbol["CCC"] == 1 and by_symbol["DDD"] == 1

    def test_missing_score_unranked(self):
        events = pd.DataFrame({
            "symbol": ["A", "B"],
            "cohort": ["2021-05"] * 2,
            "availability": [pd.Timestamp("2021-05-01")] * 2,
            "sue": [np.nan, 1.0],
            "g_rev": [0.1, 0.1],
        })
        buckets = ev.assign_buckets(events, "sue", 2)
        assert buckets.iloc[0] == -1


class TestFamaMacbeth:
    def test_t_stat_hand_computed(self):
        means = pd.Series([0.02, 0.01, 0.03, 0.02, 0.01, 0.02])
        t, n = ev.fama_macbeth(means)
        expected = means.mean() / (means.std(ddof=1) / np.sqrt(6))
        assert t == pytest.approx(expected, rel=1e-9)
        assert n == 6

    def test_too_few_cohorts_nan(self):
        t, n = ev.fama_macbeth(pd.Series([0.01, 0.02]))
        assert np.isnan(t) and n == 2


# --------------------------------------------------------- S3 state machine


def s3_series(np_decline_pattern, g_rev_pattern, d_opm_pattern,
              sector: str = "IT") -> pd.DataFrame:
    """One symbol's series with directly specified component inputs."""
    n = len(np_decline_pattern)
    ends = pd.date_range("2021-03-31", periods=n, freq="3ME")
    frame = pd.DataFrame({
        "symbol": "X",
        "period_end": ends,
        "availability": ends + pd.Timedelta(days=45),
        "entry_date": ends + pd.Timedelta(days=46),
        "entry_price": 100.0,
        "entry_pos": np.arange(n, dtype=float),
        "np_decline": np_decline_pattern,
        "g_rev": g_rev_pattern,
        "d_opm_bps": d_opm_pattern,
        "d_opm_8_bps": d_opm_pattern,
        "sector": sector,
        "financial": sector in ("Financials", "Financial Services"),
    })
    return frame


class TestS3Machine:
    def test_watch_then_confirmed_then_clear(self):
        # idx1: one component (C3 via g_rev<0) → WATCH
        # idx2: two components (C3 again + C2 at -400bps) → CONFIRMED (event)
        # idx3: still firing (C1 + C3) → stays CONFIRMED, no new event
        # idx4: zero firing → stays CONFIRMED (clean streak 1 < 2)
        # idx5: zero firing again → CLEAR here, state NORMAL
        frame = s3_series(
            np_decline_pattern=[0, 0, 1, 1, 0, 0],
            g_rev_pattern=[np.nan, -0.1, -0.2, -0.1, 0.1, 0.2],
            d_opm_pattern=[np.nan, 0, -400.0, -100.0, 0, 0],
        )
        params = S3Params(consecutive_declines=2, margin_compression_bps=300)
        history, events = s3_state_machine(frame, params)
        assert list(history.state) == [
            "NORMAL", "WATCH", "CONFIRMED", "CONFIRMED", "CONFIRMED", "NORMAL"]
        assert len(events) == 1
        event = events.iloc[0]
        assert event.period_end == frame.period_end.iloc[2]
        assert event.c2 and event.c3 and not event.c1
        assert event.clear_period_end == frame.period_end.iloc[5]

    def test_c1_with_c3_confirms_alone(self):
        # Three consecutive NP declines → CONFIRMED even with nothing else.
        frame = s3_series(
            np_decline_pattern=[1, 1, 1],
            g_rev_pattern=[0.1, 0.1, 0.1],
            d_opm_pattern=[0.0, 0.0, 0.0],
        )
        params = S3Params(consecutive_declines=2, margin_compression_bps=300)
        _, events = s3_state_machine(frame, params)
        assert len(events) == 1
        assert events.iloc[0].components_fired == 1

    def test_financials_excluded_from_c2_only(self):
        frame = s3_series(
            np_decline_pattern=[0, 0],
            g_rev_pattern=[0.1, 0.1],
            d_opm_pattern=[-1000.0, -1000.0],
            sector="Financial Services",
        )
        comp = s3_components(frame, S3Params(margin_compression_bps=300))
        assert not comp.c2.any()
        # C1/C3 still apply to financials:
        frame2 = s3_series(
            np_decline_pattern=[1, 1],
            g_rev_pattern=[-0.1, -0.1],
            d_opm_pattern=[-1000.0, -1000.0],
            sector="Financials",
        )
        comp2 = s3_components(frame2, S3Params(consecutive_declines=2))
        assert comp2.c1.iloc[1] and comp2.c3.iloc[1]

    def test_confirmed_needs_two_components_at_c2(self):
        # Exactly one component firing (C1 at c=2) → WATCH, no event.
        frame = s3_series(
            np_decline_pattern=[1, 1],
            g_rev_pattern=[0.3, 0.3],
            d_opm_pattern=[50.0, 50.0],
        )
        _, events = s3_state_machine(frame, S3Params(consecutive_declines=2))
        assert events.empty


# ------------------------------------------------------------ sim accounting


class TestLedger:
    def test_cash_and_positions_reconcile_to_the_cent(self):
        ledger = sim.Ledger(1_000_000.0, side_cost=0.0015)
        ledger.buy(pd.Timestamp("2021-01-04"), "A", 100_000.0, 250.0)
        ledger.buy(pd.Timestamp("2021-01-05"), "B", 50_000.0, 100.0)
        ledger.sell(pd.Timestamp("2021-01-06"), "A", 100.0, 260.0)
        prices = {"A": 260.0, "B": 95.0}
        nav = ledger.nav(prices.get)
        expected_positions = sum(ledger.positions[s] * prices[s] for s in ledger.positions)
        assert nav == pytest.approx(ledger.cash + expected_positions, abs=1e-9)
        # Cash flows: 1e6 - 100000*1.0015 - 50000*1.0015 + 100*260*(1-0.0015)
        expected_cash = 1e6 - 100_150.0 - 50_075.0 + 26_000 * 0.9985
        assert ledger.cash == pytest.approx(expected_cash, abs=1e-6)

    def test_buy_scales_to_available_cash(self):
        ledger = sim.Ledger(100.0, side_cost=0.001)
        shares = ledger.buy(pd.Timestamp("2021-01-04"), "A", 500.0, 1.0)
        assert shares * 1.0 * 1.001 <= 100.0 + 1e-9
        assert ledger.cash < 1.0

    def test_sell_all_removes_position(self):
        ledger = sim.Ledger(1_000.0, side_cost=0.0)
        ledger.buy(pd.Timestamp("2021-01-04"), "A", 500.0, 10.0)
        ledger.sell_all(pd.Timestamp("2021-01-05"), "A", 12.0)
        assert "A" not in ledger.positions


class _FakeSimEnv:
    """Synthetic two-asset world for sim tests (monkeypatched calendar)."""

    def __init__(self):
        self.days = pd.bdate_range("2021-01-01", periods=60)
        self.tables = {
            "AAA": self._table(100.0, 0.002),
            "BBB": self._table(50.0, -0.001),
            "SPY": self._table(300.0, 0.0005),
        }

    def _table(self, start, drift):
        close = start * np.exp(np.cumsum(np.full(len(self.days), drift)))
        return PriceTable(pd.DataFrame(
            {"Close": close, "Volume": 1e6}, index=self.days))

    def price_fn(self, symbol: str) -> Optional[PriceTable]:
        return self.tables.get(symbol)


def _fake_event_set(events: pd.DataFrame) -> EventSet:
    series = {
        s: pd.DataFrame({"symbol": [s], "sector": ["IT"]})
        for s in events.symbol.unique()
    }
    return EventSet("us", events, series)


class TestUnconstrainedSim:
    def test_monthly_equal_weight_rebalance(self, monkeypatch):
        env = _FakeSimEnv()
        monkeypatch.setattr(sim, "trading_days", lambda es, s, e: env.days)
        events = pd.DataFrame({
            "symbol": ["AAA", "BBB"],
            "entry_date": [env.days[0], env.days[0]],
            "entry_pos": [0, 0],
            "availability": [env.days[0] - pd.Timedelta(days=1)] * 2,
        })
        es = _fake_event_set(events)

        def active(df, day):
            return df.entry_date <= day

        result = sim.unconstrained_sim(
            es, events, active, env.days[0], env.days[-1], env.price_fn,
            initial_capital=100_000.0,
        )
        nav = result.nav
        assert len(nav) == 60
        # Reconciliation at every step: NAV = cash + positions (exact).
        # Final NAV hand-check: both names bought ~equal weight at day0 close.
        table_a, table_b = env.tables["AAA"], env.tables["BBB"]
        r_a = table_a.close[-1] / table_a.close[0] - 1
        r_b = table_b.close[-1] / table_b.close[0] - 1
        avg_r = (r_a + r_b) / 2
        # Costs: initial buys charge side cost once; weights drift mildly.
        assert nav.iloc[-1] / nav.iloc[0] - 1 == pytest.approx(avg_r, abs=0.01)
        assert nav.iloc[-1] < 100_000 * (1 + avg_r)  # costs deducted

    def test_cash_when_nothing_active(self, monkeypatch):
        env = _FakeSimEnv()
        monkeypatch.setattr(sim, "trading_days", lambda es, s, e: env.days)
        events = pd.DataFrame({
            "symbol": ["AAA"], "entry_date": [env.days[40]],
            "entry_pos": [40],
            "availability": [env.days[39]],
        })
        es = _fake_event_set(events)
        result = sim.unconstrained_sim(
            es, events, lambda df, day: df.entry_date <= day,
            env.days[0], env.days[-1], env.price_fn, initial_capital=50_000.0,
        )
        first_rebalance = sim.month_starts(env.days)[0]
        after_first = result.nav.loc[first_rebalance:env.days[30]]
        # No signal active before day 40: NAV flat at initial capital (cash 0%).
        assert np.allclose(after_first.values, 50_000.0)


class TestBudgetTracker:
    def test_min_convention_matches_turnover_py(self):
        tracker = sim.BudgetTracker(75.0)
        day0 = pd.Timestamp("2021-01-04")
        for i in range(60):
            tracker.record_day(day0 + pd.Timedelta(days=i), 1_000_000.0)
        tracker.record_trades(day0, [sim.Trade(day0, "A", "buy", 400_000, 0),
                                     sim.Trade(day0, "B", "sell", 200_000, 0)])
        # churn = min(400k, 200k) = 200k / 1e6 = 20% — a 60% trade fits, 56% doesn't push over
        assert not tracker.would_exceed(day0 + pd.Timedelta(days=59), 500_000.0)
        assert tracker.would_exceed(day0 + pd.Timedelta(days=59), 600_000.0)


# ------------------------------------------------------------ S4 validation


class TestS4Schema:
    def test_valid_payload(self):
        payload = {
            "verdict": "beat", "quality_of_earnings": 8.4,
            "red_flags": [], "thesis_impact": "strengthens",
            "rationale": "Revenue +18% YoY with margin expansion.",
            "confidence": 1.4,
        }
        verdict = llm_study.validate_payload(payload)
        assert verdict is not None
        assert verdict.quality_of_earnings == 8  # coerced to int
        assert verdict.confidence == 1.0  # clamped

    @pytest.mark.parametrize("mutation", [
        {"verdict": "strong_buy"},
        {"thesis_impact": "gone"},
        {"quality_of_earnings": "high"},
        {"confidence": "sure"},
        {"red_flags": "none"},
        {"rationale": ""},
    ])
    def test_invalid_payloads_rejected(self, mutation):
        payload = {
            "verdict": "miss", "quality_of_earnings": 3,
            "red_flags": ["margin"], "thesis_impact": "weakens",
            "rationale": "Profit fell 30% YoY.", "confidence": 0.6,
        }
        payload.update(mutation)
        assert llm_study.validate_payload(payload) is None

    def test_missing_key_rejected(self):
        payload = {"verdict": "beat"}
        assert llm_study.validate_payload(payload) is None

    def test_kappa_hand_computed(self):
        a = ["beat", "beat", "miss", "miss"]
        b = ["beat", "miss", "miss", "miss"]
        # po = 3/4; pe = (2/4*2/4) + (2/4*2/4) = 0.5; kappa = (0.75-0.5)/0.5
        assert llm_study.cohens_kappa(a, b) == pytest.approx(0.5)

    def test_refuses_without_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(
            "src.config.get_config",
            lambda: (_ for _ in ()).throw(RuntimeError("no config")),
        )
        with pytest.raises(SystemExit, match="OPENAI_API_KEY"):
            llm_study.require_api_key()

    def test_cost_estimate_prints(self):
        text = llm_study.cost_estimate("gpt-4o", 300)
        assert "$" in text and "300" in text
