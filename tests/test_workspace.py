from __future__ import annotations

from datetime import date
from datetime import datetime, timezone
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from src.config import get_persona_config
from src.dashboard.app import create_app
from src.journal.db import connect
from src.journal.repository import JournalRepository
from src.workspace.dashboard_snapshot import DashboardSnapshotService
from src.workspace.service import WorkspaceRepository


class FakeWorkspaceAuth:
    def identity(self):
        return {"id": 1, "email": "member@example.com", "role": "member"}


class FakeAdminAuth:
    def identity(self):
        return {"id": 1, "email": "admin@example.com", "role": "admin"}


class RepositoryAuth:
    def __init__(self, repository, email: str):
        self.repository = repository
        self.email = email

    def identity(self):
        return self.repository.user_for_email(self.email)


def client_with_member(temp_config):
    repository = WorkspaceRepository(temp_config)
    user = repository.user_for_email("member@example.com")
    assert user["id"] == 1
    return TestClient(create_app(temp_config, workspace_auth=FakeWorkspaceAuth())), repository


def test_workspace_profile_and_job_contract(temp_config, monkeypatch):
    monkeypatch.setattr("src.workspace.service.WorkspaceService.generate", lambda *_: None)
    client, _ = client_with_member(temp_config)

    response = client.get("/api/v1/workspace/profile")
    assert response.status_code == 200
    assert response.json()["profile"]["target_positions"] == 15

    response = client.put(
        "/api/v1/workspace/profile",
        json={"profile": {"target_positions": 12, "excluded_sectors": ["Metals"]}},
    )
    assert response.status_code == 403

    response = client.post("/api/v1/workspace/portfolios")
    assert response.status_code == 403


def test_workspace_portfolio_and_inbox_are_user_scoped(temp_config):
    client, repository = client_with_member(temp_config)
    portfolio_id = repository.save_portfolio(1, {"as_of": "2026-08-23T00:00:00Z", "holdings": [], "data_status": "delayed_eod"})
    event_id = repository.add_event(1, {"title": "Quarterly result needs review", "impact": "high"})

    assert client.get(f"/api/v1/workspace/portfolios/{portfolio_id}").status_code == 200
    inbox = client.get("/api/v1/workspace/inbox")
    assert inbox.status_code == 200
    assert inbox.json()["events"][0]["id"] == event_id
    assert client.post(f"/api/v1/workspace/events/{event_id}/acknowledge").status_code == 403


def test_workspace_routes_reject_requests_without_google_token(temp_config):
    response = TestClient(create_app(temp_config)).get("/api/v1/workspace/profile")
    assert response.status_code == 401


def test_workspace_accepts_local_vite_authorization_preflight(temp_config):
    response = TestClient(create_app(temp_config)).options(
        "/api/v1/workspace/me",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_verified_google_account_is_provisioned_as_member_and_admin_is_preserved(temp_config):
    repository = WorkspaceRepository(temp_config)
    member = repository.user_for_email("new.user@example.com", "New User", "https://example.com/avatar.png")
    assert member["role"] == "member"
    assert member["display_name"] == "New User"
    assert member["avatar_url"] == "https://example.com/avatar.png"

    repository.create_invite("admin@example.com", role="admin")
    admin = repository.user_for_email("admin@example.com")
    assert admin["role"] == "admin"
    # A subsequent non-admin sign-in must never demote an existing administrator.
    assert repository.user_for_email("admin@example.com")["role"] == "admin"


def test_workspace_me_returns_account_essentials(temp_config):
    client, _ = client_with_member(temp_config)
    response = client.get("/api/v1/workspace/me")
    assert response.status_code == 200
    assert response.json()["user"]["email"] == "member@example.com"
    assert response.json()["user"]["role"] == "member"


def test_expired_members_are_redirected_to_subscription_at_the_api_boundary(temp_config, monkeypatch):
    monkeypatch.setenv("WORKSPACE_BILLING_ENABLED", "true")
    monkeypatch.setenv("WORKSPACE_BILLING_LAUNCH_AT", "2026-01-01T00:00:00+00:00")
    monkeypatch.setenv("WORKSPACE_TRIAL_DAYS", "1")
    monkeypatch.setenv("RAZORPAY_PLAN_ID", "plan_test")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "key_test")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret_test")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "webhook_test")
    repository = WorkspaceRepository(temp_config)
    user = repository.user_for_email("expired@example.com")
    conn = connect(temp_config)
    try:
        conn.execute("UPDATE workspace_users SET created_at = ? WHERE id = ?", ("2025-01-01T00:00:00+00:00", user["id"]))
        conn.commit()
    finally:
        conn.close()
    client = TestClient(create_app(temp_config, workspace_auth=RepositoryAuth(repository, "expired@example.com")))

    me = client.get("/api/v1/workspace/me")
    assert me.status_code == 200
    assert me.json()["user"]["entitlement"]["state"] == "expired"
    assert client.get("/api/v1/workspace/dashboard").status_code == 403


def test_razorpay_webhook_is_verified_and_idempotent(temp_config, monkeypatch):
    monkeypatch.setenv("WORKSPACE_BILLING_ENABLED", "true")
    monkeypatch.setenv("WORKSPACE_BILLING_LAUNCH_AT", "2026-01-01T00:00:00+00:00")
    monkeypatch.setenv("RAZORPAY_PLAN_ID", "plan_test")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "key_test")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret_test")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "webhook_test")
    repository = WorkspaceRepository(temp_config)
    user = repository.user_for_email("paid@example.com")
    conn = connect(temp_config)
    try:
        conn.execute("UPDATE workspace_users SET billing_subscription_id = ? WHERE id = ?", ("sub_test", user["id"]))
        conn.commit()
    finally:
        conn.close()
    payload = {"payload": {"subscription": {"entity": {"id": "sub_test", "status": "active", "current_end": 1900000000}}}}
    body = json.dumps(payload).encode()
    signature = hmac.new(b"webhook_test", body, hashlib.sha256).hexdigest()
    client = TestClient(create_app(temp_config, workspace_auth=RepositoryAuth(repository, "paid@example.com")))

    assert client.post("/api/v1/workspace/billing/webhook", content=body, headers={"X-Razorpay-Signature": signature}).status_code == 200
    assert client.post("/api/v1/workspace/billing/webhook", content=body, headers={"X-Razorpay-Signature": signature}).status_code == 200
    assert client.get("/api/v1/workspace/me").json()["user"]["entitlement"]["state"] == "active"


def test_razorpay_subscription_checkout_verifies_the_payment_signature(temp_config, monkeypatch):
    monkeypatch.setenv("WORKSPACE_BILLING_ENABLED", "true")
    monkeypatch.setenv("WORKSPACE_BILLING_LAUNCH_AT", "2026-01-01T00:00:00+00:00")
    monkeypatch.setenv("RAZORPAY_PLAN_ID", "plan_test")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "key_test")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret_test")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "webhook_test")
    monkeypatch.setattr("src.workspace.billing.WorkspaceBilling._razorpay", lambda *_: {"id": "sub_test", "status": "created"})
    repository = WorkspaceRepository(temp_config)
    repository.user_for_email("checkout@example.com")
    client = TestClient(create_app(temp_config, workspace_auth=RepositoryAuth(repository, "checkout@example.com")))

    checkout = client.post("/api/v1/workspace/billing/checkout")
    assert checkout.status_code == 200
    assert checkout.json()["subscription_id"] == "sub_test"
    payment_id = "pay_test"
    signature = hmac.new(b"secret_test", b"pay_test|sub_test", hashlib.sha256).hexdigest()
    assert client.post("/api/v1/workspace/billing/verify-payment", json={"razorpay_payment_id": payment_id, "razorpay_subscription_id": "sub_test", "razorpay_signature": signature}).json() == {"verified": True}
    assert client.post("/api/v1/workspace/billing/verify-payment", json={"razorpay_payment_id": payment_id, "razorpay_subscription_id": "sub_test", "razorpay_signature": "wrong"}).status_code == 400


def test_workspace_dashboard_returns_the_published_eod_snapshot(temp_config):
    client, _ = client_with_member(temp_config)
    publisher = DashboardSnapshotService(temp_config)
    day = date.today()
    published = publisher.publish(day)

    response = client.get("/api/v1/workspace/dashboard")
    assert response.status_code == 200
    assert response.json()["snapshot_date"] == day.isoformat()
    assert response.json()["data_status"] == "delayed_eod"
    assert publisher.publish(day)["as_of"] == published["as_of"]


def test_workspace_ipo_routes_return_published_research(temp_config):
    client, _ = client_with_member(temp_config)
    DashboardSnapshotService(temp_config).publish(date(2026, 8, 21), ipos={"issues": [{"id": "acme-1", "issuer": "Acme Limited"}], "warnings": [], "data_status": "available"})

    assert client.get("/api/v1/workspace/ipos").json()["ipos"]["issues"][0]["id"] == "acme-1"
    assert client.get("/api/v1/workspace/ipos/acme-1").json()["ipo"]["issuer"] == "Acme Limited"
    assert client.get("/api/v1/workspace/ipos/missing").status_code == 404


def test_workspace_ipo_refresh_preserves_the_daily_snapshot(temp_config):
    publisher = DashboardSnapshotService(temp_config)
    day = date.today()
    published = publisher.publish(day, ipos={"issues": [{"id": "old"}]})

    refreshed = publisher.publish(day, ipos={"issues": [{"id": "new"}]}, refresh_ipos=True)

    assert refreshed["as_of"] == published["as_of"]
    assert refreshed["ipos"]["issues"][0]["id"] == "new"


def test_workspace_portfolio_refresh_preserves_the_daily_ipo_research(temp_config):
    publisher = DashboardSnapshotService(temp_config)
    day = date.today()
    publisher.publish(day, ipos={"issues": [{"id": "acme"}]})

    refreshed = publisher.refresh_portfolio(day)

    assert refreshed["ipos"]["issues"][0]["id"] == "acme"


def test_workspace_persona_contracts_are_member_read_only(temp_config):
    client, _ = client_with_member(temp_config)
    published = DashboardSnapshotService(temp_config).publish(date(2026, 8, 21))
    persona_id = published["personas"][0]["id"]

    personas = client.get("/api/v1/workspace/personas")
    assert personas.status_code == 200
    assert personas.json()["personas"][0]["id"] == persona_id
    for suffix in ("", "/holdings", "/performance", "/risk", "/news", "/runs"):
        assert client.get(f"/api/v1/workspace/personas/{persona_id}{suffix}").status_code == 200
    assert client.get("/api/v1/workspace/news").status_code == 200
    assert client.get("/api/v1/workspace/operations").status_code == 403
    assert client.post("/api/v1/admin/proposals/1/approve").status_code == 403


def test_workspace_persona_run_detail_is_scoped_and_preserves_snapshot_metadata(temp_config):
    client, _ = client_with_member(temp_config)
    published = DashboardSnapshotService(temp_config).publish(date(2026, 8, 21))
    persona_id = published["personas"][0]["id"]
    repo = JournalRepository(get_persona_config(persona_id, base=temp_config))
    run_id = repo.start_run(date(2026, 8, 21), False, 1_000_000.0, 100_000.0, command="daily_monitor")
    repo.record_run_stage(
        run_id,
        "candidate_pack_load",
        "warning",
        0,
        {"summary": "Weekly candidate pack was stale.", "input_count": 1, "output_count": 0, "stale": True},
    )
    repo.finish_run(run_id, "No picks.", True, 0.0, stage_status={"outcome_summary": "No picks: stale candidate pack."})

    response = client.get(f"/api/v1/workspace/personas/{persona_id}/runs/{run_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["persona_id"] == persona_id
    assert payload["snapshot_date"] == "2026-08-21"
    assert payload["run"]["id"] == run_id
    assert payload["run"]["stage_events"][0]["stage_name"] == "candidate_pack_load"
    assert payload["recent_runs"][0]["id"] == run_id
    assert client.get(f"/api/v1/workspace/personas/{persona_id}").json()["persona"]["runs"][0]["id"] == run_id

    other_persona = next(row["id"] for row in published["personas"] if row["id"] != persona_id)
    assert client.get(f"/api/v1/workspace/personas/{other_persona}/runs/{run_id}").status_code == 404


def test_legacy_snapshot_is_upgraded_with_personas_once(temp_config):
    publisher = DashboardSnapshotService(temp_config)
    day = date(2026, 8, 21).isoformat()
    conn = connect(temp_config)
    try:
        conn.execute(
            "INSERT INTO dashboard_snapshots (snapshot_date, as_of, payload, created_at) VALUES (?, ?, ?, ?)",
            (day, "2026-08-21T18:15:00+00:00", '{"snapshot_date":"2026-08-21","as_of":"2026-08-21T18:15:00+00:00"}', "2026-08-21T18:15:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    upgraded = publisher.publish(date(2026, 8, 21))
    assert upgraded["as_of"] == "2026-08-21T18:15:00+00:00"
    assert "personas" in upgraded


def test_healthcheck_is_public(temp_config):
    assert TestClient(create_app(temp_config)).get("/healthz").json() == {"status": "ok"}


def test_admin_routes_require_admin_role(temp_config):
    member_client, _ = client_with_member(temp_config)
    assert member_client.get("/api/v1/admin/overview").status_code == 403

    repository = WorkspaceRepository(temp_config)
    repository.create_invite("admin@example.com", role="admin")
    repository.user_for_email("admin@example.com")
    admin_client = TestClient(create_app(temp_config, workspace_auth=FakeAdminAuth()))
    assert admin_client.get("/api/v1/admin/overview").status_code == 200
    assert admin_client.get("/api/v1/admin/operations").status_code == 200


def test_gmp_observations_are_admin_only_and_append_only(temp_config):
    member_client, _ = client_with_member(temp_config)
    DashboardSnapshotService(temp_config).publish(date.today(), ipos={"issues": [{"id": "acme"}]})
    payload = {"source_name": "investorgain", "gmp_value": 10, "source_url": "https://www.investorgain.com/acme", "observed_at": datetime.now(timezone.utc).isoformat()}
    assert member_client.post("/api/v1/admin/ipos/acme/gmp-observations", json=payload).status_code == 403

    repository = WorkspaceRepository(temp_config)
    repository.create_invite("admin@example.com", role="admin")
    repository.user_for_email("admin@example.com")
    admin_client = TestClient(create_app(temp_config, workspace_auth=FakeAdminAuth()))
    response = admin_client.post("/api/v1/admin/ipos/acme/gmp-observations", json=payload)

    assert response.status_code == 201
    assert response.json()["observation"]["reviewer_user_id"] == 1
