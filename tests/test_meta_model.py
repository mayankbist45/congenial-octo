"""Tests for lightweight meta-label model."""

from __future__ import annotations

from src.intraday.meta_model import MetaLabelModel


def test_vectorize_is_stable_length() -> None:
    a = MetaLabelModel.vectorize({"gap_pct": 1.0, "strategy": "scalping", "side": "BUY"})
    b = MetaLabelModel.vectorize({"strategy": "other", "side": "SELL"})
    assert len(a) == len(b)
    assert len(a) > 20


def test_train_and_predict_roundtrip(tmp_path) -> None:
    rows = []
    for i in range(20):
        rows.append(
            {
                "features": {
                    "hour_of_day": 10.0,
                    "gap_pct": 1.5 if i % 2 == 0 else 0.1,
                    "trend_cleanliness": 0.8 if i % 2 == 0 else 0.2,
                    "volume_expansion": 2.5 if i % 2 == 0 else 0.8,
                    "gross_rr": 1.5,
                    "strategy": "scalping",
                    "side": "BUY",
                    "in_play": True,
                },
                "meta_label": 1 if i % 2 == 0 else 0,
            }
        )
    model, metrics = MetaLabelModel.train(rows, version="test_v1")
    assert metrics["n_samples"] == 20
    assert model.ready
    path = tmp_path / "meta.pkl"
    model.save(path)
    loaded = MetaLabelModel.load(path)
    score = loaded.should_take(
        {
            "gap_pct": 1.5,
            "trend_cleanliness": 0.8,
            "volume_expansion": 2.5,
            "strategy": "scalping",
            "side": "BUY",
            "in_play": True,
            "gross_rr": 1.5,
            "hour_of_day": 10.0,
        },
        min_confidence=0.4,
    )
    assert score.model_version == "test_v1"
    assert 0.0 <= score.confidence <= 1.0
