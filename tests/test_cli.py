"""CLI approval-routing tests.

The orchestrator enforces the paper-trading guard, but the command line decides
*which* approval path a run takes. Getting that wrong is how an unattended run
would silently sit waiting for a human, or how a run meant to be reviewed would
approve itself, so the routing is pinned down here.
"""

from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace
from typing import List, Optional

import pytest

from run_daily import (
    _auto_mode,
    _clear_pending,
    _run_learning_for,
    _scheduled_long_personas,
    build_parser,
    main,
)
from src.config import get_config
from src.orchestrator import AutoApprovalRefused
from src.scheduler_controls import SchedulerControls


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


def test_watch_is_a_supported_command() -> None:
    assert build_parser().parse_args(["watch", "--persona", "calan"]).command == "watch"


def test_long_cycle_is_a_supported_command() -> None:
    assert build_parser().parse_args(["long_cycle"]).command == "long_cycle"


def test_scheduled_long_cycle_selects_all_paper_long_horizon_personas() -> None:
    args = build_parser().parse_args(["long_cycle"])
    personas, problems = _scheduled_long_personas(args, get_config())

    assert problems == []
    assert personas == [
        "balanced",
        "steady_compounder",
        "small_cap_aggressor",
        "momentum_rider",
        "balanced_us",
        "steady_compounder_us",
        "momentum_rider_us",
    ]


def test_scheduled_long_cycle_filters_disabled_market_but_manual_does_not(temp_config, monkeypatch) -> None:
    SchedulerControls(temp_config).set("us_long_cycle", False, "admin@example.com")
    args = build_parser().parse_args(["long_cycle"])

    manual, _ = _scheduled_long_personas(args, temp_config)
    monkeypatch.setenv("FUND_SCHEDULED_RUN", "1")
    scheduled, _ = _scheduled_long_personas(args, temp_config)

    assert "balanced_us" in manual
    assert all(not persona.endswith("_us") for persona in scheduled)


def test_long_cycle_refuses_ineligible_personas_without_running(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "run_daily._scheduled_long_personas",
        lambda _args, _config: ([], ["balanced: DRY_RUN must be true for unattended execution"]),
    )
    monkeypatch.setattr(
        "run_daily._run_persona",
        lambda *_args: pytest.fail("ineligible long cycle must not start a persona"),
    )

    assert main(["long_cycle"]) == 1
    assert "Scheduled long cycle refused" in capsys.readouterr().err


class ScheduledLongCycleManager:
    """Minimal manager double for the unattended fresh-analysis path."""

    auto_approval_enabled = True

    def __init__(self, proposal_count: int = 1):
        self.calls: List[str] = []
        self.result = SimpleNamespace(
            candidates=["ABC", "XYZ"],
            proposal_count=proposal_count,
            technicals={"ABC": "technical"},
            fundamentals={"ABC": "fundamental"},
        )
        self.summary = SimpleNamespace(failed=[], describe=lambda: "1 order(s) placed")

    def analyse(self, **_kwargs):
        self.calls.append("analyse")
        return self.result

    def daily_monitor(self, **_kwargs):
        pytest.fail("scheduled long cycle must not use daily_monitor")

    def auto_approve(self) -> int:
        self.calls.append("auto_approve")
        return self.result.proposal_count

    def execute_approved(self, technicals, fundamentals):
        self.calls.append("execute_approved")
        assert technicals is self.result.technicals
        assert fundamentals is self.result.fundamentals
        return self.summary

    def disconnect(self) -> None:
        self.calls.append("disconnect")


def test_long_cycle_freshly_analyses_approves_and_executes_without_daily_monitor(
    monkeypatch, capsys
) -> None:
    manager = ScheduledLongCycleManager()
    monkeypatch.setattr("src.orchestrator.FundManager", lambda **_kwargs: manager)

    assert main(["long_cycle", "--persona", "balanced"]) == 0

    assert manager.calls == ["analyse", "auto_approve", "execute_approved", "disconnect"]
    output = capsys.readouterr().out
    assert "screened 2 candidate(s)" in output
    assert "Created 1 proposal(s)" in output
    assert "Auto-approved 1 proposal(s)" in output
    assert "1 order(s) placed" in output


def test_long_cycle_executes_cleanly_when_analysis_creates_no_proposals(monkeypatch, capsys) -> None:
    manager = ScheduledLongCycleManager(proposal_count=0)
    manager.summary = SimpleNamespace(failed=[], describe=lambda: "No approved trades to execute.")
    monkeypatch.setattr("src.orchestrator.FundManager", lambda **_kwargs: manager)

    assert main(["long_cycle", "--persona", "balanced"]) == 0

    assert manager.calls == ["analyse", "execute_approved", "disconnect"]
    output = capsys.readouterr().out
    assert "No actionable proposals were created." in output
    assert "No approved trades to execute." in output


def test_learning_failures_are_reported_without_raising(monkeypatch, capsys) -> None:
    class BrokenEvaluator:
        def __init__(self, config):
            pass

        def run(self, as_of=None):
            raise RuntimeError("no price cache")

    monkeypatch.setattr("src.learning.LearningEvaluator", BrokenEvaluator)

    _run_learning_for(config={})

    assert "learning skipped: no price cache" in capsys.readouterr().err
