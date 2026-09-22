from __future__ import annotations

import pytest

from src.dashboard.app import create_app
from src.journal.db import connect
from src.workspace.entitlements import EntitlementsService, QuotaExceeded
from src.workspace.tiers import UNPAID_MAX_BOOKS
from src.workspace.service import WorkspaceRepository
from fastapi.testclient import TestClient


class RepositoryAuth:
    def __init__(self, repository, email: str):
        self.repository = repository
        self.email = email

    def identity(self):
        return self.repository.user_for_email(self.email)


def test_subscription_is_created_for_new_user(temp_config) -> None:
    repository = WorkspaceRepository(temp_config)
    user = repository.user_for_email("quota@example.com")
    entitlements = EntitlementsService(temp_config)
    sub = entitlements.subscription_for_user(int(user["id"]))
    assert sub.tier == "sandbox"
    assert sub.seat_limit == 1
    assert entitlements.can_access(sub)


def test_watchlist_book_quota_enforced(temp_config) -> None:
    repository = WorkspaceRepository(temp_config)
    user = repository.user_for_email("books@example.com")
    user_id = int(user["id"])
    entitlements = EntitlementsService(temp_config)
    limits = entitlements.limits(entitlements.subscription_for_user(user_id))
    for idx in range(UNPAID_MAX_BOOKS):
        entitlements.check_quota(user_id, "watchlist_books")
        conn = connect(temp_config)
        try:
            conn.execute(
                "INSERT INTO user_books (id, user_id, name, market_id, currency, status, starting_capital, allocation_mode, created_at, updated_at) "
                "VALUES (?, ?, ?, 'india_equity', 'INR', 'tracking', 100000, 'amount', '2026-01-01', '2026-01-01')",
                (f"book-{idx}", user_id, f"Book {idx}"),
            )
            conn.commit()
        finally:
            conn.close()
    with pytest.raises(QuotaExceeded) as exc:
        entitlements.check_quota(user_id, "watchlist_books")
    assert exc.value.quota == "watchlist_books"


def test_legacy_active_paper_books_do_not_count_toward_watchlist_quota(temp_config) -> None:
    repository = WorkspaceRepository(temp_config)
    user = repository.user_for_email("legacy-books@example.com")
    user_id = int(user["id"])
    entitlements = EntitlementsService(temp_config)
    limits = entitlements.limits(entitlements.subscription_for_user(user_id))
    conn = connect(temp_config)
    try:
        for idx in range(UNPAID_MAX_BOOKS):
            conn.execute(
                "INSERT INTO user_books (id, user_id, name, market_id, currency, status, starting_capital, allocation_mode, created_at, updated_at) "
                "VALUES (?, ?, ?, 'india_equity', 'INR', 'tracking', 100000, 'amount', '2026-01-01', '2026-01-01')",
                (f"track-{idx}", user_id, f"Track {idx}"),
            )
        conn.execute(
            "INSERT INTO user_books (id, user_id, name, market_id, currency, status, starting_capital, allocation_mode, created_at, updated_at) "
            "VALUES ('legacy-paper', ?, 'Legacy paper', 'india_equity', 'INR', 'active_paper', 100000, 'amount', '2026-01-01', '2026-01-01')",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(QuotaExceeded):
        entitlements.check_quota(user_id, "watchlist_books")
    assert entitlements.count_active_books(user_id) == UNPAID_MAX_BOOKS


def test_paid_users_have_unlimited_watchlists(temp_config) -> None:
    from datetime import datetime, timedelta, timezone

    repository = WorkspaceRepository(temp_config)
    user = repository.user_for_email("paid-books@example.com")
    user_id = int(user["id"])
    entitlements = EntitlementsService(temp_config)
    paid_through = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    entitlements.update_tier(
        user_id,
        tier="desk",
        billing_subscription_status="active",
        billing_paid_through_at=paid_through,
    )
    sub = entitlements.subscription_for_user(user_id)
    assert entitlements.watchlist_book_limit(sub) is None
    conn = connect(temp_config)
    try:
        for idx in range(6):
            conn.execute(
                "INSERT INTO user_books (id, user_id, name, market_id, currency, status, starting_capital, allocation_mode, created_at, updated_at) "
                "VALUES (?, ?, ?, 'india_equity', 'INR', 'tracking', 100000, 'amount', '2026-01-01', '2026-01-01')",
                (f"paid-book-{idx}", user_id, f"Paid book {idx}"),
            )
        conn.commit()
    finally:
        conn.close()
    entitlements.check_quota(user_id, "watchlist_books")
    payload = entitlements.usage_payload(sub, user_id)
    assert payload["watchlist_books"]["used"] == 6
    assert payload["watchlist_books"]["limit"] is None


def test_workspace_me_includes_subscription_payload(temp_config) -> None:
    repository = WorkspaceRepository(temp_config)
    repository.user_for_email("member@example.com")
    client = TestClient(create_app(temp_config, workspace_auth=RepositoryAuth(repository, "member@example.com")))
    payload = client.get("/api/v1/workspace/me").json()
    assert payload["subscription"]["tier"] == "sandbox"
    assert payload["limits"]["max_books"] == UNPAID_MAX_BOOKS
    assert "usage" in payload


def test_plans_payload_includes_enterprise() -> None:
    from src.workspace.tiers import plans_payload

    tiers = {row["tier"] for row in plans_payload()}
    assert "enterprise" in tiers
    enterprise = next(row for row in plans_payload() if row["tier"] == "enterprise")
    assert enterprise["price_inr"] == 299999
    assert enterprise["max_active_funds"] is None


def test_enterprise_users_have_unlimited_active_funds(temp_config) -> None:
    from datetime import datetime, timedelta, timezone

    repository = WorkspaceRepository(temp_config)
    user = repository.user_for_email("enterprise-funds@example.com")
    user_id = int(user["id"])
    entitlements = EntitlementsService(temp_config)
    paid_through = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    entitlements.update_tier(
        user_id,
        tier="enterprise",
        billing_subscription_status="active",
        billing_paid_through_at=paid_through,
    )
    sub = entitlements.subscription_for_user(user_id)
    assert entitlements.active_fund_limit(sub) is None
    for idx in range(6):
        entitlements.check_quota(user_id, "active_funds")
        conn = connect(temp_config)
        try:
            conn.execute(
                """
                INSERT INTO user_book_personas (
                    id, user_id, persona_id, template_persona_id, status,
                    frozen_allocation, pending_overrides, created_at
                ) VALUES (?, ?, ?, 'balanced', 'active', '{}', '{}', '2026-01-01')
                """,
                (f"persona-{idx}", user_id, f"fund-{idx}"),
            )
            conn.commit()
        finally:
            conn.close()
    payload = entitlements.usage_payload(sub, user_id)
    assert payload["active_funds"]["used"] == 6
    assert payload["active_funds"]["limit"] is None
