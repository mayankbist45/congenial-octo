from __future__ import annotations

from fastapi.testclient import TestClient

from src.dashboard.app import create_app
from src.ipo.service import IpoResearchService


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
