from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from src.dashboard.app import create_app
from src.ipo.service import IpoResearchError, IpoResearchService


class FakeResponse:
    def __init__(self, text: str, content_type: str = "text/html") -> None:
        self.text = text
        self.content = text.encode()
        self.headers = {"content-type": content_type}

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def get(self, *args, **kwargs) -> FakeResponse:
        return self.response


def test_list_issues_filters_public_issue_documents(temp_config) -> None:
    html = """
    <a href="/docs/acme.pdf">Acme Limited - DRHP</a>
    <a href="/docs/acme-rhp.pdf">Acme Limited - RHP</a>
    <a href="/about">About SEBI</a>
    """
    service = IpoResearchService(temp_config, session=FakeSession(FakeResponse(html)))

    issues = service.list_issues()

    assert len(issues) == 2
    assert issues[0]["issuer"] == "Acme Limited"
    assert issues[0]["document_type"] == "DRHP"
    assert issues[0]["source_url"].endswith("/docs/acme.pdf")


def test_rules_dossier_exposes_coverage_and_source(temp_config) -> None:
    service = IpoResearchService(temp_config)
    service._llm = type("OfflineLLM", (), {"available": False})()

    dossier = service.research(
        "Acme Limited",
        "https://example.test/acme.pdf",
        document_text=(
            "Our Business\nAcme makes industrial sensors.\n"
            "Objects of the Offer\nFund capacity expansion and repay debt.\n"
            "Risk Factors\nCustomer concentration may affect revenue.\n"
            "Restated Financial Information\nRevenue grew over three years."
        ),
    )

    assert dossier["issuer"] == "Acme Limited"
    assert dossier["generated_by"] == "rules_only"
    assert dossier["coverage"]["business"] is True
    assert dossier["coverage"]["risks"] is True
    assert dossier["source_url"] == "https://example.test/acme.pdf"


def test_incomplete_official_coverage_cannot_be_overridden_by_llm(temp_config) -> None:
    service = IpoResearchService(temp_config)
    service._llm = type("OverconfidentLLM", (), {"available": True, "digest_model": "test", "complete_json": lambda *_args, **_kwargs: {
        "verdict": "PASS", "confidence": 0.99, "investment_case": "Buy", "coverage": {"business": True, "financials": True, "risks": True},
    }})()

    dossier = service.research("Acme Limited", "https://example.test/acme.pdf", document_text="Corrupt document placeholder")

    assert dossier["research_status"] == "unavailable"
    assert dossier["confidence"] == 0
    assert dossier["verdict"] == "UNAVAILABLE"
    assert "business" in dossier["research_block_reason"]


def test_ipo_research_api_accepts_uploaded_document_text(temp_config) -> None:
    client = TestClient(create_app(temp_config))

    response = client.post(
        "/api/v1/ipos/research",
        json={
            "issuer": "Acme Limited",
            "source_url": "https://www.sebi.gov.in/acme.pdf",
            "document_text": "Our Business\nAcme makes industrial sensors.\nRisk Factors\nCustomer concentration.",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["issuer"] == "Acme Limited"


def test_daily_analysis_publishes_dossier_stages_and_an_unavailable_subscription(temp_config, monkeypatch) -> None:
    service = IpoResearchService(temp_config)
    monkeypatch.setattr(service, "list_issues", lambda limit: [{"issuer": "Acme Limited", "document_type": "RHP", "source_url": "https://www.sebi.gov.in/acme.pdf"}])
    monkeypatch.setattr(service, "_current_nse_issues", lambda: [])
    monkeypatch.setattr(service, "research", lambda *args: {"generated_at": "2026-08-25T00:00:00Z", "coverage": {"financials": True, "risks": True}, "verdict": "REVIEW", "investment_case": "Review official evidence.", "risks": ["Customer concentration."]})

    payload = service.daily_analysis()

    assert payload["data_status"] == "available"
    assert payload["issues"][0]["status"] == "unconfirmed"
    assert payload["issues"][0]["subscription"]["status"] == "unavailable"
    assert {stage["name"] for stage in payload["issues"][0]["pipeline_stages"]} >= {"fundamentals", "risk_analytics", "subscription_tracking"}


def test_daily_analysis_uses_nse_subscription_for_a_running_issue(temp_config, monkeypatch) -> None:
    service = IpoResearchService(temp_config)
    monkeypatch.setattr(service, "list_issues", lambda limit: [])
    monkeypatch.setattr(service, "_current_nse_issues", lambda: [{"companyName": "Acme Limited", "symbol": "ACME", "status": "Active", "issueStartDate": "25-Aug-2026", "issueEndDate": "28-Aug-2026", "noOfTime": "2.5"}])

    payload = service.daily_analysis()

    assert payload["issues"][0]["status"] == "running"
    assert payload["issues"][0]["subscription"]["total"] == "2.5"


def test_daily_analysis_prioritizes_the_nse_prospectus_for_a_running_issue(temp_config, monkeypatch) -> None:
    service = IpoResearchService(temp_config)
    monkeypatch.setattr(service, "list_issues", lambda limit: [])
    monkeypatch.setattr(service, "_current_nse_issues", lambda: [{"companyName": "Acme Limited", "symbol": "ACME", "status": "Active", "noOfTime": "2.5"}])
    monkeypatch.setattr(service, "_nse_offer_document", lambda symbol: "https://nsearchives.nseindia.com/content/ipo/RHP_ACME.zip")
    monkeypatch.setattr(service, "research", lambda issuer, source_url: {"generated_at": "2026-08-25T00:00:00Z", "coverage": {}, "verdict": "REVIEW", "investment_case": "Official document analysed.", "risks": []})

    payload = service.daily_analysis()

    assert payload["issues"][0]["source_url"].endswith("RHP_ACME.zip")
    assert payload["issues"][0]["dossier"]["investment_case"] == "Official document analysed."


def test_nse_issue_terms_are_source_linked(temp_config, monkeypatch) -> None:
    service = IpoResearchService(temp_config)
    monkeypatch.setattr(service, "_bse_issue_terms", lambda _: {})

    terms = service._issue_terms({"issuePrice": "Rs. 95 - Rs. 100", "_nse_details": {"source_url": "https://www.nseindia.com/market-data/all-upcoming-issues-ipo", "values": {"issue size": "1,00,000 shares", "bid lot": "150"}}})

    assert terms["price_band"]["value"] == "Rs. 95 - Rs. 100"
    assert terms["issue_size"]["value"] == "1,00,000 shares"
    assert terms["lot_size"]["value"] == "150"
    assert terms["lot_size"]["source_url"].startswith("https://www.nseindia.com/")


def test_bse_terms_fill_only_missing_nse_values(temp_config, monkeypatch) -> None:
    service = IpoResearchService(temp_config)
    monkeypatch.setattr(service, "_bse_issue_terms", lambda _: {"source_url": "https://www.bseindia.com/markets/publicIssues/DisplayIPO.aspx", "issue_size": "2,00,000 shares", "lot_size": "100"})

    terms = service._issue_terms({"issuePrice": "Rs. 95 - Rs. 100", "bse_issue_url": "https://www.bseindia.com/markets/publicIssues/DisplayIPO.aspx", "_nse_details": {"values": {}}})

    assert terms["price_band"]["value"] == "Rs. 95 - Rs. 100"
    assert terms["issue_size"]["source_url"].startswith("https://www.bseindia.com/")


def test_unreadable_offer_document_is_published_as_unavailable(temp_config, monkeypatch) -> None:
    service = IpoResearchService(temp_config)
    monkeypatch.setattr(service, "list_issues", lambda limit: [{"issuer": "Acme Limited", "document_type": "RHP", "source_url": "https://example.test/acme.pdf"}])
    monkeypatch.setattr(service, "_current_nse_issues", lambda: [])
    monkeypatch.setattr(service, "research", lambda *_args: (_ for _ in ()).throw(IpoResearchError("no readable text")))

    issue = service.daily_analysis()["issues"][0]

    assert issue["dossier"]["research_status"] == "unavailable"
    assert issue["dossier"]["confidence"] == 0
    assert {stage["status"] for stage in issue["pipeline_stages"] if stage["name"] in {"document_fetch", "decision"}} == {"unavailable"}


def test_gmp_requires_two_fresh_same_day_sources_and_does_not_change_research(temp_config) -> None:
    from src.workspace.service import WorkspaceRepository

    repo = WorkspaceRepository(temp_config)
    reviewer = repo.user_for_email("admin@example.com")
    observed_at = datetime.now(timezone.utc).isoformat()
    repo.add_gmp_observation("acme", "investorgain", 12, "https://www.investorgain.com/acme", observed_at, reviewer["id"])
    assert IpoResearchService(temp_config)._gmp_range("acme") is None
    repo.add_gmp_observation("acme", "zoom_news", 18, "https://www.zoomnews.in/acme", observed_at, reviewer["id"])

    gmp_range = IpoResearchService(temp_config)._gmp_range("acme")

    assert gmp_range and (gmp_range["low"], gmp_range["high"]) == (12.0, 18.0)
    assert "confidence" not in gmp_range

    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    repo.add_gmp_observation("stale", "investorgain", 12, "https://www.investorgain.com/stale", stale, reviewer["id"])
    repo.add_gmp_observation("stale", "zoom_news", 18, "https://www.zoomnews.in/stale", stale, reviewer["id"])
    assert IpoResearchService(temp_config)._gmp_range("stale") is None
