# AI Mutual Fund Manager (Indian Equities)

An AI portfolio manager for the Nifty 500 that works the way a professional fund
manager does: it reads the news and the numbers every trading day, but trades
rarely. Most days it tells you to do nothing and explains why. When a trigger
does fire, it hands you a written trade proposal to approve or reject, and only
then places the order through Angel One.

**Nothing is ever bought or sold without your explicit approval.**

## What it does each day

1. **Market data** — daily OHLCV for the universe, plus technical indicators
   (50/200-DMA, RSI, MACD, ATR, 6- and 12-month momentum).
2. **Fundamentals** — P/E, ROE, ROCE, debt/equity, revenue and EPS growth,
   scraped from Screener.in with a weekly cache and Yahoo Finance as backup.
3. **Quant screen** — a composite z-score across quality, value, growth,
   momentum and trend, behind hard fundamental gates.
4. **Risk analytics** — a simplified Barra-style factor model giving each
   position a beta and an idiosyncratic volatility, then portfolio tracking
   error, active share, and each holding's contribution to total risk.
5. **News** — RSS from Moneycontrol, Economic Times, LiveMint, BusinessLine and
   NDTV Profit, deduplicated and tagged to companies, plus a targeted search per
   holding. An LLM turns this into a digest and flags anything that threatens an
   investment case.
6. **Sell discipline** — the six professional sell triggers (stop-loss, profit
   review, trailing stop, valuation stretch, fundamental deterioration,
   opportunity cost) checked mechanically, plus drift bands and position, sector
   and risk-concentration caps.
7. **Decision** — the LLM receives a full briefing and must return structured
   proposals naming the trigger that justifies each one. Every proposal is then
   re-validated against the hard risk rules, which the LLM cannot override.
8. **Journal** — the run, the memo, every proposal (approved or rejected), fills,
   NAV and the risk profile are all written to SQLite for later review.

The bias toward inaction is enforced, not merely encouraged: an annual turnover
budget of ~75% (about a 16-month average holding period) caps how much the
manager is allowed to churn, and no proposal is valid without a trigger.

## Setup

Requires Python 3.9 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill it in
python scripts/setup_check.py
```

`setup_check.py` validates your config, the universe file, database access and
which credentials it found, then prints the active policy summary. Run it first;
it tells you exactly what is missing and what the fund will do without it.

### Credentials

Everything goes in `.env` (never committed):

| Variable | Needed for | Where to get it |
| --- | --- | --- |
| `ANGELONE_API_KEY` | live prices, holdings, orders | Create a "Trading API" app at [smartapi.angelbroking.com](https://smartapi.angelbroking.com) |
| `ANGELONE_CLIENT_CODE` | login | Your Angel One client ID |
| `ANGELONE_PIN` | login | Your trading PIN |
| `ANGELONE_TOTP_SECRET` | login | The base32 secret shown when you enable TOTP for the API |
| `OPENAI_API_KEY` | news digest and trade proposals | Any OpenAI-compatible provider |
| `OPENAI_BASE_URL` | optional | Set this to use Anthropic, Gemini or a local model through a gateway |
| `DRY_RUN` | safety | `true` simulates every order, even approved ones |
| `INITIAL_CAPITAL_INR` | NAV tracking | Your starting allocation |

The system degrades rather than fails when something is missing. Without Angel
One credentials it uses a simulated broker and Yahoo Finance prices. Without an
LLM key it falls back to a keyword news digest and a purely mechanical version
of the quant screen, which will still deploy idle cash into top-ranked names but
writes no qualitative thesis.

## Daily use

```bash
python run_daily.py analyse       # before the open: research and propose
python run_daily.py dashboard     # review and approve at localhost:8000
python run_daily.py execute       # place what you approved
python run_daily.py close         # after the close: record the day's NAV
```

Or as one unattended sequence that pauses for your approvals in between:

```bash
python run_daily.py cycle --wait 90
```

`python run_daily.py status` prints a one-screen summary: fund value, cash,
positions, return versus the benchmark, turnover used, and how many proposals
are waiting on you.

Useful flags: `--simulated-broker` to force paper mode, `--skip-news` for a fast
rerun, `--max-candidates N` to research only the N most liquid names,
`--refresh-fundamentals` to bypass the cache, and `--as-of YYYY-MM-DD` for a
back-dated catch-up run.

### The dashboard

`python run_daily.py dashboard` serves the approval UI on
<http://127.0.0.1:8000>: today's proposals with their full rationale and the
trigger that produced them, current holdings with factor scores and risk
contributions, the news digest, a NAV chart against the benchmark, and the
searchable history of past runs. Approving a proposal is what authorises the
order; nothing else does.

## Before the first live run

1. Run in `DRY_RUN=true` with `--simulated-broker` for several days and read the
   memos. The point is to see whether you agree with its reasoning before money
   is involved.
2. Read `config/settings.yaml` end to end. Every number in it is a policy
   decision — position caps, sector caps, stop-loss, turnover budget, tracking
   error limit, factor weights — and it is meant to be treated as the fund's
   investment policy statement. Change it deliberately, not mid-cycle.
3. Refresh the universe with `python scripts/refresh_universe.py` so the
   constituent list is current.
4. Set `DRY_RUN=false` only when you are ready for real orders.

## Layout

```
config/settings.yaml   Risk limits, factor weights, feeds, cadence: the IPS
data/                  SQLite journal, caches, briefings, universe CSV, logs
run_daily.py           CLI entry point for the daily cycle
scripts/               setup_check.py, refresh_universe.py
src/
  broker/              Angel One SmartAPI, plus simulated and dry-run wrappers
  market_data/         Price history and technical indicators
  fundamentals/        Screener.in and Yahoo fetchers with caching
  news/                RSS ingestion, symbol tagging, per-holding search, digest
  quant/               Multi-factor scoring and ranking
  risk_analytics/      Factor model, beta and idio vol, tracking error
  risk/                Sell triggers, position sizing, caps, turnover budget
  decision/            Briefing, prompts, and the LLM decision engine
  execution/           Order placement and fill handling
  journal/             SQLite schema, repository, performance, portfolio state
  dashboard/           FastAPI approval UI and charts
tests/                 208 tests covering tagging, risk, decisions, orchestration
```

Run the tests with `python -m pytest`.

## Caveats

This is a decision-support tool, not investment advice, and it has not been
tested across a full market cycle. Fundamentals come from scraping, so a layout
change upstream can silently degrade data quality — the run logs tell you how
many names resolved. The factor model is a simplification of a commercial risk
model and its tracking-error estimate is an approximation. Review the proposals
on their merits; the approval gate exists because the system can be wrong.
