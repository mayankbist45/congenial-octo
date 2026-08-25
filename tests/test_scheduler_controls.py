from __future__ import annotations

from fastapi.testclient import TestClient

from run_daily import main
from src.dashboard.app import create_app
from src.dashboard.service import DashboardService
from src.scheduler_controls import CONTROLS, SchedulerControls


class AdminAuth:
    def identity(self):
        return {"id": 1, "email": "admin@example.com", "role": "admin"}


class MemberAuth:
    def identity(self):
        return {"id": 2, "email": "member@example.com", "role": "member"}


def test_controls_default_enabled_and_persist_updates(temp_config) -> None:
    controls = SchedulerControls(temp_config)

    assert {row["key"] for row in controls.all()} == set(CONTROLS)
    assert all(row["enabled"] for row in controls.all())

    saved = controls.set("us_intraday", False, "admin@example.com")

    assert saved["enabled"] is False
    assert saved["updated_by"] == "admin@example.com"
    assert SchedulerControls(temp_config).enabled("us_intraday") is False


def test_operations_api_updates_controls_for_admin_only(temp_config) -> None:
    member = TestClient(create_app(temp_config, workspace_auth=MemberAuth()))
    assert member.put("/api/v1/admin/operations/schedule-controls/us_intraday", json={"enabled": False}).status_code == 403

    admin = TestClient(create_app(temp_config, workspace_auth=AdminAuth()))
    response = admin.put("/api/v1/admin/operations/schedule-controls/us_intraday", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    controls = {row["key"]: row for row in admin.get("/api/v1/admin/operations").json()["controls"]}
    assert controls["us_intraday"]["enabled"] is False
    assert admin.put("/api/v1/admin/operations/schedule-controls/unknown", json={"enabled": False}).status_code == 404


def test_disabled_controls_are_not_reported_late(temp_config) -> None:
    SchedulerControls(temp_config).set("india_intraday", False, "admin@example.com")
    service = DashboardService(temp_config)

    cards = {card["key"]: card for card in service.operations_overview()["cards"]}

    assert cards["intraday-india_equity"]["current_state"] == "disabled"


def test_all_disabled_scheduled_long_cycle_is_a_clean_skip(temp_config, monkeypatch) -> None:
    controls = SchedulerControls(temp_config)
    controls.set("india_long_cycle", False, "admin@example.com")
    controls.set("us_long_cycle", False, "admin@example.com")
    monkeypatch.setattr("run_daily.get_config", lambda: temp_config)
    monkeypatch.setenv("FUND_SCHEDULED_RUN", "1")

    assert main(["long_cycle"]) == 0
