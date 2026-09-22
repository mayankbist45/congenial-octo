from __future__ import annotations

from fastapi.testclient import TestClient

from src.dashboard.app import create_app
from src.workspace.service import WorkspaceRepository
from src.workspace.user_books import UserBookService
from tests.test_workspace import FakeWorkspaceAuth


class FakePriceService:
    def __init__(self, prices=None):
        self.prices = prices or {"RELIANCE": 2500.0, "TCS": 3500.0, "INFY": 1800.0}

    def get_last_price(self, symbol: str):
        return self.prices.get(symbol.upper())

    def get_last_prices(self, symbols):
        return {symbol: self.get_last_price(symbol) for symbol in symbols if self.get_last_price(symbol) is not None}


def user_book_client(temp_config, monkeypatch):
    repository = WorkspaceRepository(temp_config)
    repository.user_for_email("member@example.com")
    fake_prices = FakePriceService()
    monkeypatch.setattr("src.workspace.user_books.PriceService", lambda config=None: fake_prices)
    app = create_app(temp_config, workspace_auth=FakeWorkspaceAuth())
    return TestClient(app), UserBookService(temp_config, prices=fake_prices)


def test_create_list_and_get_user_book(temp_config, monkeypatch):
    client, _ = user_book_client(temp_config, monkeypatch)

    created = client.post(
        "/api/v1/workspace/user-books",
        json={"name": "Tech picks", "market_id": "india_equity", "starting_capital": 1_000_000},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["book"]["name"] == "Tech picks"
    assert body["book"]["currency"] == "INR"
    assert body["summary"]["unallocated_cash"] == 1_000_000
    assert "Paper tracker" in body["disclaimer"]
    assert body["data_status"] == "delayed_eod"
    book_id = body["book"]["id"]

    listing = client.get("/api/v1/workspace/user-books")
    assert listing.status_code == 200
    assert len(listing.json()["books"]) == 1

    detail = client.get(f"/api/v1/workspace/user-books/{book_id}")
    assert detail.status_code == 200
    assert detail.json()["book"]["id"] == book_id
    assert detail.json()["lines"] == []


def test_add_update_remove_line_and_valuation(temp_config, monkeypatch):
    client, service = user_book_client(temp_config, monkeypatch)
    book = service.create_book(1, name="Core", market_id="india_equity", starting_capital=500_000)
    book_id = book["book"]["id"]

    added = client.post(
        f"/api/v1/workspace/user-books/{book_id}/lines",
        json={"symbol": "RELIANCE", "target_amount": 200_000},
    )
    assert added.status_code == 201
    line = added.json()["line"]
    assert line["symbol"] == "RELIANCE"
    assert line["entry_price"] == 2500.0
    assert line["weight_pct"] == 100.0
    assert line["return_since_added_pct"] == 0.0

    updated = client.patch(
        f"/api/v1/workspace/user-books/{book_id}/lines/RELIANCE",
        json={"target_amount": 250_000},
    )
    assert updated.status_code == 200
    assert updated.json()["line"]["target_amount"] == 250_000

    service._prices.prices["RELIANCE"] = 2750.0
    detail = client.get(f"/api/v1/workspace/user-books/{book_id}")
    assert detail.status_code == 200
    reliance = detail.json()["lines"][0]
    assert reliance["last_price"] == 2750.0
    assert reliance["return_since_added_pct"] == 10.0
    assert reliance["current_value"] == 275_000.0
    assert detail.json()["summary"]["invested_amount"] == 250_000
    assert detail.json()["summary"]["unallocated_cash"] == 250_000

    removed = client.delete(f"/api/v1/workspace/user-books/{book_id}/lines/RELIANCE")
    assert removed.status_code == 200
    assert client.get(f"/api/v1/workspace/user-books/{book_id}/lines").json()["lines"] == []


def test_allocation_cannot_exceed_starting_capital(temp_config, monkeypatch):
    client, service = user_book_client(temp_config, monkeypatch)
    book_id = service.create_book(1, name="Tight", market_id="india_equity", starting_capital=100_000)["book"]["id"]

    first = client.post(
        f"/api/v1/workspace/user-books/{book_id}/lines",
        json={"symbol": "RELIANCE", "target_amount": 60_000},
    )
    assert first.status_code == 201

    second = client.post(
        f"/api/v1/workspace/user-books/{book_id}/lines",
        json={"symbol": "TCS", "target_amount": 50_000},
    )
    assert second.status_code == 400
    assert "exceeds starting capital" in second.json()["detail"]


def test_symbol_search_and_validation(temp_config, monkeypatch):
    client, service = user_book_client(temp_config, monkeypatch)
    book_id = service.create_book(1, name="Search", market_id="india_equity", starting_capital=100_000)["book"]["id"]

    search = client.get("/api/v1/workspace/user-books/symbols", params={"market_id": "india_equity", "q": "REL"})
    assert search.status_code == 200
    symbols = {item["symbol"] for item in search.json()["symbols"]}
    assert "RELIANCE" in symbols

    invalid = client.post(
        f"/api/v1/workspace/user-books/{book_id}/lines",
        json={"symbol": "NOTAREALSYMBOL", "target_amount": 10_000},
    )
    assert invalid.status_code == 400


def test_user_books_are_user_scoped(temp_config, monkeypatch):
    client, service = user_book_client(temp_config, monkeypatch)
    book_id = service.create_book(1, name="Private", market_id="india_equity", starting_capital=100_000)["book"]["id"]

    other = WorkspaceRepository(temp_config)
    other_user = other.user_for_email("other@example.com")
    assert other_user["id"] != 1

    assert client.get(f"/api/v1/workspace/user-books/{book_id}").status_code == 200

    other_service = UserBookService(temp_config, prices=FakePriceService())
    try:
        other_service.get_book(other_user["id"], book_id)
        raised = False
    except Exception:
        raised = True
    assert raised


def test_archive_user_book(temp_config, monkeypatch):
    client, service = user_book_client(temp_config, monkeypatch)
    book_id = service.create_book(1, name="Archive me", market_id="india_equity", starting_capital=100_000)["book"]["id"]

    archived = client.delete(f"/api/v1/workspace/user-books/{book_id}")
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"

    assert client.get(f"/api/v1/workspace/user-books/{book_id}").status_code == 404
    assert client.get("/api/v1/workspace/user-books").json()["books"] == []


def test_performance_endpoint_returns_series(temp_config, monkeypatch):
    client, service = user_book_client(temp_config, monkeypatch)
    book_id = service.create_book(1, name="Perf", market_id="india_equity", starting_capital=200_000)["book"]["id"]
    service.add_line(1, book_id, symbol="TCS", target_amount=100_000)

    response = client.get(f"/api/v1/workspace/user-books/{book_id}/performance")
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["invested_amount"] == 100_000
    assert len(payload["series"]) >= 1


def test_record_all_snapshots(temp_config):
    WorkspaceRepository(temp_config).user_for_email("member@example.com")
    service = UserBookService(temp_config, prices=FakePriceService())
    book_id = service.create_book(1, name="Snap", market_id="india_equity", starting_capital=100_000)["book"]["id"]
    service.add_line(1, book_id, symbol="INFY", target_amount=50_000)

    count = service.record_all_snapshots("2026-08-27")
    assert count == 1
    series = service.repo.list_snapshots(book_id)
    assert len(series) == 1
    assert series[0]["snapshot_date"] == "2026-08-27"


def test_my_watchlist_dev_page_and_api(temp_config, monkeypatch):
    from src.dashboard.app import create_app

    WorkspaceRepository(temp_config).user_for_email("dev-ui@example.com")
    monkeypatch.setattr("src.workspace.user_books.PriceService", lambda config=None: FakePriceService())
    client = TestClient(create_app(temp_config))

    page = client.get("/my-watchlist")
    assert page.status_code == 200
    assert "My Watchlist" in page.text
    assert "Pick stocks" in page.text
    assert "Watchlist name" in page.text

    universe = client.get("/my-watchlist/api/universe?user_id=1&market_id=india_equity")
    assert universe.status_code == 200
    body = universe.json()
    assert body["count"] > 100
    assert body["count"] <= 501
    assert all("NSETEST" not in item["symbol"] for item in body["symbols"])
    assert body["symbols"][0]["company_name"] != f"{body['symbols'][0]['symbol']}-EQ"

    search = client.get("/my-watchlist/api/universe?user_id=1&market_id=india_equity&q=TEMPSENS")
    assert search.status_code == 200
    hits = [item["symbol"] for item in search.json()["symbols"]]
    assert "TEMPSENS" in hits

    created = client.post(
        "/my-watchlist/api/books?user_id=1",
        json={"name": "UI book", "market_id": "india_equity", "starting_capital": 250000},
    )
    assert created.status_code == 201
    book_id = created.json()["book"]["id"]

    listed = client.get("/my-watchlist/api/books?user_id=1")
    assert listed.status_code == 200
    assert len(listed.json()["books"]) == 1

    added = client.post(
        f"/my-watchlist/api/books/{book_id}/lines?user_id=1",
        json={"symbol": "TCS", "target_amount": 100000},
    )
    assert added.status_code == 201

    detail = client.get(f"/my-watchlist/api/books/{book_id}?user_id=1")
    assert detail.status_code == 200
    assert detail.json()["lines"][0]["symbol"] == "TCS"


def test_activate_user_book_persona_and_config(temp_config, monkeypatch):
    from src.config import get_persona_config
    from src.universe import load_universe
    from src.workspace.user_book_personas import active_userbook_persona_ids, persona_universe_path

    monkeypatch.setattr("src.workspace.user_books.PriceService", lambda config=None: FakePriceService())
    service = UserBookService(temp_config, prices=FakePriceService())
    WorkspaceRepository(temp_config).user_for_email("member@example.com")

    book_id = service.create_book(1, name="Activate me", market_id="india_equity", starting_capital=500_000)["book"]["id"]
    service.add_line(1, book_id, symbol="RELIANCE", target_amount=200_000)
    service.add_line(1, book_id, symbol="TCS", target_amount=150_000)

    activated = service.activate_paper_fund(1, book_id, template_persona_id="balanced")
    persona_id = activated["persona_id"]
    assert persona_id.startswith("userbook_")
    assert activated["status"] == "active"
    assert activated["applies_on"] == "next_long_cycle"

    book = service.repo.get_book(book_id, 1)
    assert book["status"] == "tracking"

    persona = service.linked_persona(1, book_id)
    assert persona["persona"]["persona_id"] == persona_id

    config = get_persona_config(persona_id, base=temp_config)
    assert config.get("execution.auto_approve") is True
    assert config.secrets.dry_run is True
    assert config.get("universe.mode") != "fixed"
    universe = load_universe(config)
    assert len(universe.symbols) > 2
    assert "RELIANCE" in universe.symbols
    assert config.get("universe.file") == persona_universe_path("india_equity")
    assert persona_id in active_userbook_persona_ids(temp_config)

    service.archive_book(1, book_id)
    assert persona_id not in active_userbook_persona_ids(temp_config)


def test_multiple_personas_from_same_watchlist(temp_config, monkeypatch):
    from src.workspace.entitlements import EntitlementsService

    monkeypatch.setattr("src.workspace.user_books.PriceService", lambda config=None: FakePriceService())
    service = UserBookService(temp_config, prices=FakePriceService())
    WorkspaceRepository(temp_config).user_for_email("member@example.com")
    EntitlementsService(temp_config).update_tier(1, tier="fund", live_trading_enabled=True)

    book_id = service.create_book(1, name="Multi seed", market_id="india_equity", starting_capital=500_000)["book"]["id"]
    service.add_line(1, book_id, symbol="RELIANCE", target_amount=200_000)

    first = service.activate_paper_fund(1, book_id, template_persona_id="balanced")
    second = service.activate_paper_fund(1, book_id, template_persona_id="steady_compounder")
    assert first["persona_id"] != second["persona_id"]
    assert service.linked_persona(1, book_id)["persona_id"] == second["persona_id"]


def test_user_persona_pending_overrides(temp_config, monkeypatch):
    from src.config import get_persona_config
    from src.workspace.user_book_personas import UserBookPersonaService

    monkeypatch.setattr("src.workspace.user_books.PriceService", lambda config=None: FakePriceService())
    service = UserBookService(temp_config, prices=FakePriceService())
    WorkspaceRepository(temp_config).user_for_email("member@example.com")
    book_id = service.create_book(1, name="Override", market_id="india_equity", starting_capital=500_000)["book"]["id"]
    service.add_line(1, book_id, symbol="RELIANCE", target_amount=200_000)
    persona_id = service.activate_paper_fund(1, book_id)["persona_id"]

    updated = UserBookPersonaService(temp_config).update_persona(
        1,
        persona_id,
        template_persona_id="steady_compounder",
        starting_capital=600_000,
    )
    assert updated["applies_on"] == "next_long_cycle"
    assert updated["pending_overrides"]["template_persona_id"] == "steady_compounder"

    config = get_persona_config(persona_id, base=temp_config)
    assert config.secrets.initial_capital == 600_000


def test_activate_requires_holdings(temp_config, monkeypatch):
    monkeypatch.setattr("src.workspace.user_books.PriceService", lambda config=None: FakePriceService())
    service = UserBookService(temp_config, prices=FakePriceService())
    WorkspaceRepository(temp_config).user_for_email("member@example.com")
    book_id = service.create_book(1, name="Empty", market_id="india_equity", starting_capital=100_000)["book"]["id"]

    try:
        service.activate_paper_fund(1, book_id)
        raised = False
    except Exception as exc:
        raised = True
        assert "at least one holding" in str(exc).lower()
    assert raised


def test_dev_activate_api(temp_config, monkeypatch):
    from src.dashboard.app import create_app

    WorkspaceRepository(temp_config).user_for_email("dev-ui@example.com")
    monkeypatch.setattr("src.workspace.user_books.PriceService", lambda config=None: FakePriceService())
    client = TestClient(create_app(temp_config))
    service = UserBookService(temp_config, prices=FakePriceService())

    book_id = service.create_book(1, name="API activate", market_id="india_equity", starting_capital=300_000)["book"]["id"]
    service.add_line(1, book_id, symbol="INFY", target_amount=100_000)

    response = client.post(
        f"/my-watchlist/api/books/{book_id}/activate?user_id=1",
        json={"template_persona_id": "balanced"},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["persona_id"].startswith("userbook_")
    assert client.get(f"/my-watchlist/api/books/{book_id}/persona?user_id=1").status_code == 200
