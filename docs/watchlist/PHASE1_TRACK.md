# Phase 1 — Track only (no FE required)

Paper tracker for user-curated stock baskets. **UI label: My Watchlist.** Backend namespace: `user_books`. No paper persona, no orders.

## What the user does (product)

1. Create a **watchlist** (name + market + starting capital).
2. **Add stocks** with a **₹/$ amount** per line.
3. See **weights** (amount ÷ invested total), **last price**, **return since added**.
4. Edit or remove lines; archive a watchlist.

Unallocated cash = `starting_capital − sum(target_amounts)`.

## What you can test without React

All routes under **`/api/v1/workspace/user-books`** with Google Bearer token (or test client with `FakeWorkspaceAuth`).

| Step | Method | Path | Body |
|------|--------|------|------|
| List | GET | `/api/v1/workspace/user-books` | — |
| Create | POST | `/api/v1/workspace/user-books` | `{"name":"Tech","market_id":"india_equity","starting_capital":500000}` |
| Detail | GET | `/api/v1/workspace/user-books/{id}` | — |
| Add line | POST | `/api/v1/workspace/user-books/{id}/lines` | `{"symbol":"RELIANCE","target_amount":200000}` |
| List lines | GET | `/api/v1/workspace/user-books/{id}/lines` | — |
| Update line | PATCH | `/api/v1/workspace/user-books/{id}/lines/RELIANCE` | `{"target_amount":250000}` |
| Remove | DELETE | `/api/v1/workspace/user-books/{id}/lines/RELIANCE` | — |
| Performance | GET | `/api/v1/workspace/user-books/{id}/performance` | — |
| Search symbols | GET | `/api/v1/workspace/user-books/symbols?q=REL&market_id=india_equity` | — |
| Archive | DELETE | `/api/v1/workspace/user-books/{id}` | — |

Every JSON response includes:

```json
{
  "disclaimer": "Paper tracker. Not investment advice. No live order execution.",
  "data_status": "delayed_eod"
}
```

## Valuation rules (Phase 1)

- **Entry:** on add, `entry_price` = latest EOD close from `PriceService`.
- **Line return:** `(last_price − entry_price) / entry_price × 100`.
- **Line value:** `target_amount × (last_price / entry_price)`.
- **Book total:** `sum(line values) + unallocated_cash`.
- **Book return:** `(total_value − starting_capital) / starting_capital × 100`.

Prices come from cached Yahoo history (same as swing fund). Label `delayed_eod`.

## Limits (MVP)

- Max **5** active books per user.
- Max **25** active lines per book.
- Markets: `india_equity`, `us_equity`.
- Symbol must exist in that market’s universe CSV.

## DB tables

- `user_books` — container (owner, capital, market, status).
- `user_book_lines` — symbol, target_amount, weight_pct, entry_price/date.
- `user_book_snapshots` — optional nightly history (Phase 1b / cron); Phase 1 computes on read.

## Out of scope (Phase 1)

- Activate paper persona (Phase 2).
- Intraday marks.
- Live broker / real money.

## curl smoke (after deploy)

```bash
TOKEN="<google id token>"
BASE="https://<your-api-host>/api/v1/workspace/user-books"

curl -s -H "Authorization: Bearer $TOKEN" "$BASE" | jq .

curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"My picks","market_id":"india_equity","starting_capital":1000000}' \
  "$BASE" | jq .
```

Local tests: `pytest tests/test_user_books.py -q`

## Dev UI (dashboard)

Lite testing page integrated into the operator dashboard — no Google token needed:

```
http://localhost:<dashboard-port>/my-watchlist
```

- Nav link: **My Watchlist** in the dashboard header
- Pick any `workspace_users` row from the dropdown
- Create watchlists, add/remove holdings, see delayed EOD marks
- Backed by `/my-watchlist/api/*` (dev-only; skips Bearer auth)

If the user dropdown is empty, sign in once via the React workspace app (creates a `workspace_users` row) or insert a test user manually.
