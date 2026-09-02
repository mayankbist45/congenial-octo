from __future__ import annotations

from fastapi.testclient import TestClient

from src.dashboard.app import create_app
from src.workspace.service import WorkspaceRepository


class WorkspaceAuth:
    def __init__(self, user_id: int, role: str):
        self._identity = {"id": user_id, "email": f"{role}@example.com", "role": role}

    def identity(self):
        return self._identity


def client_for(temp_config, role: str):
    repo = WorkspaceRepository(temp_config)
    repo.create_invite(f"{role}@example.com", role=role)
    user = repo.user_for_email(f"{role}@example.com")
    return TestClient(create_app(temp_config, workspace_auth=WorkspaceAuth(user["id"], role))), repo


def test_pm_case_run_comment_and_finalize_are_auditable(temp_config):
    client, _ = client_for(temp_config, "pm")
    created = client.post("/api/v1/workspace/cases", json={
        "title": "Nvidia earnings review",
        "brief": "Assess the earnings setup using verified sources and record the key risks.",
        "scope": {"symbols": ["nvda"], "market": "US equities"},
    })
    assert created.status_code == 201
    case_id = created.json()["case"]["id"]

    run = client.post(
        f"/api/v1/workspace/cases/{case_id}/runs",
        json={"agent_type": "company_deep_dive", "brief": "Review the earnings setup and key risks.", "scope": {"symbols": ["NVDA"]}},
        headers={"Idempotency-Key": "nvidia-earnings-1"},
    )
    assert run.status_code == 202
    assert run.json()["run"]["agent_version"] == "2"
    duplicate = client.post(
        f"/api/v1/workspace/cases/{case_id}/runs",
        json={"agent_type": "company_deep_dive", "brief": "Review the earnings setup and key risks.", "scope": {"symbols": ["NVDA"]}},
        headers={"Idempotency-Key": "nvidia-earnings-1"},
    )
    assert duplicate.json()["run"]["id"] == run.json()["run"]["id"]

    assert client.post(f"/api/v1/workspace/cases/{case_id}/comments", json={"body": "Use primary filings before finalizing."}).status_code == 201
    finalized = client.post(f"/api/v1/workspace/cases/{case_id}/finalize", json={"assessment": "investigate", "note": "Wait for verified filing coverage."})
    assert finalized.status_code == 200
    case = client.get(f"/api/v1/workspace/cases/{case_id}").json()["case"]
    assert case["final_assessment"] == "investigate"
    assert case["runs"][0]["status"] == "queued"
    assert case["runs"][0]["events"]


def test_any_workspace_member_can_start_and_finalize_work(temp_config):
    pm, _ = client_for(temp_config, "pm")
    case_id = pm.post("/api/v1/workspace/cases", json={"title": "Risk review", "brief": "Review the current portfolio risk context.", "scope": {}}).json()["case"]["id"]
    member, _ = client_for(temp_config, "member")

    assert member.get(f"/api/v1/workspace/cases/{case_id}").status_code == 200
    assert member.post(f"/api/v1/workspace/cases/{case_id}/comments", json={"body": "Please include concentration context."}).status_code == 201
    run = member.post(f"/api/v1/workspace/cases/{case_id}/runs", json={"agent_type": "event_impact", "brief": "Assess the event impact.", "scope": {}})
    assert run.status_code == 202
    assert member.post(f"/api/v1/workspace/cases/{case_id}/finalize", json={"assessment": "monitor", "note": "Team decision."}).status_code == 200
