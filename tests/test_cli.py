"""CLI approval-routing tests.

The orchestrator enforces the paper-trading guard, but the command line decides
*which* approval path a run takes. Getting that wrong is how an unattended run
would silently sit waiting for a human, or how a run meant to be reviewed would
approve itself, so the routing is pinned down here.
"""

from __future__ import annotations

from argparse import Namespace
from typing import List, Optional

import pytest

from run_daily import _auto_mode, _clear_pending, _run_learning_for, build_parser
from src.orchestrator import AutoApprovalRefused


class StubManager:
    """Records which approval route was taken."""

    def __init__(self, enabled: bool = True, refuse: bool = False, pending: int = 1):
        self.auto_approval_enabled = enabled
        self.calls: List[str] = []
        self._refuse = refuse
        self._pending = pending

    def pending_count(self) -> int:
        return self._pending

    def auto_approve(self) -> int:
        if self._refuse:
            raise AutoApprovalRefused("DRY_RUN is false")
        self.calls.append("auto")
        return self._pending

    def wait_for_approval(self, timeout_minutes: int = 90) -> bool:
        self.calls.append("wait")
        return True


def args_with(auto: Optional[bool]) -> Namespace:
    return Namespace(auto_approve=auto, wait=90)


def test_config_decides_when_no_flag_is_given() -> None:
    assert _auto_mode(StubManager(enabled=True), args_with(None)) is True
    assert _auto_mode(StubManager(enabled=False), args_with(None)) is False


def test_the_flag_overrides_the_config_either_way() -> None:
    assert _auto_mode(StubManager(enabled=False), args_with(True)) is True
    assert _auto_mode(StubManager(enabled=True), args_with(False)) is False


def test_auto_mode_takes_the_unattended_route() -> None:
    manager = StubManager(enabled=True)
    _clear_pending(manager, args_with(None))
    assert manager.calls == ["auto"]


def test_require_approval_waits_for_a_human() -> None:
    manager = StubManager(enabled=True)
    _clear_pending(manager, args_with(False))
    assert manager.calls == ["wait"]


def test_a_refused_auto_approval_falls_back_to_waiting(capsys) -> None:
    """Live trading must not crash the run; it must ask for a person instead."""
    manager = StubManager(enabled=True, refuse=True)

    _clear_pending(manager, args_with(None))

    assert manager.calls == ["wait"]
    assert "Falling back to waiting" in capsys.readouterr().err


def test_require_approval_and_auto_approve_cannot_be_combined() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["cycle", "--auto-approve", "--require-approval"])


def test_approval_defaults_to_the_config_when_unspecified() -> None:
    assert build_parser().parse_args(["cycle"]).auto_approve is None


def test_learn_is_a_supported_command() -> None:
    assert build_parser().parse_args(["learn"]).command == "learn"


def test_learning_failures_are_reported_without_raising(monkeypatch, capsys) -> None:
    class BrokenEvaluator:
        def __init__(self, config):
            pass

        def run(self, as_of=None):
            raise RuntimeError("no price cache")

    monkeypatch.setattr("src.learning.LearningEvaluator", BrokenEvaluator)

    _run_learning_for(config={})

    assert "learning skipped: no price cache" in capsys.readouterr().err
