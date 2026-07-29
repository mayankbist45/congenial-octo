"""Shared logging configuration."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

_CONFIGURED = False


def setup_logging(level: str = "INFO", log_file: Optional[Path] = None) -> None:
    """Configure root logging once, to stderr and optionally to a file."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Third-party libraries are chatty; keep their noise out of the journal.
    for noisy in ("urllib3", "yfinance", "peewee", "openai", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
