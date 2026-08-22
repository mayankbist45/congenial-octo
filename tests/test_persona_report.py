"""Tests for EOD persona WhatsApp digests."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src.notify.persona_report import (
    collect_market_summaries,
    format_market_report,
)
from src.notify.whatsapp import WhatsAppConfig, _normalize_whatsapp_address, send_whatsapp_message


def test_normalize_whatsapp_address():
    assert _normalize_whatsapp_address("+919876543210") == "whatsapp:+919876543210"
    assert _normalize_whatsapp_address("whatsapp:+14155550100") == "whatsapp:+14155550100"
    assert _normalize_whatsapp_address("919876543210") == "whatsapp:+919876543210"


def test_format_market_report_from_fixture(tmp_path: Path):
    day = date(2026, 8, 7)
    intraday = tmp_path / "data" / "intraday"
    intraday.mkdir(parents=True)
    (intraday / "calan_latest.json").write_text(
        json.dumps(
            {
                "persona_id": "calan",
                "start_date": "2026-08-07",
                "end_date": "2026-08-07",
                "total_trades": 0,
                "daily_net_pnl": 0.0,
                "total_return_pct": 0.0,
                "watchlist": [{"symbol": "BAJFINANCE", "confidence": 0.8}],
                "skipped_signals": [
                    {"reason": "stale signal (missed entry window)"},
                    {"reason": "stale signal (missed entry window)"},
                ],
                "completed_trades": [],
            }
        ),
        encoding="utf-8",
    )
    (intraday / "mayank_latest.json").write_text(
        json.dumps(
            {
                "persona_id": "mayank",
                "start_date": "2026-08-07",
                "total_trades": 1,
                "daily_net_pnl": -120.5,
                "total_return_pct": -0.02,
                "completed_trades": [
                    {
                        "symbol": "INFY",
                        "side": "BUY",
                        "strategy": "vwap_pullback",
                        "net_pnl": -120.5,
                        "exit_reason": "stop_loss",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    text = format_market_report("india_equity", day, root=tmp_path)
    assert "India Equity" in text
    assert "2026-08-07" in text
    assert "*calan*" in text
    assert "BAJFINANCE" in text
    assert "stale signal" in text
    assert "*mayank*" in text
    assert "INFY" in text
    assert "Book total" in text


def test_collect_marks_missing_when_stale(tmp_path: Path):
    day = date(2026, 8, 8)
    intraday = tmp_path / "data" / "intraday"
    intraday.mkdir(parents=True)
    (intraday / "calan_latest.json").write_text(
        json.dumps(
            {
                "persona_id": "calan",
                "start_date": "2026-08-07",
                "total_trades": 0,
            }
        ),
        encoding="utf-8",
    )
    rows = collect_market_summaries("india_equity", day, root=tmp_path)
    by_id = {r["persona_id"]: r for r in rows}
    assert by_id["calan"].get("missing") is True
    assert by_id["calan"].get("stale_date") == "2026-08-07"


def test_whatsapp_dry_run_no_network():
    cfg = WhatsAppConfig(
        account_sid="ACxxx",
        auth_user="ACxxx",
        auth_password="token",
        from_number="whatsapp:+14155238886",
        to_numbers=["whatsapp:+919800000000"],
        enabled=True,
    )
    result = send_whatsapp_message("hello", cfg, dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["recipients"] == ["whatsapp:+919800000000"]


def test_whatsapp_skips_when_unconfigured():
    cfg = WhatsAppConfig(
        account_sid="",
        auth_user="",
        auth_password="",
        from_number="",
        to_numbers=[],
        enabled=True,
    )
    result = send_whatsapp_message("hello", cfg)
    assert result["ok"] is False
    assert result["skipped"] is True


def test_generate_schedule_includes_daily_crypto_report():
    from src.market.registry import get_market
    from scripts.generate_schedule import _market_start_dow, _sample_trading_day, _stop_dow_expr
    from zoneinfo import ZoneInfo
    from datetime import date, datetime

    market = get_market("crypto_spot_us")
    assert _market_start_dow(market) == "*"
    assert _sample_trading_day(market, date(2026, 8, 9)) == date(2026, 8, 9)

    et = ZoneInfo("America/New_York")
    ist = ZoneInfo("Asia/Kolkata")
    start = datetime(2026, 8, 9, 18, 30, tzinfo=ist).astimezone(et)
    stop = datetime(2026, 8, 10, 2, 30, tzinfo=ist).astimezone(et)
    assert _stop_dow_expr(start, stop, ist, market) == "*"
