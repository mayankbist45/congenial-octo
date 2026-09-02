from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from src.workspace.agent_platform import agent_contract
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


def test_eodhd_is_explicitly_unavailable_without_credentials(monkeypatch):
    monkeypatch.delenv("EODHD_API_TOKEN", raising=False)
    records = AgentSourceCollector().collect(agent_contract("comparative_screen"), {"symbols": ["NVDA", "AMD"]})
    assert {record.coverage_status for record in records} == {"unavailable"}
    assert {record.category for record in records} == {"market_data", "fundamentals"}


def test_eodhd_marks_old_vendor_data_stale(monkeypatch):
    monkeypatch.setenv("EODHD_API_TOKEN", "test-token")
    old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    session = FakeSession([FakeResponse({"timestamp": old})])
    records = AgentSourceCollector(session).collect(agent_contract("company_deep_dive"), {"symbols": ["NVDA"]})
    eodhd = next(record for record in records if record.label == "EODHD")
    assert eodhd.coverage_status == "stale"
    assert "api_token" not in eodhd.source_url


def test_multi_symbol_eodhd_coverage_is_partial_until_every_symbol_is_collected(monkeypatch):
    monkeypatch.setenv("EODHD_API_TOKEN", "test-token")
    session = FakeSession([FakeResponse({"timestamp": datetime.now(timezone.utc).isoformat()}), FakeResponse({"timestamp": datetime.now(timezone.utc).isoformat()})])
    records = AgentSourceCollector(session).collect(agent_contract("comparative_screen"), {"symbols": ["NVDA", "AMD"]})
    assert records[0].coverage_status == "partial"
    assert records[0].details["reason"] == "Only the first requested symbol was retrieved."


def test_eodhd_malformed_or_rate_limited_responses_are_unavailable(monkeypatch):
    monkeypatch.setenv("EODHD_API_TOKEN", "test-token")
    malformed = AgentSourceCollector(FakeSession([FakeResponse(ValueError("bad json"))]))
    records = malformed.collect(agent_contract("company_deep_dive"), {"symbols": ["NVDA"]})
    assert next(record for record in records if record.label == "EODHD").coverage_status == "unavailable"

    limited = AgentSourceCollector(FakeSession([FakeResponse(error=requests.HTTPError("429"))]))
    records = limited.collect(agent_contract("company_deep_dive"), {"symbols": ["NVDA"]})
    assert next(record for record in records if record.label == "EODHD").coverage_status == "unavailable"


def test_sec_and_exchange_disclosures_require_explicit_configuration(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    us = AgentSourceCollector().collect(agent_contract("company_deep_dive"), {"symbols": ["NVDA"], "market": "US equities"})
    assert next(record for record in us if record.category == "filings").coverage_status == "unavailable"

    monkeypatch.setenv("WORKSPACE_PRIMARY_DISCLOSURES_ENABLED", "false")
    india = AgentSourceCollector().collect(agent_contract("company_deep_dive"), {"symbols": ["INFY"], "market": "India equities", "exchange": "NSE"})
    assert next(record for record in india if record.category == "filings").coverage_status == "unavailable"


def test_nse_and_bse_disclosures_normalize_enabled_responses(monkeypatch):
    monkeypatch.setenv("WORKSPACE_PRIMARY_DISCLOSURES_ENABLED", "true")
    for exchange in ("NSE", "BSE"):
        if exchange == "BSE":
            monkeypatch.setenv("BSE_DISCLOSURES_URL", "https://bse.example.test/disclosures")
            monkeypatch.setenv("BSE_DATAFEED_API_KEY", "test-key")
        session = FakeSession([FakeResponse([{"subject": "Quarterly results"}])])
        records = AgentSourceCollector(session).collect(
            agent_contract("company_deep_dive"),
            {"symbols": ["INFY"], "market": "India equities", "exchange": exchange},
        )
        filing = next(record for record in records if record.category == "filings")
        assert filing.coverage_status == "available"
        assert filing.details["exchange"] == exchange
        assert session.calls[0][0].startswith("https://")


def test_sec_primary_filing_and_case_artifact_sources_are_normalized(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Nivelo Research research@example.com")
    session = FakeSession([
        FakeResponse({"0": {"ticker": "NVDA", "cik_str": 1045810}}),
        FakeResponse({"facts": {"us-gaap": {}}}),
    ])
    collector = AgentSourceCollector(session)
    record = collector.collect(agent_contract("company_deep_dive"), {"symbols": ["NVDA"], "market": "US equities"})[-1]
    assert record.label == "SEC EDGAR"
    assert record.coverage_status == "available"
    assert record.details["cik"] == "0001045810"

    memo = collector.collect(agent_contract("ic_memo"), {}, completed_artifacts=2)
    assert memo[0].coverage_status == "available"
    assert memo[0].details == {"artifact_count": 2}
