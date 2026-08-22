"""Shared logging configuration."""

from __future__ import annotations

import contextlib
import contextvars
from datetime import datetime, timezone
import logging
import re
import sys
from pathlib import Path
from typing import Dict, Iterator, Optional

_CONFIGURED = False
_ROOT_HANDLERS: list[logging.Handler] = []
_PERSONA_ROUTER: Optional["PersonaFileRouterHandler"] = None

_DEFAULT_CONTEXT = {
    "persona_id": "-",
    "run_id": "-",
    "run_label": "-",
    "command": "-",
    "market_id": "-",
}
_LOG_CONTEXT: contextvars.ContextVar[Dict[str, str]] = contextvars.ContextVar(
    "log_context",
    default=dict(_DEFAULT_CONTEXT),
)


class LogContextFilter(logging.Filter):
    """Inject persona-aware metadata into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = dict(_DEFAULT_CONTEXT)
        context.update(_LOG_CONTEXT.get())
        for key, default in _DEFAULT_CONTEXT.items():
            value = context.get(key, default)
            setattr(record, key, _normalize_context_value(value, default))
        return True


class PersonaFileRouterHandler(logging.Handler):
    """Dispatch log records to a per-persona text log."""

    def __init__(self, base_dir: Path):
        super().__init__()
        self._base_dir = base_dir
        self._handlers: Dict[str, logging.FileHandler] = {}

    def emit(self, record: logging.LogRecord) -> None:
        persona_id = str(getattr(record, "persona_id", "-") or "-").strip()
        if persona_id == "-":
            return
        handler = self._handler_for(persona_id)
        handler.emit(record)

    def close(self) -> None:
        try:
            for handler in self._handlers.values():
                handler.close()
            self._handlers.clear()
        finally:
            super().close()

    def _handler_for(self, persona_id: str) -> logging.FileHandler:
        handler = self._handlers.get(persona_id)
        if handler is not None:
            return handler
        safe_name = _sanitize_persona_id(persona_id)
        path = self._base_dir / f"{safe_name}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        if self.formatter is not None:
            handler.setFormatter(self.formatter)
        for item in self.filters:
            handler.addFilter(item)
        self._handlers[persona_id] = handler
        return handler


def _normalize_context_value(value: object, default: str = "-") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _sanitize_persona_id(persona_id: str) -> str:
    sanitized = re.sub(r"[\\/]+", "_", persona_id.strip())
    return sanitized or "unknown"


def make_run_label(command: str, persona_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cmd = _sanitize_persona_id(command).lower()
    persona = _sanitize_persona_id(persona_id).lower()
    return f"{cmd}-{persona}-{stamp}"


def bind_log_context(
    *,
    persona_id: Optional[str] = None,
    run_id: Optional[object] = None,
    run_label: Optional[str] = None,
    command: Optional[str] = None,
    market_id: Optional[str] = None,
) -> None:
    context = dict(_LOG_CONTEXT.get())
    updates = {
        "persona_id": persona_id,
        "run_id": run_id,
        "run_label": run_label,
        "command": command,
        "market_id": market_id,
    }
    for key, value in updates.items():
        if value is not None:
            context[key] = _normalize_context_value(value)
    _LOG_CONTEXT.set(context)


def clear_log_context() -> None:
    _LOG_CONTEXT.set(dict(_DEFAULT_CONTEXT))


@contextlib.contextmanager
def logging_context(**kwargs: object) -> Iterator[None]:
    token = _LOG_CONTEXT.set(dict(_LOG_CONTEXT.get()))
    try:
        bind_log_context(**kwargs)
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def _reset_logging_for_tests() -> None:
    global _CONFIGURED, _ROOT_HANDLERS, _PERSONA_ROUTER
    root = logging.getLogger()
    for handler in list(_ROOT_HANDLERS):
        root.removeHandler(handler)
        handler.close()
    _ROOT_HANDLERS = []
    if _PERSONA_ROUTER is not None:
        _PERSONA_ROUTER.close()
        _PERSONA_ROUTER = None
    _CONFIGURED = False
    clear_log_context()


def setup_logging(
    level: str = "INFO",
    log_file: Optional[Path] = None,
    *,
    persona_id: Optional[str] = None,
    command: Optional[str] = None,
    run_label: Optional[str] = None,
    persona_log_dir: Optional[Path] = None,
) -> None:
    """Configure root logging once, to stderr and optionally to a file."""
    global _CONFIGURED, _ROOT_HANDLERS, _PERSONA_ROUTER
    if persona_id is not None or command is not None or run_label is not None:
        bind_log_context(persona_id=persona_id, command=command, run_label=run_label)
    if _CONFIGURED:
        return

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-28s "
        "[persona=%(persona_id)s run=%(run_label)s db_run=%(run_id)s "
        "cmd=%(command)s market=%(market_id)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    context_filter = LogContextFilter()

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(context_filter)
    root.addHandler(stream_handler)
    _ROOT_HANDLERS.append(stream_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(context_filter)
        root.addHandler(file_handler)
        _ROOT_HANDLERS.append(file_handler)

    if persona_log_dir is not None:
        persona_log_dir.mkdir(parents=True, exist_ok=True)
        _PERSONA_ROUTER = PersonaFileRouterHandler(persona_log_dir)
        _PERSONA_ROUTER.setFormatter(formatter)
        _PERSONA_ROUTER.addFilter(context_filter)
        root.addHandler(_PERSONA_ROUTER)
        _ROOT_HANDLERS.append(_PERSONA_ROUTER)

    # Third-party libraries are chatty; keep their noise out of the journal.
    for noisy in ("urllib3", "yfinance", "peewee", "openai", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
