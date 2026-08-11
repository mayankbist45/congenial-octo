"""Compatibility facade for focused journal repositories."""

from __future__ import annotations

from typing import Any, Optional

from src.config import Config, get_config
from src.journal.intraday_repository import IntradayRepository
from src.journal.learning_repository import LearningRepository
from src.journal.nav_repository import NavRepository
from src.journal.proposal_repository import ProposalRepository
from src.journal.run_repository import RunRepository
from src.logging_setup import get_logger

logger = get_logger(__name__)


class JournalRepository:
    """Backwards-compatible facade over focused repositories."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._persona_id = self._config.persona_id
        self._runs = RunRepository(self._config)
        self._proposals = ProposalRepository(self._config)
        self._nav = NavRepository(self._config)
        self._learning = LearningRepository(self._config)
        self._intraday = IntradayRepository(self._config)
        self._delegates = (
            self._runs,
            self._proposals,
            self._nav,
            self._learning,
            self._intraday,
        )

    def __getattr__(self, name: str) -> Any:
        for delegate in self._delegates:
            if hasattr(delegate, name):
                return getattr(delegate, name)
        raise AttributeError(f"{type(self).__name__!s} has no attribute {name!r}")
