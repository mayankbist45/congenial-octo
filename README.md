# AI Mutual Fund Manager (Indian Equities)

An AI portfolio manager for the Nifty 500 that works the way a professional fund
manager does: it reads the news and the numbers every trading day, but trades
rarely. Most days it decides to do nothing and explains why. When a trigger does
fire, it produces a written trade proposal, which is either approved by you in
the dashboard or, in unattended mode, by the fund itself.

**Unattended trading is confined to paper.** With `DRY_RUN=false` the fund
refuses to approve its own orders and waits for a human, so reaching the live
market always takes a person.

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
   NAV and the risk profile are all written to PostgreSQL for later review.

The bias toward inaction is enforced, not merely encouraged: an annual turnover
budget of ~75% (about a 16-month average holding period) caps how much the
manager is allowed to churn, and no proposal is valid without a trigger.

## Setup

Requires Python 3.9 or newer and PostgreSQL.

```bash
brew install postgresql@17
brew services start postgresql@17      # also restarts it at login
createdb mutualfund

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill it in
python scripts/setup_check.py
```

Already have a journal in the old SQLite file? Carry it across, ids and all:

```bash
python scripts/migrate_journal.py --to postgresql:///mutualfund
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
| `DATABASE_URL` | optional | Overrides `database.url`; use it when the DSN has a password |
| `DRY_RUN` | safety | `true` simulates every order and is what keeps auto mode on paper |
| `INITIAL_CAPITAL_INR` | NAV tracking | Your starting allocation |

The system degrades rather than fails when something is missing. Without Angel
One credentials it uses a simulated broker and Yahoo Finance prices. Without an
LLM key it falls back to a keyword news digest and a purely mechanical version
of the quant screen, which will still deploy idle cash into top-ranked names but
writes no qualitative thesis.

## Daily use

The default configuration is unattended: `execution.auto_approve` is on, so one
command does the whole day and nobody needs to be watching.

```bash
python run_daily.py cycle      # research, approve, place, all in one
python run_daily.py close      # after the close: record the day's NAV
```

To review the trades yourself instead, either set `execution.auto_approve: false`
in `config/settings.yaml` or pass `--require-approval` for a single run:

```bash
python run_daily.py analyse                    # before the open: research and propose
python run_daily.py dashboard                  # review and approve at localhost:8000
python run_daily.py execute --require-approval  # place only what you approved
```

Auto-approved trades are journalled with `decided_by = auto`, so a later review
can always separate what the machine waved through from what a human agreed to.

### Running it on a schedule

```bash
./scripts/install_schedule.sh              # install and start
./scripts/install_schedule.sh --status     # check they are loaded
./scripts/install_schedule.sh --remove     # uninstall
```

That registers two launchd jobs, weekdays only: **08:15** runs the full cycle
before the open, and **18:00** marks the book at closing prices and records the
day's NAV. The close deliberately runs well after the 15:30 bell, because the
free price feed publishes the official close with a lag and marking at 16:00
would often re-use the previous day's.

The laptop has to be awake at those times. To have macOS wake itself:

```bash
sudo pmset repeat wakeorpoweron MTWRF 08:10:00
```

Everything the scheduled runs print goes to `data/scheduled.log`, so a week of
unattended operation reads as one continuous story:

```bash
tail -f data/scheduled.log
```

### Running it in the cloud instead

To take the laptop off the critical path, the same two jobs can run on a small
Linux VM with cron instead of launchd:

```bash
./scripts/deploy_gcp.sh deploy     # create the VM, push the fund, provision it
./scripts/deploy_gcp.sh status     # what it did up there
./scripts/deploy_gcp.sh dashboard  # tunnel the UI back to localhost:8000
```

That builds the cloud copy in the same shape as the local one — one machine,
PostgreSQL on localhost, the repo on a persistent disk — so it is portable to any
Debian or Ubuntu host via `scripts/bootstrap_vm.sh`. Costs, backups, security and
the migration path are in [docs/DEPLOY_GCP.md](docs/DEPLOY_GCP.md).

### How much starting capital

Indian equities trade in whole shares, so an underfunded portfolio silently
loses its most expensive candidates and strands cash on every rounded-down
position. This measures both against live prices and the fund's own sizing rules:

```bash
python scripts/capital_check.py
python scripts/capital_check.py --capital 1000000 2500000
```

`python run_daily.py status` prints a one-screen summary: fund value, cash,
positions, return versus the benchmark, turnover used, and how many proposals
are waiting on you.

Useful flags: `--simulated-broker` to force paper mode, `--skip-news` for a fast
rerun, `--max-candidates N` to research only the N most liquid names,
`--refresh-fundamentals` to bypass the cache, and `--as-of YYYY-MM-DD` for a
back-dated catch-up run.

### The dashboard

`python run_daily.py dashboard` serves the UI on <http://127.0.0.1:8000>: today's
proposals with their full rationale and the trigger that produced them, current
holdings with factor scores and risk contributions, the news digest, a NAV chart
against the benchmark, and the searchable history of past runs. In unattended
mode it is how you watch what the fund did; with auto-approval off it is also the
approval gate, and approving a proposal there is what authorises the order.

## Watching how it does

`python run_daily.py status` is the ten-second check: fund value, cash, return
against the benchmark, turnover used, and whether anything is waiting on you.

The dashboard is where the real detail lives. **Performance** carries the NAV
curve against the Nifty 500, total and excess return, Sharpe and maximum
drawdown. **Holdings** shows each position's P&L with the factor scores and risk
contribution that justify still owning it. **Journal** keeps every past run, its
memo, and every proposal including the ones the risk engine refused, which is
usually the most informative page of the lot.

For anything ad hoc the journal is a normal database:

```sql
SELECT run_date, total_value, daily_return_pct, benchmark_return_pct
  FROM nav_history ORDER BY run_date;
SELECT symbol, action, quantity, price, realised_pnl FROM trades ORDER BY id;
```

**How often to look:** weekly. This is a strategy with a ~16-month intended
holding period and a 75%/year turnover budget, so daily P&L is mostly noise, and
watching it daily mainly tempts you to interfere with a thesis that has not been
given time to work. A weekly read of the memos tells you whether the reasoning is
sound, which is the thing you can actually judge early. Check monthly for
drawdown and tracking error against the benchmark, and treat the quarterly deep
review as the real checkpoint. Judging returns over anything less than a year
tells you about luck rather than skill.

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
4. Set `DRY_RUN=false` only when you are ready for real orders. Note that this
   also switches unattended trading off: the fund will stop approving its own
   proposals and start waiting for you.

## Layout

```
config/settings.yaml   Risk limits, factor weights, feeds, cadence: the IPS
data/                  Caches, briefings, universe CSV, logs (journal is in Postgres)
run_daily.py           CLI entry point for the daily cycle
scripts/               setup_check.py, refresh_universe.py, migrate_journal.py
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
  journal/             Schema and repository (PostgreSQL or SQLite), performance
  dashboard/           FastAPI approval UI and charts
tests/                 222 tests covering tagging, risk, decisions, orchestration
```

Run the tests with `python -m pytest`. They use a throwaway SQLite file by
default so no server is needed. To exercise the same suite against PostgreSQL,
which is what the fund actually runs on:

```bash
createdb mutualfund_test
FUND_TEST_DB_URL=postgresql:///mutualfund_test python -m pytest
```

## Caveats

This is a decision-support tool, not investment advice, and it has not been
tested across a full market cycle. Fundamentals come from scraping, so a layout
change upstream can silently degrade data quality — the run logs tell you how
many names resolved. The factor model is a simplification of a commercial risk
model and its tracking-error estimate is an approximation. Review the proposals
on their merits; the approval gate exists because the system can be wrong.
