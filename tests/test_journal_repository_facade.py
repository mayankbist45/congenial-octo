"""Compatibility checks for the journal facade."""

from __future__ import annotations

from src.journal.repository import JournalRepository
from src.journal.run_repository import RunRepository


def test_journal_repository_delegates_to_focused_repositories(temp_config) -> None:
    repo = JournalRepository(temp_config)

    assert isinstance(repo._runs, RunRepository)
    assert callable(repo.latest_run)
    assert callable(repo.pending_proposals)
    assert callable(repo.nav_history)
    assert callable(repo.learning_lessons)
    assert callable(repo.meta_decisions)
