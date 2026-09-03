from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from src.workspace.agent_platform import WorkScope
from src.workspace.agent_sources import AgentSourceCollector


class FakeResponse:
    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _collector(session=None, symbols=(), market=None, exchange=None):
    return AgentSourceCollector(scope=WorkScope(symbols=list(symbols), market=market, exchange=exchange), session=session)


def test_eodhd_is_explicitly_unavailable_without_credentials(monkeypatch):
    monkeypatch.delenv("EODHD_API_TOKEN", raising=False)
    collector = _collector(symbols=["NVDA", "AMD"])
    collector.fetch_eodhd_market_data()
    collector.fetch_eodhd_fundamentals()
    assert {source.coverage_status for source in collector.sources} == {"unavailable"}
    assert {source.category for source in collector.sources} == {"market_data", "fundamentals"}


def test_eodhd_marks_old_vendor_data_stale(monkeypatch):
    monkeypatch.setenv("EODHD_API_TOKEN", "test-token")
    old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    collector = _collector(FakeSession([FakeResponse({"timestamp": old})]), symbols=["NVDA"])
    result = collector.fetch_eodhd_market_data()
    record = collector.sources[-1]
    assert record.label == "EODHD"
    assert record.coverage_status == "stale"
    assert result["provider"] == "EODHD"
    assert "api_token" not in record.source_url


def test_eodhd_malformed_or_rate_limited_responses_are_unavailable(monkeypatch):
    monkeypatch.setenv("EODHD_API_TOKEN", "test-token")
    malformed = _collector(FakeSession([FakeResponse(ValueError("bad json"))]), symbols=["NVDA"])
    malformed.fetch_eodhd_market_data()
    assert malformed.sources[-1].coverage_status == "unavailable"

    limited = _collector(FakeSession([FakeResponse(error=requests.HTTPError("429"))]), symbols=["NVDA"])
    limited.fetch_eodhd_market_data()
    assert limited.sources[-1].coverage_status == "unavailable"


def test_sec_filings_require_user_agent_and_normalize_facts(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    unconfigured = _collector(symbols=["NVDA"], market="US equities")
    result = unconfigured.fetch_primary_filings()
    assert result["coverage_status"] == "unavailable"
    assert unconfigured.sources[-1].label == "SEC EDGAR"

    monkeypatch.setenv("SEC_USER_AGENT", "Nivelo Research research@example.com")
    session = FakeSession([
        FakeResponse({"0": {"ticker": "NVDA", "cik_str": 1045810}}),
        FakeResponse({"entityName": "NVIDIA CORP", "facts": {"us-gaap": {"Assets": {"units": {"USD": []}}}}}),
    ])
    collector = _collector(session, symbols=["NVDA"], market="US equities")
    result = collector.fetch_primary_filings()
    record = collector.sources[-1]
    assert record.label == "SEC EDGAR"
    assert record.coverage_status == "available"
    assert record.details["cik"] == "0001045810"
    assert result["data"]["entityName"] == "NVIDIA CORP"


def test_exchange_disclosures_require_explicit_configuration(monkeypatch):
    monkeypatch.setenv("WORKSPACE_PRIMARY_DISCLOSURES_ENABLED", "false")
    india = _collector(symbols=["INFY"], market="India equities", exchange="NSE")
    india.fetch_primary_filings()
    assert india.sources[-1].coverage_status == "unavailable"

    monkeypatch.setenv("WORKSPACE_PRIMARY_DISCLOSURES_ENABLED", "true")
    monkeypatch.setenv("BSE_DISCLOSURES_URL", "https://bse.example.test/disclosures")
    monkeypatch.setenv("BSE_DATAFEED_API_KEY", "test-key")
    for exchange in ("NSE", "BSE"):
        session = FakeSession([FakeResponse([{"subject": "Quarterly results"}])])
        collector = _collector(session, symbols=["INFY"], market="India equities", exchange=exchange)
        result = collector.fetch_primary_filings()
        record = collector.sources[-1]
        assert record.coverage_status == "available"
        assert record.details["exchange"] == exchange
        assert result["coverage_status"] == "available"


def test_portfolio_snapshot_degrades_gracefully_and_reads_frozen_persona(monkeypatch):
    import src.workspace.dashboard_snapshot as snap_mod
    from src.workspace.agent_sources import AgentSourceCollector as Collector

    class FakeSnapshots:
        def __init__(self, config=None):
            pass

        def latest(self):
            return {
                "as_of": "2026-09-01T00:00:00+00:00",
                "snapshot_date": "2026-09-01",
                "personas": [{
                    "id": "balanced",
                    "metadata": {"fund_name": "Balanced India Research"},
                    "overview": {"total_value": 1_000_000, "total_return_pct": 5.0, "position_count": 12, "cash_pct": 8.0},
                    "risk": {"portfolio_beta": 1.0, "market_regime": "calm", "warnings": []},
                    "holdings": [{"symbol": "TCS", "company_name": "TCS Ltd", "weight_pct": 8.0, "return_pct": 3.0}],
                }],
            }

    monkeypatch.setattr(snap_mod, "DashboardSnapshotService", FakeSnapshots)
    found = Collector(scope=WorkScope(portfolio_persona_id="balanced"), run={}).read_portfolio_snapshot("balanced")
    assert found["coverage_status"] == "available"
    assert found["data"]["fund_name"] == "Balanced India Research"

    missing = Collector(scope=WorkScope(portfolio_persona_id="nope"), run={}).read_portfolio_snapshot("nope")
    assert missing["coverage_status"] == "unavailable"


def test_case_artifacts_require_a_case_and_completed_runs(temp_config):
    collector = _collector()
    result = collector.read_case_artifacts()
    assert result["coverage_status"] == "unavailable"
    assert collector.sources[-1].label == "Completed case artifacts"
