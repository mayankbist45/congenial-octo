from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from src.dashboard.app import create_app
from src.workspace.dashboard_snapshot import DashboardSnapshotService
from src.workspace.service import WorkspaceRepository


class FakeWorkspaceAuth:
    def identity(self):
        return {"id": 1, "email": "member@example.com", "role": "member"}


class FakeAdminAuth:
    def identity(self):
        return {"id": 1, "email": "admin@example.com", "role": "admin"}


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


def test_workspace_dashboard_returns_the_published_eod_snapshot(temp_config):
    client, _ = client_with_invite(temp_config)
    publisher = DashboardSnapshotService(temp_config)
    published = publisher.publish(date(2026, 8, 21))

    response = client.get("/api/v1/workspace/dashboard")
    assert response.status_code == 200
    assert response.json()["snapshot_date"] == "2026-08-21"
    assert response.json()["data_status"] == "delayed_eod"
    assert publisher.publish(date(2026, 8, 21))["as_of"] == published["as_of"]


def test_healthcheck_is_public(temp_config):
    assert TestClient(create_app(temp_config)).get("/healthz").json() == {"status": "ok"}


def test_admin_routes_require_admin_role(temp_config):
    member_client, _ = client_with_invite(temp_config)
    assert member_client.get("/api/v1/admin/overview").status_code == 403

    repository = WorkspaceRepository(temp_config)
    repository.create_invite("admin@example.com", role="admin")
    repository.user_for_email("admin@example.com")
    admin_client = TestClient(create_app(temp_config, workspace_auth=FakeAdminAuth()))
    assert admin_client.get("/api/v1/admin/overview").status_code == 200
    assert admin_client.get("/api/v1/admin/operations").status_code == 200
