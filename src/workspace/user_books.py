"""User-curated paper watchlists (UI: My Watchlist; API: user_books).

Track-only Phase 1: users allocate capital to symbols and see delayed EOD marks.
No broker execution and no paper persona provisioning.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.config import PROJECT_ROOT, Config, _deep_merge, get_config
from src.journal.db import connect
from src.logging_setup import get_logger
from src.market.registry import get_market
from src.market_data.prices import PriceService
from src.universe import Universe, _load_universe_cached

logger = get_logger(__name__)

DISCLAIMER = "Paper tracker. Not investment advice. No live order execution."
DATA_STATUS = "delayed_eod"
MAX_BOOKS_PER_USER = 5
MAX_LINES_PER_BOOK = 25
SUPPORTED_MARKETS = frozenset({"india_equity", "us_equity"})


class UserBookError(ValueError):
    """Validation or business-rule failure for user books."""


class UserBookNotFoundError(KeyError):
    """Raised when a book or line is missing or not owned by the caller."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return date.today().isoformat()


def market_config(base: Config, market_id: str) -> Config:
    """Config overlay for a marketplace's universe and currency."""
    market = get_market(market_id)
    merged = _deep_merge(
        copy.deepcopy(dict(base.raw)),
        {
            "universe": dict(market.universe or {}),
            "fund": dict(market.fund or {}),
        },
    )
    return Config(raw=merged, secrets=base.secrets, root=base.root, persona_id=base.persona_id)


def universe_for_market(market_id: str) -> Universe:
    """Load the investable universe CSV for a marketplace."""
    market = get_market(market_id)
    universe_file = str(market.universe.get("file") or "data/nifty500.csv")
    path = PROJECT_ROOT / universe_file if not universe_file.startswith("/") else universe_file
    return _load_universe_cached(str(path))


class UserBookRepository:
    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    class _ConnectionContext:
        def __init__(self, config: Config):
            self.conn = connect(config)

        def __enter__(self):
            return self.conn

        def __exit__(self, *_):
            self.conn.close()

    def _connection(self):
        return self._ConnectionContext(self._config)

    def count_active_books(self, user_id: int) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM user_books WHERE user_id = ? AND status != 'archived'",
                (user_id,),
            ).fetchone()
        return int(row["n"])

    def name_taken(self, user_id: int, name: str, exclude_book_id: Optional[str] = None) -> bool:
        query = (
            "SELECT 1 FROM user_books WHERE user_id = ? AND lower(name) = lower(?) "
            "AND status != 'archived'"
        )
        params: List[Any] = [user_id, name.strip()]
        if exclude_book_id:
            query += " AND id != ?"
            params.append(exclude_book_id)
        with self._connection() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        return row is not None

    def list_books(self, user_id: int) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM user_books WHERE user_id = ? AND status != 'archived' "
                "ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_book(self, book_id: str, user_id: int) -> Dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM user_books WHERE id = ? AND user_id = ? AND status != 'archived'",
                (book_id, user_id),
            ).fetchone()
        if not row:
            raise UserBookNotFoundError("User book not found.")
        return dict(row)

    def create_book(
        self,
        user_id: int,
        *,
        name: str,
        market_id: str,
        currency: str,
        starting_capital: float,
    ) -> Dict[str, Any]:
        book_id, now = str(uuid.uuid4()), _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO user_books "
                "(id, user_id, name, market_id, currency, status, starting_capital, allocation_mode, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'tracking', ?, 'amount', ?, ?)",
                (book_id, user_id, name.strip(), market_id, currency, starting_capital, now, now),
            )
            conn.commit()
        return self.get_book(book_id, user_id)

    def update_book(self, book_id: str, user_id: int, **updates: Any) -> Dict[str, Any]:
        allowed = {"name", "starting_capital", "allocation_mode", "status"}
        fields = [(key, value) for key, value in updates.items() if key in allowed and value is not None]
        if not fields:
            return self.get_book(book_id, user_id)
        assignments = ", ".join(f"{key} = ?" for key, _ in fields) + ", updated_at = ?"
        values = [value for _, value in fields] + [_now(), book_id, user_id]
        with self._connection() as conn:
            cursor = conn.execute(
                f"UPDATE user_books SET {assignments} WHERE id = ? AND user_id = ? AND status != 'archived'",
                values,
            )
            conn.commit()
        if cursor.rowcount == 0:
            raise UserBookNotFoundError("User book not found.")
        return self.get_book(book_id, user_id)

    def archive_book(self, book_id: str, user_id: int) -> None:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE user_books SET status = 'archived', updated_at = ? WHERE id = ? AND user_id = ? AND status != 'archived'",
                (_now(), book_id, user_id),
            )
            conn.commit()
        if cursor.rowcount == 0:
            raise UserBookNotFoundError("User book not found.")

    def list_lines(self, book_id: str, user_id: int) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM user_book_lines WHERE book_id = ? AND user_id = ? AND removed_at IS NULL "
                "ORDER BY added_at ASC",
                (book_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_lines(self, book_id: str, user_id: int) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM user_book_lines WHERE book_id = ? AND user_id = ? AND removed_at IS NULL",
                (book_id, user_id),
            ).fetchone()
        return int(row["n"])

    def get_line(self, book_id: str, user_id: int, symbol: str) -> Dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM user_book_lines WHERE book_id = ? AND user_id = ? AND symbol = ? AND removed_at IS NULL",
                (book_id, user_id, symbol.upper()),
            ).fetchone()
        if not row:
            raise UserBookNotFoundError("Holding not found.")
        return dict(row)

    def add_line(
        self,
        book_id: str,
        user_id: int,
        *,
        symbol: str,
        target_amount: float,
        entry_price: float,
        entry_date: str,
        weight_pct: float,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        line_id, now = str(uuid.uuid4()), _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO user_book_lines "
                "(id, book_id, user_id, symbol, target_amount, weight_pct, entry_price, entry_date, notes, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    line_id,
                    book_id,
                    user_id,
                    symbol.upper(),
                    target_amount,
                    weight_pct,
                    entry_price,
                    entry_date,
                    notes,
                    now,
                ),
            )
            conn.execute("UPDATE user_books SET updated_at = ? WHERE id = ?", (now, book_id))
            conn.commit()
        return self.get_line(book_id, user_id, symbol)

    def update_line(
        self,
        book_id: str,
        user_id: int,
        symbol: str,
        *,
        target_amount: float,
        weight_pct: float,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE user_book_lines SET target_amount = ?, weight_pct = ?, notes = COALESCE(?, notes) "
                "WHERE book_id = ? AND user_id = ? AND symbol = ? AND removed_at IS NULL",
                (target_amount, weight_pct, notes, book_id, user_id, symbol.upper()),
            )
            conn.execute("UPDATE user_books SET updated_at = ? WHERE id = ?", (now, book_id))
            conn.commit()
        if cursor.rowcount == 0:
            raise UserBookNotFoundError("Holding not found.")
        return self.get_line(book_id, user_id, symbol)

    def update_line_weights(self, book_id: str, user_id: int, weights: Dict[str, float]) -> None:
        now = _now()
        with self._connection() as conn:
            for symbol, weight_pct in weights.items():
                conn.execute(
                    "UPDATE user_book_lines SET weight_pct = ? WHERE book_id = ? AND user_id = ? AND symbol = ? AND removed_at IS NULL",
                    (weight_pct, book_id, user_id, symbol.upper()),
                )
            conn.execute("UPDATE user_books SET updated_at = ? WHERE id = ?", (now, book_id))
            conn.commit()

    def remove_line(self, book_id: str, user_id: int, symbol: str) -> None:
        now = _now()
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE user_book_lines SET removed_at = ? WHERE book_id = ? AND user_id = ? AND symbol = ? AND removed_at IS NULL",
                (now, book_id, user_id, symbol.upper()),
            )
            conn.execute("UPDATE user_books SET updated_at = ? WHERE id = ?", (now, book_id))
            conn.commit()
        if cursor.rowcount == 0:
            raise UserBookNotFoundError("Holding not found.")

    def save_snapshot(
        self,
        book_id: str,
        snapshot_date: str,
        *,
        total_value: float,
        total_return_pct: float,
        invested_amount: float,
        unallocated_cash: float,
        payload: Dict[str, Any],
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO user_book_snapshots "
                "(book_id, snapshot_date, total_value, total_return_pct, invested_amount, unallocated_cash, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(book_id, snapshot_date) DO UPDATE SET "
                "total_value = excluded.total_value, total_return_pct = excluded.total_return_pct, "
                "invested_amount = excluded.invested_amount, unallocated_cash = excluded.unallocated_cash, "
                "payload = excluded.payload, created_at = excluded.created_at",
                (
                    book_id,
                    snapshot_date,
                    total_value,
                    total_return_pct,
                    invested_amount,
                    unallocated_cash,
                    json.dumps(payload),
                    _now(),
                ),
            )
            conn.commit()

    def list_snapshots(self, book_id: str, limit: int = 365) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT snapshot_date, total_value, total_return_pct, invested_amount, unallocated_cash, payload, created_at "
                "FROM user_book_snapshots WHERE book_id = ? ORDER BY snapshot_date DESC LIMIT ?",
                (book_id, limit),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload") or "{}")
            out.append(item)
        return list(reversed(out))

    def list_tracking_books(self) -> List[Tuple[str, int]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, user_id FROM user_books WHERE status = 'tracking' ORDER BY updated_at ASC"
            ).fetchall()
        return [(str(row["id"]), int(row["user_id"])) for row in rows]


class UserBookService:
    def __init__(
        self,
        config: Optional[Config] = None,
        repo: Optional[UserBookRepository] = None,
        prices: Optional[PriceService] = None,
    ):
        self._config = config or get_config()
        self.repo = repo or UserBookRepository(self._config)
        self._prices = prices

    def _price_service(self, market_id: str) -> PriceService:
        if self._prices is not None:
            return self._prices
        return PriceService(market_config(self._config, market_id))

    def envelope(self, **payload: Any) -> Dict[str, Any]:
        return {
            **payload,
            "disclaimer": DISCLAIMER,
            "data_status": DATA_STATUS,
        }

    def list_books(self, user_id: int) -> Dict[str, Any]:
        books = self.repo.list_books(user_id)
        summaries = []
        for book in books:
            detail = self._summarize_book(book, self.repo.list_lines(book["id"], user_id))
            summaries.append({**book, "summary": detail})
        return self.envelope(books=summaries)

    def create_book(
        self,
        user_id: int,
        *,
        name: str,
        market_id: str,
        starting_capital: float,
    ) -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise UserBookError("Name is required.")
        if market_id not in SUPPORTED_MARKETS:
            raise UserBookError(f"Unsupported market_id: {market_id}")
        if starting_capital <= 0:
            raise UserBookError("starting_capital must be positive.")
        if self.repo.count_active_books(user_id) >= MAX_BOOKS_PER_USER:
            raise UserBookError(f"Maximum of {MAX_BOOKS_PER_USER} active watchlists reached.")
        if self.repo.name_taken(user_id, name):
            raise UserBookError("A watchlist with this name already exists.")
        market = get_market(market_id)
        book = self.repo.create_book(
            user_id,
            name=name,
            market_id=market_id,
            currency=market.currency,
            starting_capital=float(starting_capital),
        )
        return self.envelope(book=book, summary=self._empty_summary(book))

    def get_book(self, user_id: int, book_id: str) -> Dict[str, Any]:
        book = self.repo.get_book(book_id, user_id)
        lines = self.repo.list_lines(book_id, user_id)
        return self.envelope(
            book=book,
            summary=self._summarize_book(book, lines),
            lines=self._mark_lines(book, lines),
        )

    def update_book(
        self,
        user_id: int,
        book_id: str,
        *,
        name: Optional[str] = None,
        starting_capital: Optional[float] = None,
    ) -> Dict[str, Any]:
        updates: Dict[str, Any] = {}
        if name is not None:
            cleaned = name.strip()
            if not cleaned:
                raise UserBookError("Name is required.")
            if self.repo.name_taken(user_id, cleaned, exclude_book_id=book_id):
                raise UserBookError("A watchlist with this name already exists.")
            updates["name"] = cleaned
        if starting_capital is not None:
            if starting_capital <= 0:
                raise UserBookError("starting_capital must be positive.")
            updates["starting_capital"] = float(starting_capital)
        book = self.repo.update_book(book_id, user_id, **updates)
        lines = self.repo.list_lines(book_id, user_id)
        if updates.get("starting_capital") is not None and lines:
            self._recompute_weights(book_id, user_id, lines)
            lines = self.repo.list_lines(book_id, user_id)
        return self.envelope(
            book=book,
            summary=self._summarize_book(book, lines),
            lines=self._mark_lines(book, lines),
        )

    def archive_book(self, user_id: int, book_id: str) -> Dict[str, Any]:
        self.repo.archive_book(book_id, user_id)
        return self.envelope(status="archived", book_id=book_id)

    def list_lines(self, user_id: int, book_id: str) -> Dict[str, Any]:
        book = self.repo.get_book(book_id, user_id)
        lines = self.repo.list_lines(book_id, user_id)
        return self.envelope(
            book_id=book_id,
            lines=self._mark_lines(book, lines),
            summary=self._summarize_book(book, lines),
        )

    def add_line(
        self,
        user_id: int,
        book_id: str,
        *,
        symbol: str,
        target_amount: float,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        book = self.repo.get_book(book_id, user_id)
        symbol = symbol.strip().upper()
        if not symbol:
            raise UserBookError("symbol is required.")
        if target_amount <= 0:
            raise UserBookError("target_amount must be positive.")
        if self.repo.count_lines(book_id, user_id) >= MAX_LINES_PER_BOOK:
            raise UserBookError(f"Maximum of {MAX_LINES_PER_BOOK} holdings per watchlist reached.")
        universe = universe_for_market(book["market_id"])
        if not universe.contains(symbol):
            raise UserBookError(f"{symbol} is not in the {book['market_id']} universe.")
        try:
            self.repo.get_line(book_id, user_id, symbol)
            raise UserBookError(f"{symbol} is already in this watchlist.")
        except UserBookNotFoundError:
            pass

        entry_price = self._price_service(book["market_id"]).get_last_price(symbol)
        if entry_price is None or entry_price <= 0:
            raise UserBookError(f"No delayed EOD price available for {symbol}.")

        lines = self.repo.list_lines(book_id, user_id)
        invested_after = sum(float(line["target_amount"] or 0) for line in lines) + float(target_amount)
        if invested_after > float(book["starting_capital"]):
            raise UserBookError("Total allocated amount exceeds starting capital.")

        weight_pct = (float(target_amount) / invested_after * 100.0) if invested_after > 0 else 0.0
        line = self.repo.add_line(
            book_id,
            user_id,
            symbol=symbol,
            target_amount=float(target_amount),
            entry_price=float(entry_price),
            entry_date=_today(),
            weight_pct=weight_pct,
            notes=notes,
        )
        self._recompute_weights(book_id, user_id, lines + [line])
        book = self.repo.get_book(book_id, user_id)
        lines = self.repo.list_lines(book_id, user_id)
        return self.envelope(
            line=next(item for item in self._mark_lines(book, lines) if item["symbol"] == symbol),
            summary=self._summarize_book(book, lines),
        )

    def update_line(
        self,
        user_id: int,
        book_id: str,
        symbol: str,
        *,
        target_amount: Optional[float] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        book = self.repo.get_book(book_id, user_id)
        symbol = symbol.upper()
        existing = self.repo.get_line(book_id, user_id, symbol)
        lines = self.repo.list_lines(book_id, user_id)
        amount = float(target_amount if target_amount is not None else existing["target_amount"])
        if amount <= 0:
            raise UserBookError("target_amount must be positive.")
        other_total = sum(float(line["target_amount"] or 0) for line in lines if line["symbol"] != symbol)
        if other_total + amount > float(book["starting_capital"]):
            raise UserBookError("Total allocated amount exceeds starting capital.")
        invested_after = other_total + amount
        weight_pct = (amount / invested_after * 100.0) if invested_after > 0 else 0.0
        line = self.repo.update_line(
            book_id,
            user_id,
            symbol,
            target_amount=amount,
            weight_pct=weight_pct,
            notes=notes,
        )
        updated_lines = [
            line if item["symbol"] == symbol else item
            for item in lines
        ]
        self._recompute_weights(book_id, user_id, updated_lines)
        book = self.repo.get_book(book_id, user_id)
        lines = self.repo.list_lines(book_id, user_id)
        return self.envelope(
            line=next(item for item in self._mark_lines(book, lines) if item["symbol"] == symbol),
            summary=self._summarize_book(book, lines),
        )

    def remove_line(self, user_id: int, book_id: str, symbol: str) -> Dict[str, Any]:
        book = self.repo.get_book(book_id, user_id)
        self.repo.remove_line(book_id, user_id, symbol.upper())
        lines = self.repo.list_lines(book_id, user_id)
        self._recompute_weights(book_id, user_id, lines)
        book = self.repo.get_book(book_id, user_id)
        lines = self.repo.list_lines(book_id, user_id)
        return self.envelope(summary=self._summarize_book(book, lines))

    def performance(self, user_id: int, book_id: str) -> Dict[str, Any]:
        book = self.repo.get_book(book_id, user_id)
        lines = self.repo.list_lines(book_id, user_id)
        summary = self._summarize_book(book, lines)
        series = self.repo.list_snapshots(book_id)
        if not series:
            series = [
                {
                    "snapshot_date": _today(),
                    "total_value": summary["total_value"],
                    "total_return_pct": summary["total_return_pct"],
                    "invested_amount": summary["invested_amount"],
                    "unallocated_cash": summary["unallocated_cash"],
                    "payload": {"lines": self._mark_lines(book, lines)},
                    "created_at": _now(),
                }
            ]
        return self.envelope(book_id=book_id, summary=summary, series=series)

    def search_symbols(self, market_id: str, query: str, limit: int = 20) -> Dict[str, Any]:
        if market_id not in SUPPORTED_MARKETS:
            raise UserBookError(f"Unsupported market_id: {market_id}")
        needle = (query or "").strip().upper()
        if len(needle) < 1:
            raise UserBookError("query is required.")
        return self.envelope(
            market_id=market_id,
            query=query,
            symbols=self._match_universe(market_id, needle, limit),
        )

    def list_universe(self, market_id: str, query: str = "") -> Dict[str, Any]:
        if market_id not in SUPPORTED_MARKETS:
            raise UserBookError(f"Unsupported market_id: {market_id}")
        needle = (query or "").strip().upper()
        symbols = self._match_universe(market_id, needle, limit=None)
        return self.envelope(market_id=market_id, query=query, symbols=symbols, count=len(symbols))

    def _match_universe(
        self,
        market_id: str,
        needle: str,
        limit: Optional[int],
    ) -> List[Dict[str, str]]:
        universe = universe_for_market(market_id)
        matches: List[Dict[str, str]] = []
        for entry in universe:
            if needle and needle not in entry.symbol and needle not in entry.company_name.upper():
                continue
            matches.append(
                {
                    "symbol": entry.symbol,
                    "company_name": entry.company_name,
                    "sector": entry.sector,
                }
            )
            if limit is not None and len(matches) >= limit:
                break
        return matches

    def record_snapshot(self, book_id: str, user_id: int, snapshot_date: Optional[str] = None) -> None:
        detail = self.get_book(user_id, book_id)
        summary = detail["summary"]
        self.repo.save_snapshot(
            book_id,
            snapshot_date or _today(),
            total_value=summary["total_value"],
            total_return_pct=summary["total_return_pct"],
            invested_amount=summary["invested_amount"],
            unallocated_cash=summary["unallocated_cash"],
            payload={"lines": detail["lines"], "book": detail["book"]},
        )

    def record_all_snapshots(self, snapshot_date: Optional[str] = None) -> int:
        when = snapshot_date or _today()
        count = 0
        for book_id, user_id in self.repo.list_tracking_books():
            try:
                self.record_snapshot(book_id, user_id, when)
                count += 1
            except Exception as exc:
                logger.warning("User book snapshot failed for %s: %s", book_id, exc)
        return count

    def _recompute_weights(self, book_id: str, user_id: int, lines: List[Dict[str, Any]]) -> None:
        invested = sum(float(line["target_amount"] or 0) for line in lines)
        if invested <= 0:
            return
        weights = {
            line["symbol"]: float(line["target_amount"] or 0) / invested * 100.0
            for line in lines
        }
        self.repo.update_line_weights(book_id, user_id, weights)

    def _empty_summary(self, book: Dict[str, Any]) -> Dict[str, Any]:
        capital = float(book["starting_capital"])
        return {
            "total_value": capital,
            "total_return_pct": 0.0,
            "invested_amount": 0.0,
            "unallocated_cash": capital,
        }

    def _summarize_book(self, book: Dict[str, Any], lines: List[Dict[str, Any]]) -> Dict[str, Any]:
        marked = self._mark_lines(book, lines)
        starting = float(book["starting_capital"])
        invested = sum(float(line["target_amount"] or 0) for line in lines)
        line_values = sum(float(line.get("current_value") or 0) for line in marked)
        unallocated = starting - invested
        total_value = line_values + unallocated
        total_return_pct = ((total_value - starting) / starting * 100.0) if starting > 0 else 0.0
        return {
            "total_value": round(total_value, 2),
            "total_return_pct": round(total_return_pct, 4),
            "invested_amount": round(invested, 2),
            "unallocated_cash": round(unallocated, 2),
        }

    def _mark_lines(self, book: Dict[str, Any], lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not lines:
            return []
        symbols = [line["symbol"] for line in lines]
        prices = self._price_service(book["market_id"]).get_last_prices(symbols)
        universe = universe_for_market(book["market_id"])
        invested = sum(float(line["target_amount"] or 0) for line in lines)
        marked: List[Dict[str, Any]] = []
        for line in lines:
            amount = float(line["target_amount"] or 0)
            entry_price = float(line["entry_price"])
            last_price = prices.get(line["symbol"])
            if last_price and entry_price > 0:
                current_value = amount * (last_price / entry_price)
                return_since_added_pct = (last_price - entry_price) / entry_price * 100.0
            else:
                current_value = amount
                last_price = last_price or entry_price
                return_since_added_pct = None
            entry = universe.get(line["symbol"])
            marked.append(
                {
                    **line,
                    "company_name": entry.company_name if entry else line["symbol"],
                    "sector": entry.sector if entry else "Unknown",
                    "last_price": round(float(last_price), 4) if last_price is not None else None,
                    "current_value": round(current_value, 2),
                    "return_since_added_pct": round(return_since_added_pct, 4)
                    if return_since_added_pct is not None
                    else None,
                    "weight_pct": round(float(line.get("weight_pct") or 0), 4)
                    if invested > 0
                    else 0.0,
                }
            )
        return marked
