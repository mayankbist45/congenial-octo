from __future__ import annotations

import copy
from pathlib import Path

import yaml

from src.config import Config, get_persona_config
from src.decision.briefing import BriefingBuilder
from src.quant.scoring import ScoringEngine
from src.risk.turnover import TurnoverStatus
from tests.conftest import make_fundamental, make_portfolio, make_position, make_technical


def test_persona_config_exposes_richer_mandate_fields() -> None:
    cfg = get_persona_config("steady_compounder")

    assert cfg.mandate["investment_horizon_years"] == 5
    assert cfg.mandate["market_cap_limits"]["allowed"] == ["large", "mid"]
    assert cfg.analytics["preferred_sector_models"] == [
        "banks",
        "insurance",
        "retail",
        "telecom",
    ]


def test_us_persona_mandate_inherits_sp500_benchmark() -> None:
    cfg = get_persona_config("steady_compounder_us")
    assert cfg.mandate["benchmark_label"] == "S&P 500"
    assert cfg.mandate["benchmark_symbol"] == "^GSPC"


def test_legacy_config_gets_default_mandate_and_analytics(tmp_path, config) -> None:
    raw = copy.deepcopy(config.raw)
    raw.pop("mandate", None)
    raw.pop("analytics", None)
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    cfg = Config(raw=yaml.safe_load(path.read_text(encoding="utf-8")), secrets=config.secrets, root=Path(tmp_path))
    from src.config import _apply_policy_defaults

    _apply_policy_defaults(cfg.raw)

    assert cfg.mandate["benchmark_label"] == cfg.get("fund.benchmark_label")
    assert "banks" in cfg.analytics["sector_models"]


def test_bank_names_are_not_penalized_by_non_bank_leverage_gate(temp_config) -> None:
    engine = ScoringEngine(temp_config)
    fundamentals = {
        "HDFCBANK": make_fundamental(
            "HDFCBANK",
            debt_to_equity=8.0,
            sector_model="banks",
            sector_metrics={"nim": 4.1, "gnpa": 1.3, "cet1": 15.0},
        ),
        "ICICIBANK": make_fundamental(
            "ICICIBANK",
            debt_to_equity=9.0,
            sector_model="banks",
            sector_metrics={"nim": 4.0, "gnpa": 1.5, "cet1": 14.5},
        ),
    }
    technicals = {symbol: make_technical(symbol) for symbol in fundamentals}

    scores = engine.score(fundamentals, technicals)

    assert scores["HDFCBANK"].passes_gates
    assert not any("debt/equity" in failure for failure in scores["HDFCBANK"].gate_failures)


def test_saas_sector_metrics_flow_into_scoring(temp_config) -> None:
    raw = copy.deepcopy(temp_config.raw)
    raw.setdefault("analytics", {})["metric_weights"] = {"arr": 2.0}
    cfg = Config(raw=raw, secrets=temp_config.secrets, root=temp_config.root, persona_id=temp_config.persona_id)
    engine = ScoringEngine(cfg)
    fundamentals = {
        "WIPRO": make_fundamental(
            "WIPRO",
            sector_model="saas",
            sector_metrics={"arr": 200.0, "rule_of_40": 48.0},
        ),
        "HCLTECH": make_fundamental(
            "HCLTECH",
            sector_model="saas",
            sector_metrics={"arr": 150.0, "rule_of_40": 35.0},
        ),
    }
    technicals = {symbol: make_technical(symbol) for symbol in fundamentals}

    scores = engine.score(fundamentals, technicals)

    assert "arr" in scores["WIPRO"].contributing_metrics
    assert "arr" in scores["WIPRO"].supported_metrics
    assert "nrr" in scores["WIPRO"].unsupported_metrics
    assert scores["WIPRO"].metric_coverage_pct is not None
    assert scores["WIPRO"].sector_model == "saas"


def test_briefing_includes_mandate_and_sector_analytics(temp_config) -> None:
    builder = BriefingBuilder(temp_config)
    portfolio = make_portfolio([make_position("TCS")], cash=100_000.0)
    text = builder.build(
        portfolio=portfolio,
        scores={},
        fundamentals={"TCS": make_fundamental("TCS", sector_model="saas")},
        technicals={"TCS": make_technical("TCS")},
        triggers=[],
        candidates=[],
        digest=None,
        risk_profile=None,
        quarterly_context=None,
        turnover=TurnoverStatus(
            budget_pct=75.0,
            used_pct=10.0,
            purchases=50_000.0,
            sales=50_000.0,
            average_net_assets=1_000_000.0,
            window_days=365,
        ),
        is_deep_review=False,
    )

    assert "Sector modules" in text
    assert "Target excess return" in text
    assert "missing analytics are not a green light" in text.lower()
