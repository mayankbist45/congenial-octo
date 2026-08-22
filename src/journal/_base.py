"""Base utilities for focused journal repositories."""

from __future__ import annotations

from typing import Optional

from src.config import Config, get_config


class JournalRepositoryBase:
    """Common config/persona plumbing for focused repositories."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._persona_id = self._config.persona_id
