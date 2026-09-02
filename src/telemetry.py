"""Optional OpenTelemetry wiring that keeps local/test installs dependency-free."""

from __future__ import annotations

from typing import Any

from src.logging_setup import get_logger

logger = get_logger(__name__)


def instrument_fastapi(app: Any) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        return
    FastAPIInstrumentor.instrument_app(app, excluded_urls="/healthz")
    logger.info("OpenTelemetry FastAPI instrumentation enabled")
