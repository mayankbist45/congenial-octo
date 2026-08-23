from __future__ import annotations

from fastapi.testclient import TestClient

from src.dashboard.app import create_app
from src.workspace.service import WorkspaceRepository


class FakeWorkspaceAuth:
    def identity(self):
        return {"id": 1, "email": "member@example.com", "role": "member"}


def client_with_invite(temp_config):
    repository = WorkspaceRepository(temp_config)
    repository.create_invite("member@example.com")
    user = repository.user_for_email("member@example.com")
    assert user["id"] == 1
    return TestClient(create_app(temp_config, workspace_auth=FakeWorkspaceAuth())), repository


def test_workspace_profile_and_job_contract(temp_config, monkeypatch):
    monkeypatch.setattr("src.workspace.service.WorkspaceService.generate", lambda *_: None)
    client, _ = client_with_invite(temp_config)

    response = client.get("/api/v1/workspace/profile")
    assert response.status_code == 200
    assert response.json()["profile"]["target_positions"] == 15

    response = client.put(
        "/api/v1/workspace/profile",
        json={"profile": {"target_positions": 12, "excluded_sectors": ["Metals"]}},
    )
    assert response.status_code == 200
    assert response.json()["profile"]["target_positions"] == 12

    response = client.post("/api/v1/workspace/portfolios")
    assert response.status_code == 202
    job = response.json()["job"]
    assert job["status"] in {"queued", "running", "failed", "completed"}
    assert client.get(f"/api/v1/workspace/jobs/{job['id']}").status_code == 200


def test_workspace_portfolio_and_inbox_are_user_scoped(temp_config):
    client, repository = client_with_invite(temp_config)
    portfolio_id = repository.save_portfolio(1, {"as_of": "2026-08-23T00:00:00Z", "holdings": [], "data_status": "delayed_eod"})
    event_id = repository.add_event(1, {"title": "Quarterly result needs review", "impact": "high"})

    assert client.get(f"/api/v1/workspace/portfolios/{portfolio_id}").status_code == 200
    inbox = client.get("/api/v1/workspace/inbox")
    assert inbox.status_code == 200
    assert inbox.json()["events"][0]["id"] == event_id
    assert client.post(f"/api/v1/workspace/events/{event_id}/acknowledge").json()["status"] == "acknowledged"


def test_workspace_routes_reject_requests_without_google_token(temp_config):
    response = TestClient(create_app(temp_config)).get("/api/v1/workspace/profile")
    assert response.status_code == 401
