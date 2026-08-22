from __future__ import annotations

import logging

from src.logging_setup import (
    _reset_logging_for_tests,
    bind_log_context,
    clear_log_context,
    make_run_label,
    setup_logging,
)


def test_setup_logging_formats_shared_lines_without_persona_context(tmp_path) -> None:
    _reset_logging_for_tests()
    shared = tmp_path / "fund.log"
    persona_dir = tmp_path / "personas"

    try:
        setup_logging(level="INFO", log_file=shared, persona_log_dir=persona_dir)
        logging.getLogger("tests.logging").info("hello world")
    finally:
        _reset_logging_for_tests()

    text = shared.read_text(encoding="utf-8")
    assert "[persona=- run=- db_run=- cmd=- market=-]" in text
    assert "hello world" in text
    assert not persona_dir.exists() or not any(persona_dir.iterdir())


def test_persona_binding_routes_to_shared_and_persona_logs(tmp_path) -> None:
    _reset_logging_for_tests()
    shared = tmp_path / "fund.log"
    persona_dir = tmp_path / "personas"

    try:
        setup_logging(level="INFO", log_file=shared, persona_log_dir=persona_dir)
        bind_log_context(
            persona_id="calan",
            run_label="intraday_backtest-calan-20260811T120000Z",
            command="intraday_backtest",
            market_id="india_equity",
        )
        logging.getLogger("tests.logging").warning("ranked candidates ready")
    finally:
        _reset_logging_for_tests()

    shared_text = shared.read_text(encoding="utf-8")
    persona_text = (persona_dir / "calan.log").read_text(encoding="utf-8")
    expected = (
        "[persona=calan run=intraday_backtest-calan-20260811T120000Z "
        "db_run=- cmd=intraday_backtest market=india_equity]"
    )
    assert expected in shared_text
    assert expected in persona_text
    assert "ranked candidates ready" in persona_text


def test_rebinding_run_id_and_switching_personas_updates_target_files(tmp_path) -> None:
    _reset_logging_for_tests()
    shared = tmp_path / "fund.log"
    persona_dir = tmp_path / "personas"

    try:
        setup_logging(level="INFO", log_file=shared, persona_log_dir=persona_dir)
        bind_log_context(
            persona_id="calan",
            run_label=make_run_label("analyse", "calan"),
            command="analyse",
            market_id="india_equity",
        )
        logger = logging.getLogger("tests.logging")
        logger.info("before journal run")
        bind_log_context(run_id=42)
        logger.info("after journal run")
        bind_log_context(
            persona_id="calan_us",
            run_id="-",
            run_label=make_run_label("intraday_backtest", "calan_us"),
            command="intraday_backtest",
            market_id="us_equity",
        )
        logger.info("us run")
        clear_log_context()
        logger.info("after clear")
    finally:
        _reset_logging_for_tests()

    shared_text = shared.read_text(encoding="utf-8")
    calan_text = (persona_dir / "calan.log").read_text(encoding="utf-8")
    calan_us_text = (persona_dir / "calan_us.log").read_text(encoding="utf-8")

    assert "before journal run" in calan_text
    assert "db_run=42" in calan_text
    assert "us run" not in calan_text
    assert "us run" in calan_us_text
    assert "market=us_equity" in calan_us_text
    assert "after clear" in shared_text
    assert "after clear" not in calan_text
    assert "after clear" not in calan_us_text
