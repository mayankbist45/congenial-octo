from __future__ import annotations

import pickle
from datetime import date, timedelta

import pandas as pd

from src.journal.repository import JournalRepository
from src.learning.evaluator import LearningEvaluator
from src.learning.labels import classify_review
from src.learning.lessons import build_lessons
from src.models import Action, TradeProposal
from tests.conftest import make_diversified_portfolio


def _proposal(symbol: str = "WIPRO", action: Action = Action.BUY) -> TradeProposal:
    return TradeProposal(
        symbol=symbol,
        action=action,
        quantity=10,
        reference_price=100.0,
        estimated_value=1000.0,
        rationale="Quality business with improving growth.",
        thesis="Margins expand over the next year.",
        trigger_types=["initial_build"],
        confidence=0.7,
    )


def _seed_benchmark(repo: JournalRepository, start: date, days: int = 120) -> None:
    portfolio = make_diversified_portfolio(cash=100_000)
    for offset in range(days + 1):
        day = start + timedelta(days=offset)
        repo.record_nav(
            day,
            portfolio,
            benchmark_close=1000.0 + offset,
            daily_return_pct=0.0,
            benchmark_return_pct=0.0,
        )


def _write_price_cache(config, symbol: str, start: date, prices: list[float]) -> None:
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {"Close": prices},
        index=pd.date_range(start=start, periods=len(prices), freq="D"),
    )
    with (config.cache_dir / "price_history.pkl").open("wb") as handle:
        pickle.dump({symbol: frame}, handle)


def test_label_assignment_uses_conservative_insufficient_evidence() -> None:
    assert classify_review({"action": "BUY"}, None, 1.0, None) == "insufficient_evidence"


def test_label_assignment_flags_bad_buy_outcome() -> None:
    proposal = {"action": "BUY", "rationale": "reasonable valuation"}
    assert classify_review(proposal, -8.0, 1.0, -9.0) == "overpaid_for_growth"


def test_lesson_becomes_actionable_only_after_enough_evidence() -> None:
    reviews = [
        {
            "label": "bad_decision_bad_outcome",
            "symbol": "AAA" if idx < 3 else "BBB",
            "review_date": f"2026-01-{idx + 1:02d}",
            "review_window_days": 90,
            "excess_return_pct": -5.0,
            "stock_return_pct": -3.0,
        }
        for idx in range(5)
    ]

    [lesson] = build_lessons(reviews)

    assert lesson["status"] == "actionable"
    assert lesson["evidence_count"] == 5


def test_learning_evaluator_writes_reviews_and_deduplicates(temp_config) -> None:
    repo = JournalRepository(temp_config)
    start = date.today() - timedelta(days=100)
    _seed_benchmark(repo, start, days=110)
    _write_price_cache(temp_config, "WIPRO", start, [100.0 + i * 0.5 for i in range(111)])

    run_id = repo.start_run(start, False, 1_000_000.0, 100_000.0)
    [proposal_id] = repo.save_proposals(run_id, [_proposal()])
    repo.set_proposal_status(proposal_id, "approved")
    repo.record_trade(
        "WIPRO",
        "BUY",
        10,
        100.0,
        "dry_run",
        proposal_id=proposal_id,
        dry_run=True,
        executed_at=pd.Timestamp(start).to_pydatetime(),
    )
    repo.set_proposal_status(proposal_id, "executed", decided_by="executor")

    evaluator = LearningEvaluator(temp_config)
    first = evaluator.run(as_of=date.today())
    second = evaluator.run(as_of=date.today())

    assert first.reviews_written >= 2
    assert second.reviews_written >= 2
    reviews = repo.decision_reviews(limit=20)
    windows = {r["review_window_days"] for r in reviews}
    assert windows == {30, 90}
    assert len(reviews) == 2


def test_missing_price_history_persists_insufficient_evidence(temp_config) -> None:
    repo = JournalRepository(temp_config)
    start = date.today() - timedelta(days=40)
    _seed_benchmark(repo, start, days=45)
    run_id = repo.start_run(start, False, 1_000_000.0, 100_000.0)
    [proposal_id] = repo.save_proposals(run_id, [_proposal()])
    repo.set_proposal_status(proposal_id, "rejected", rejection_reason="risk")

    LearningEvaluator(temp_config).run(as_of=date.today() + timedelta(days=40))

    [review] = repo.decision_reviews(limit=10)
    assert review["label"] == "insufficient_evidence"
