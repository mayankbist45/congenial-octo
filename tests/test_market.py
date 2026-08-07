"""Tests for market registry and calendar."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from src.config import get_persona_config
from src.market.calendar import SessionCalendar, calendar_for_config
from src.market.registry import (
    get_market,
    list_markets,
    persona_strategy_type,
    resolve_market_id,
)


def test_markets_load():
    markets = list_markets()
    assert "india_equity" in {m.id for m in markets}
    assert "us_equity" in {m.id for m in markets}


def test_india_session_hours():
    market = get_market("india_equity")
    cal = SessionCalendar(market)
    open_t, close_t = cal.open_close()
    assert open_t.hour == 9 and open_t.minute == 15
    assert close_t.hour == 15 and close_t.minute == 30


def test_us_session_hours():
    market = get_market("us_equity")
    cal = SessionCalendar(market)
    open_t, close_t = cal.open_close()
    assert open_t.hour == 9 and open_t.minute == 30
    assert close_t.hour == 16 and close_t.minute == 0


def test_persona_market_resolution():
    india = get_persona_config("calan")
    us = get_persona_config("calan_us")
    assert india.market_id == "india_equity"
    assert us.market_id == "us_equity"
    assert india.market_region == "IN"
    assert us.market_region == "US"


def test_persona_strategy_type():
    assert persona_strategy_type("calan_us") == "calan"
    assert persona_strategy_type("revert") == "revert"


def test_resolve_market_id_alias():
    assert resolve_market_id("calan_us") == "us_equity"
    assert resolve_market_id("calan") == "india_equity"


def test_session_window_weekend():
    config = get_persona_config("calan")
    cal = calendar_for_config(config)
    # 2026-08-08 is Saturday
    assert cal.session_window(date(2026, 8, 8)) is None


def test_format_local_includes_tz():
    market = get_market("india_equity")
    cal = SessionCalendar(market)
    dt = datetime(2026, 8, 6, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    formatted = cal.format_local(dt)
    assert "IST" in formatted


def test_us_watch_bounds_cross_midnight_ist():
    """US stop time in IST should be after session close ET."""
    from scripts.generate_schedule import _watch_bounds

    market = get_market("us_equity")
    sample_day = date(2026, 8, 6)  # Thursday
    start, stop = _watch_bounds(market, sample_day)
    ist = ZoneInfo("Asia/Kolkata")
    assert start.astimezone(ist).hour in {18, 19, 20}
    assert stop.astimezone(ist).date() >= start.astimezone(ist).date()


def test_enabled_intraday_personas_respects_market_schedule():
    from src.market.scheduling import enabled_intraday_personas

    india = enabled_intraday_personas("india_equity")
    us = enabled_intraday_personas("us_equity")
    assert india == ["calan", "mayank"]
    assert "mayank_us" in us
    assert "calan_us" in us
    assert "flux" not in india
