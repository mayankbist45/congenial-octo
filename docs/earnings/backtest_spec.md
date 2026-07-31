# Quarterly Earnings Strategies — Backtest Specification

**Status:** v1.0, for implementation. Every formula, threshold and rule a quant dev
needs is in this document. If a number is not here, ask the analyst — do not
guess. Where a choice is a judgement call rather than established practice, it is
marked **[JUDGEMENT]** and the reasoning is stated.

**Purpose.** Decide, with evidence, which quarterly-earnings signals (if any)
earn a place in this fund's production decision loop. Backtest first, integrate
only what clears the go/no-go bars in §9. A signal that fails its bars does not
get built.

**Fund constraints that shape everything below** (from `README.md` and
`config/settings.yaml`):

- ~25 positions, equal-weight target ~3.8% ((100 − 5% cash buffer) / 25),
  position cap 8%, sector cap 25%.
- Annual one-way turnover budget 75% (~16-month average holding period). Any
  signal that requires constant trading is unusable, however predictive.
- Decisions flow trigger engine → LLM decision engine → risk engine. Earnings
  output plugs in as new triggers plus a briefing section. It never places orders.
- All production thresholds live in `config/settings.yaml`.

---

## 1. Data legs

### 1.1 US leg (statistical-power leg)

| Item | Specification |
| --- | --- |
| Universe | S&P 500 constituents as of the scrape date (survivorship caveat, §6.6) |
| Fundamentals | SEC EDGAR XBRL `companyfacts` API (`https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`, free, `User-Agent` header required) |
| Prices | Daily adjusted closes, any free source (e.g. Yahoo). Benchmark: SPY adjusted close (total-return proxy) |
| Period | Signals from 2015-01-01 through 2025-12-31 (fact history back to ~2009 allows the standardisation warmup) |

**XBRL tags** (try in order, first non-null wins):

- EPS: `EarningsPerShareDiluted`, fallback `EarningsPerShareBasic`
- Revenue: `RevenueFromContractWithCustomerExcludingAssessedCost` (ASC 606 era),
  fallback `Revenues`, fallback `SalesRevenueNet`
- Net income: `NetIncomeLoss`, fallback `ProfitLoss`
- Operating income: `OperatingIncomeLoss`

**Quarterly fact identification.** Keep facts whose duration (`end − start`) is
80–100 days. For each (tag, fiscal period end), multiple filings may carry a
value: **use the value with the earliest `filed` date** (the as-originally-
reported number) and discard later revisions. This is what makes the US leg
genuinely point-in-time.

**Q4 derivation.** US issuers rarely file a standalone Q4 figure (the 10-K
carries the annual duration). Derive Q4 = FY − (Q1 + Q2 + Q3) per tag when all
three quarters and the FY figure are available. For EPS this is an approximation
(share-count drift across the year); acceptable — winsorisation (§2.4) blunts
the errors. If any component is missing, the Q4 event is skipped for that tag.

**Availability date (US).** `availability_date = filed` (the filing date on the
chosen fact). No intraday handling: a filing is treated as public for the whole
calendar day `filed`, and we never trade on that day (§2.2).

### 1.2 India leg (out-of-sample leg)

| Item | Specification |
| --- | --- |
| Universe | Nifty 500 constituents as of the scrape date (`data/nifty500.csv` plus the refresh script) |
| Fundamentals | Screener.in quarterly results table: Sales, Operating Profit, OPM %, Net Profit, EPS in Rs; ~12 quarters deep; period labels like "Jun 2025"; usually **no announcement date** |
| Prices | Daily adjusted closes (Yahoo `.NS`). Benchmark: Nifty 500 TRI proxy `^CRSLDX`, matching `fund.benchmark_symbol` in config |
| Period | Whatever the ~12-quarter depth supports; expect signals from roughly 2023 onward. **This is the leg's biggest limitation — see §6.7.** |

**Availability date (India), default assumption.** Per SEBI LODR Reg. 33(3):
quarterly results are due within 45 days of quarter end, and annual audited
results within 60 days of financial-year end. Default:

- Q1–Q3: `availability_date = period_end + 45 calendar days`
- Fiscal Q4: `availability_date = period_end + 60 calendar days`
- Roll forward to the next trading day if the result is a non-trading day.

**Real announcement dates** will be scraped where available (BSE/NSE corporate
announcements). Usage rule: the **primary** India results use the uniform
45/60-day assumption for every name, so the sample is internally consistent. A
**side-by-side sensitivity run** on the subsample with scraped dates uses
`availability = actual_announcement_date`. Additionally: any scraped actual date
*later* than the assumed date marks the assumption as too aggressive for that
event — flag those events and report results with and without them.

**Data caveats to log, not fix:**

- Record whether each company's table is consolidated or standalone, and never
  mix the two within one company's series (if Screener switches basis mid-
  history, truncate the series at the switch).
- Screener values are as-restated-at-scrape-time, not as-originally-reported.
  Unfixable with free data; §6.6 discusses the bias direction.
- "EPS in Rs" is quarterly basic EPS as Screener computes it; treat as given.

---

## 2. Common conventions

### 2.1 Cohorts

Events are ranked cross-sectionally within **monthly cohorts**: all events whose
`availability_date` falls in the same calendar month and market. India results
cluster around mid-Feb / mid-May / mid-Aug / mid-Nov; monthly cohorts keep those
cross-sections large enough to rank.

### 2.2 Entry timing (identical for every signal, both markets)

```
entry_date  = first trading day STRICTLY AFTER availability_date
entry_price = close on entry_date
```

Never trade on the availability date itself. No exceptions, no intraday. If no
price exists on `entry_date` (halt/suspension), roll forward up to 5 trading
days; if still no price, drop the event and log it. All event returns are
measured **from entry close to close** at each horizon — the announcement-day
move is deliberately excluded from tradeable results (it is only shown as a
diagnostic pre-window in the US event study, §6.1).

### 2.3 Fiscal-quarter matching for YoY

"Same quarter last year" means 4 quarters back in the **company's own filing
sequence**, not the calendar. Require the two period-end dates to be 320–410
days apart; otherwise treat the YoY pair as missing (handles off-cycle fiscal
years and skipped filings honestly instead of silently comparing the wrong
quarters).

### 2.4 Cleaning

- Cross-sectional winsorisation at the 1st/99th percentile **within each monthly
  cohort**, applied to every signal input (EPS deltas, growth rates, margin
  changes) before ranking.
- Hard-clip any time-series standardised measure (S1) to ±6 to bound data errors.
- Growth-rate denominators: use `g = (x_q − x_{q−4}) / |x_{q−4}|` so negative
  bases do not flip signs. If `|x_{q−4}|` is within the bottom 1% of the cohort's
  absolute bases, treat growth as missing (division noise).
- Every exclusion is counted and reported by reason. No silent drops.

### 2.5 Sector exclusions

**[JUDGEMENT]** Exclude financials (US: GICS 40; India: banks/NBFCs/housing
finance per the universe's sector labels) from every margin-based leg (S2's OPM
expansion, S3's margin compression). Operating margin is not economically
meaningful for lenders, and free data does not give a clean NIM proxy. They
remain eligible for the revenue/profit/SUE legs. Count and report how many
events this removes. The alternative — proxying bank margins — adds estimation
error to exactly the names where errors are largest.

---

## 3. Signal S1 — PEAD (post-earnings-announcement drift)

The classic anomaly: stocks with unexpectedly good earnings keep drifting up for
weeks after the announcement. Reference: Foster, Olsen & Shevlin (1984) SUE.

### 3.1 Surprise measures (compute all three; TS-plain is primary for US, CS is primary for India)

Let `EPS_q` be quarterly diluted EPS, `EPS_{q−4}` its YoY match (§2.3), and
`d_q = EPS_q − EPS_{q−4}` the raw surprise.

**SUE-TS (time-series, plain):**

```
SUE_TS(q) = d_q / σ_i(q)
σ_i(q)    = sample std-dev (ddof=1) of the trailing m values of d_q
            (all with availability_date strictly before the current one)
m ∈ {4, 6, 8}           # grid; min available = m, no partial windows
```

Requires m + 4 quarters of EPS history. If σ_i < 1e-6, drop the event
(constant-EPS series carry no information and produce explosive ratios).

**SUE-TS-drift (variant):** subtract the trailing-m mean of `d_q` in the
numerator (seasonal random walk with drift). Same grid.

**SUE-CS (cross-sectional):**

```
SUE_CS(q) = z-score of (d_q / P_entry) within the monthly cohort
P_entry   = close price on entry_date
```

Needs only 5 quarters of EPS history, is robust to short series, and is
therefore the **primary measure for India** (where ~12 quarters make σ_i
estimates on m=8 barely computable) and a robustness check for the US.

**Revenue SUE (robustness):** same three constructions on revenue instead of
EPS. EPS is primary because PEAD is an earnings anomaly; revenue tests whether
the signal is really about the top line.

### 3.2 Ranking and buckets

- Rank each monthly cohort by the surprise measure.
- US: deciles. India: quintiles (smaller cross-section).
- Tradeable buckets: top bucket (long candidates) and bottom bucket (avoid/early-
  warning; the fund is long-only, so bottom-bucket results matter for holdings,
  not shorts).
- Minimum-surprise filter grid: `|SUE| ≥ {0, 1.0, 2.0}` for the TS measures;
  `|z| ≥ {0, 1.0, 2.0}` for CS.

### 3.3 Entry, exit, holding window

- Entry: §2.2.
- Exit: fixed horizon `h ∈ {21, 42, 63}` trading days (grid). Primary horizon
  for go/no-go: **21 days**.
- No stop-loss or profit-taking inside the event study — that is measurement,
  not trading. The portfolio sim (§6.2) evaluates the same signals under
  fund-realistic constraints separately.
- Overlapping events in the same name: allowed and independent in the event
  study. In the portfolio sim, a fresh event re-affirms or reverses the position
  but never doubles it.

### 3.4 Ties and missing data

- Ranking ties (identical SUE): break by higher revenue YoY growth, then by
  symbol alphabetically. Deterministic, no randomness anywhere.
- Missing `EPS_q` or `EPS_{q−4}`: event excluded from S1 (still eligible for S2/
  S3 legs that do not need EPS).
- Insufficient history for SUE-TS: event excluded from TS ranking but included in
  CS ranking (that is what CS is for).

---

## 4. Signal S2 — Quality earnings momentum (buy-side)

The thesis: revenue growth **funded by** margin expansion is the highest-quality
growth and persists. Either leg alone is weak; the combination is the signal.

### 4.1 Legs

```
g_rev(q) = Sales_q / Sales_{q−4} − 1                    # §2.4 denominator rule
ΔOPM(q)  = OPM_q − OPM_{q−4}                            # in basis points
```

- Revenue leg fires when `g_rev ≥ θ_rev`, `θ_rev ∈ {0%, +5%, +10%}` (grid).
- Margin leg fires when `ΔOPM ≥ θ_opm`, `θ_opm ∈ {0, +100, +200}` bps (grid).
  (US: `OPM = OperatingIncomeLoss / Revenues` per quarter.)

### 4.2 Combination rules (two variants, both run)

**V1 — AND rule:** both legs fire at the same availability date.

**V2 — Composite z:** `score = z_cohort(g_rev) + z_cohort(ΔOPM)`; top quintile of
the monthly cohort. V2 degrades gracefully when one input is missing (use the
available z, flagged); V1 requires both.

**Persistence filter (grid, applied to both variants):** `g_rev > 0` in each of
the last `k` quarters, `k ∈ {1, 2, 3}`. This is what makes it *momentum* rather
than a one-off.

### 4.3 Entry, exit

- Entry: §2.2.
- Two exit variants, both run:
  - **Time-based:** `h ∈ {63, 126, 189}` trading days. Primary: 126.
  - **State-based:** hold while the combination still holds; exit at the close
    of the entry_date of the first subsequent announcement where it no longer
    holds; hard cap 252 trading days. State-based is the production-relevant
    variant — it is how the fund would actually use a slow signal.
- Financials excluded (§2.5).

---

## 5. Signal S3 — Earnings deterioration (SELL-side; highest priority for this fund)

This maps directly onto the fund's existing `fundamental_deterioration`
discipline, but forward-looking at quarterly frequency instead of waiting for
the annual gate refresh. The design principle: **one bad quarter is noise; a
pattern is a thesis break.** The fund's 16-month holding period means it will
sit through single bad quarters by design — the signal must not fire on them.

### 5.1 Components (all YoY, §2.3; QoQ is too seasonal in India to be trusted)

**C1 — Consecutive profit decline:** `NP_q < NP_{q−4}` in each of the last `c`
consecutive reported quarters, `c ∈ {2, 3}` (grid).

**C2 — Margin compression:** `ΔOPM(q) ≤ −θ_m` bps, `θ_m ∈ {200, 300, 500}`
(grid). Variant: cumulative `OPM_q − OPM_{q−8} ≤ −θ_m` (two-year compression).

**C3 — Revenue deceleration:** either (a) `g_rev` steps down two quarters in a
row (`g_rev(q) < g_rev(q−1) < g_rev(q−2)`), or (b) `g_rev(q) < 0`.

### 5.2 Confirmation rule

```
WATCH:     exactly one of {C1, C2, C3} fires
CONFIRMED: ≥ 2 of {C1, C2, C3} fire simultaneously, OR C1 fires with c = 3
CLEAR:     two consecutive reported quarters with zero components firing
```

State machine per name: on each new availability date, recompute. WATCH → no
action, logged. CONFIRMED → the tradeable signal. The state persists (and the
position stays exited/flagged) until CLEAR. Financials are excluded from C2 only
(§2.5); C1 and C3 still apply to them.

### 5.3 What the backtest measures (long-only fund — no shorts)

1. **Event study:** market-adjusted forward returns of the CONFIRMED cohort at
   `h ∈ {63, 126, 189, 252}`. Primary: 126. A good sell signal shows
   **significantly negative** forward excess returns.
2. **Avoidance simulation:** assume the name is held. Sell at the signal's
   entry_price (§2.2), hold cash (0%) until CLEAR, rebuy at CLEAR's entry_price.
   Avoidance benefit per event = `−(forward excess return from signal to CLEAR)`
   minus round-trip cost (§6.3). Compare against hold-through.
3. **False-positive audit:** fraction of CONFIRMED signals that reach CLEAR
   within 2 quarters with the stock not underperforming — the "cried wolf" rate.

---

## 6. Expectation benchmarks

Every surprise above is measured against a mechanical expectation, because free
data has no analyst consensus. The benchmarks, and why:

| Benchmark | Definition | Used in | Why |
| --- | --- | --- | --- |
| Seasonal random walk (SRW) | E[EPS_q] = EPS_{q−4} | S1 primary, S2, S3 | The classic PEAD benchmark; zero estimation parameters; absorbs Indian seasonality that makes QoQ comparisons meaningless |
| SRW with drift | E[EPS_q] = EPS_{q−4} + mean of trailing m YoY deltas | S1 variant | Fairer to secular growers/decliners, at the cost of a noisy m-quarter mean estimate |
| Cross-sectional | Each event judged against its own month's cohort | S1-CS, S2-V2 | Needs no per-name history beyond 5 quarters; robust to short India series; captures market-wide earnings tides automatically |
| TTM trend | Quarter vs trailing-4-quarter aggregate (e.g. TTM OPM = Σ op-profit₄ / Σ sales₄) | Diagnostic columns only | Familiar to a human reader of the briefing; not used as a signal input because TTM dilutes the new information it is supposed to measure |

No analyst-estimate surrogate (e.g. price-implied expectations) is used: with
free data they add more assumptions than information. **[JUDGEMENT]**, but
consistent with the project brief.

---

## 7. Backtest methodology

### 7.1 Event study (the statistical core)

- For each event: market-adjusted return series `r_i(t) − r_bench(t)` from
  entry close (t=0) to `t = +252` trading days.
- CAR curves per signal × bucket, mean and median, with cross-sectional
  percentile bands (25/75).
- Significance: Fama–MacBeth-style — compute each monthly cohort's mean CAR at
  the primary horizon, then t-stat across cohorts (handles overlapping windows
  correctly). Report t-stat alongside every headline number.
- **Leakage diagnostic (US only):** also plot the pre-window `availability−20`
  to entry. If top-bucket names run up *before* availability systematically,
  information is leaking into prices ahead of filings and the post-entry CAR
  understates the anomaly; note it, do not trade on it. India's assumed dates
  make its pre-window uninformative — do not interpret it.

### 7.2 Portfolio-level simulation (the cost/turnover reality check)

Two simulations per validated signal:

**(a) Unconstrained signal portfolio.** Monthly rebalance to equal weight across
all names with an active signal (inside holding window, or state-true for
state-based exits). Cash when nothing is active; cash earns 0%. Records exposure
% over time. Purpose: raw net-of-cost alpha and monotonicity checks.

**(b) Fund-overlay simulation.** A hypothetical 25-name book mirroring the
fund's rules (3.8% target weight, 8% cap, 25% sector cap, 5% cash buffer,
one-way turnover budget 75%/yr enforced and measured exactly as
`src/risk/turnover.py` does):

- Buy-side signals (S1 top bucket, S2) are eligible only at the quarterly deep-
  review cadence (config `cadence.deep_review_months`) — this is how the fund
  can actually absorb them. Replacement rule: exit the weakest-scored holding,
  buy the highest-ranked signalled name not already held.
- Sell-side (S3 CONFIRMED) fires immediately on availability, replacing into the
  next best non-signalled candidate at the following deep review (cash until
  then) — matching how the production trigger will work (§8).
- Purpose: does the signal survive the fund's own constraints, and how much of
  the turnover budget does it consume?

### 7.3 Costs (from the project brief — fixed, not gridded)

| Leg | Round-trip | Applied as |
| --- | --- | --- |
| India (delivery) | 0.30% | 0.15% on each buy and each sell |
| US | 0.05% | 0.025% on each buy and each sell |

### 7.4 Benchmarks

- US: SPY adjusted close (total-return proxy).
- India: `^CRSLDX` (Nifty 500 TRI), matching production config.
- All event-study and sim returns are local-currency; the US leg is a research
  leg — the fund trades India only. FX is irrelevant to every conclusion.

### 7.5 Evaluation metrics (report all, per signal × variant × market)

- Event study: mean/median CAR at primary horizon, FM t-stat, hit rate (share of
  events with positive market-adjusted return), bucket monotonicity
  (top-to-bottom spread and rank correlation).
- Portfolio sims: CAGR, annualised excess over benchmark (geometric), Sharpe
  (rf = 0 — stated for cross-market comparability; excess-over-benchmark is the
  primary evidence, not absolute Sharpe), max drawdown, per-calendar-year
  excess-return table, hit rate per year, **annualised one-way turnover**
  ((buys + sells)/2 / avg NAV), average number of positions, average exposure.

### 7.6 Anti-lookahead rules (each must be a unit test in the code)

1. No fact with `availability_date ≥ entry_date` may enter any computation for
   that event. Standardisation windows use only quarters available strictly
   before.
2. US facts use the **earliest-`filed`** value per (tag, period) — never a later
   restatement (§1.1).
3. Entry is strictly after availability (§2.2), at close. No announcement-day
   returns in any tradeable result.
4. Parameters are never selected on data after the trades they govern (§7.7).
5. India standardisation denominators: same rule — trailing windows only.
6. LLM (§8): the brief contains no information after the report's availability
   date; temperature 0; raw responses logged.
7. Known-but-unfixable biases (not tests, disclosures): Screener restated
   history; current-constituent survivorship; see §7.8.

### 7.7 Walk-forward discipline (no full-sample grid winners)

- **US leg:** parameters are selected on rolling 5-year windows and applied to
  the following 1 year, stepped annually (expanding window after the first
  5-year block). Headline US results are the stitched out-of-sample path only.
  In-sample grid tables are reported for diagnosis and are never the evidence.
- **India leg:** **no parameter selection on India data at all.** The India
  depth (~12 quarters) cannot support walk-forward, and selecting on three years
  of one regime would be curve-fitting by construction. India runs with the
  parameters the US walk-forward selected (nearest grid cell). This makes India
  a genuine cross-market out-of-sample test — statistically thin, but honest.
- A parameter chosen "because it looked best on the full sample" invalidates the
  result. If the dev needs a rule: *selection data and evaluation data never
  overlap.*

### 7.8 Known biases and their directions (state these in the results memo)

| Bias | Mechanism | Direction on results |
| --- | --- | --- |
| India 45/60-day assumed availability | Entry happens later than the real announcement for most names; the announcement-day move (correlated with surprise sign) is missed entirely | **Understates** buy-side drift alpha and **understates** S3's avoidance value. Conservative — tradeable alpha is at least what we measure. **[JUDGEMENT]:** if Indian mid-caps run up on anticipation/leakage *before* results, part of the drift precedes even the real date, and the measured post-entry drift is smaller still — the assumption is conservative in more scenarios than not, but the pre-window cannot verify it (§7.1) |
| Survivorship (both legs) | Current constituents only; fallen/delisted names missing | Overstates absolute returns; cross-sectional spreads and per-year sign consistency are much less affected — that is why go/no-go bars lean on them |
| Screener restated history | Old quarters shown as restated today | Usually small for large caps; direction ambiguous; disclosed, not fixed |
| EDGAR pre-2015 gaps | Early XBRL coverage incomplete | Warmup period absorbs it; signals start 2015 |

---

## 8. LLM-as-analyst (S4: subsample study now, production later)

### 8.1 Subsample design

- 300 events: 150 US + 150 India, stratified by year × SUE tercile × sector
  (each stratum ≥ 5 events). Same entry/exit conventions as S1.
- Model: the fund's configured decision model (config `llm.model`), temperature
  0, one call per event, raw JSON logged. Optional second run with a frontier
  model for comparison if budget allows.
- Evaluation: (a) agreement with the mechanical verdict (Cohen's κ on
  beat/in_line/miss); (b) forward market-adjusted returns by LLM verdict bucket
  — the LLM only earns its place if its buckets **separate forward returns at
  least as well as the mechanical buckets do**; (c) does `thesis_impact` add
  anything over S3's mechanical CONFIRMED state?

### 8.2 What the LLM sees (the quarterly brief — nothing else)

- Company name, sector label, fiscal period label.
- The last min(12, available) quarters of: Sales, Operating Profit, OPM %, Net
  Profit, EPS — plus precomputed YoY deltas for each.
- No prices, no news, no peer data, no guidance text, no dates after the
  report's availability date.

### 8.3 System prompt (verbatim — implement exactly)

```
You are a senior fundamental equity analyst with 20 years of experience reading
quarterly results, first on the sell side and then at a long-only fund with a
low-turnover, thesis-driven process. You are reading one company's results
brief exactly as it stood on the day the results became public.

The brief below contains the company's recent quarterly financial history:
sales, operating profit, operating margin, net profit, and EPS, with
year-on-year changes. There are no analyst consensus estimates available; judge
the quarter against the company's own history and seasonality (compare each
quarter to the same quarter a year earlier).

Assess:
1. Whether the quarter is a beat, in line, or a miss relative to what the
   company's own trend and seasonality would have led a reasonable investor to
   expect.
2. The quality of the earnings: are profits backed by revenue growth and
   margins, or by one-offs, other income, or unsustainable margin levels?
3. Red flags: margin erosion, profit growth far ahead of revenue growth,
   deceleration, volatility, or anything that would make you distrust the
   numbers.
4. What this quarter does to the investment thesis for a long-term holder.

Hard rules:
- Use ONLY the numbers in this brief. Do not use any knowledge you have about
  this company, its industry, or events after the report date. If you recognise
  the company, pretend you do not.
- Never invent numbers. If the data is insufficient to judge, say so and lower
  your confidence.
- Be calibrated: most quarters are "in_line". Reserve "beat" and "miss" for
  quarters that clearly depart from the company's own trend.
- One paragraph of rationale, written the way you would write it in a fund's
  investment journal: specific numbers in, conclusion out.

Respond with a single JSON object and nothing else:
{
  "verdict": "beat" | "in_line" | "miss",
  "quality_of_earnings": <integer 0-10, where 10 = profit fully backed by
      revenue growth and stable-or-expanding margins, 0 = pure one-offs>,
  "red_flags": [<short strings; empty list if none>],
  "thesis_impact": "strengthens" | "intact" | "weakens" | "broken",
  "rationale": "<one paragraph>",
  "confidence": <float 0.0-1.0>
}
```

### 8.4 Schema validation

- Strict parse; any deviation (missing key, wrong enum, non-numeric score) → one
  retry with a "return valid JSON only" reminder; second failure → record as
  `null` verdict and count it in the failure rate.
- `quality_of_earnings` coerced to int in [0,10]; `confidence` clamped to [0,1].

### 8.5 Honest limitation

The no-world-knowledge instruction **only partially prevents lookahead**: model
training data almost certainly includes these companies and their outcomes
through the training cutoff, so S4 results on pre-cutoff events are contaminated
in the model's favour. Treat S4 as directional. The clean test only comes in
production, on results published after deployment.

---

## 9. Production integration (only for signals that clear §10)

Touch points: `src/models.py` (new `TriggerType`s), `src/risk/triggers.py` (new
evaluations), `src/decision/briefing.py` (new section), `src/fundamentals/`
(persist quarterly history), `config/settings.yaml` (new keys).

### 9.1 Mapping per signal

| Signal | Production form | Severity / placement |
| --- | --- | --- |
| **S3 deterioration** | New `TriggerType.EARNINGS_DETERIORATION` on holdings, fired on CONFIRMED | **REVIEW** — LLM weighs it with news context; never MANDATORY (scraped quarterly data is not reliable enough to auto-sell, and the existing `fundamental_deterioration` trigger is REVIEW for the same reason). WATCH state: briefing note only, no trigger |
| **S1 PEAD** | If validated: `TriggerType.EARNINGS_MISS` on holdings with extreme negative surprise (threshold from backtest) | **OPPORTUNITY** — early-warning complement to S3; suggested action HOLD with note. Buy-side: top-bucket prints on existing candidates appear as a timing flag in the briefing's candidates section — they may inform deep-review timing, never generate a trade alone |
| **S2 quality momentum** | Candidate/buy side only: an "earnings momentum" flag plus its z-score blended into the quant screen's growth factor at deep reviews (blend weight from backtest, ≤ 50%) | No new trigger type. All buys still need `min_buy_score` and the hard gates; S2 changes *which* candidates rank highest, not the buying rules |
| **S4 LLM verdict** | Generated for every holding when new results are detected in the weekly Screener refresh; shown in the briefing; `thesis_impact: broken` escalates an S3 WATCH to a REVIEW trigger | Advisory only; never a trigger on its own |

Note what is deliberately **not** proposed: a standalone PEAD trading strategy.
A 21–63-day holding signal is structurally incompatible with a 75%/yr turnover
budget; its production value is timing and early warning on a 25-name book, not
a trade stream.

### 9.2 Daily briefing — new "Quarterly earnings" section

For each holding (and each screened candidate) with results made available in
the last 30 days:

- Fiscal period, availability basis (actual date or 45/60-day assumption).
- Sales / net profit / EPS YoY %, OPM change in bps.
- SUE (TS and CS), cohort bucket.
- S3 component states (C1/C2/C3) and overall state (none/WATCH/CONFIRMED/CLEAR
  count).
- LLM verdict block (verdict, quality score, thesis impact, confidence, red
  flags, one-line rationale) when enabled.

### 9.3 Config keys (placeholders — filled from backtest results before `enabled: true`)

```yaml
earnings:
  enabled: false                      # master switch; flip only after §10 bars clear
  availability_lag_days_q123: 45      # SEBI default when no actual date scraped
  availability_lag_days_q4: 60
  exclude_financials_from_margin_signals: true
  sue:
    lookback_quarters: 8              # PLACEHOLDER from backtest (grid {4,6,8})
    min_history_quarters: 12          # derived from lookback choice
    surprise_threshold: 1.0           # PLACEHOLDER from backtest (grid {0,1,2})
    miss_trigger_threshold: -2.0      # PLACEHOLDER: SUE that fires EARNINGS_MISS
  quality_momentum:
    min_revenue_growth_yoy_pct: 5.0   # PLACEHOLDER (grid {0,5,10})
    min_opm_expansion_bps: 100        # PLACEHOLDER (grid {0,100,200})
    persistence_quarters: 2           # PLACEHOLDER (grid {1,2,3})
    growth_factor_blend_weight: 0.3   # PLACEHOLDER, cap 0.5
  deterioration:
    consecutive_profit_declines: 2    # PLACEHOLDER (grid {2,3})
    margin_compression_bps: -300      # PLACEHOLDER (grid {-200,-300,-500})
    confirm_components: 2             # components needed for CONFIRMED
    clear_after_clean_quarters: 2
  llm_verdict:
    enabled: false                    # after S4 subsample clears its bar
    escalate_thesis_broken: true      # WATCH + broken -> REVIEW trigger
```

---

## 10. Go/no-go criteria (per signal — concrete and falsifiable)

A signal **passes only if every bar for it clears**. A signal that fails its
bars **does not get built into production** — the results memo records the
failure, and any future revisit amends this spec with new evidence, not the bars.

**S1 PEAD:**
1. Top-minus-bottom bucket net CAR at +21d > 0, FM t-stat ≥ 2, in **both** markets.
2. Same-sign top-bucket excess in > 60% of calendar years in each market.
3. Fund-overlay sim: positive net excess CAGR, and incremental one-way turnover
   ≤ 15%/yr when overlaid on the 25-name book.

**S2 quality momentum:**
1. Top-quintile net excess > 0 in both markets at the primary 126d horizon, FM t ≥ 2.
2. Rough quintile monotonicity (top minus bottom > 0, and rank correlation of
   bucket vs CAR positive).
3. State-based exit variant beats time-based buy-and-hold cohort net of costs.
4. Fund-overlay turnover bar: same ≤ 15%/yr incremental.

**S3 deterioration (sell-side):**
1. CONFIRMED cohort forward 126d market-adjusted return **significantly negative**:
   mean ≤ −2% and FM t ≤ −2, in both markets.
2. Avoidance sim: net-of-cost avoidance benefit > 0 in > 60% of calendar years.
3. False-positive ("cried wolf") rate < 40% (§5.3.3).

**S4 LLM analyst:**
1. Cohen's κ vs mechanical verdict ≥ 0.35 (roughly ≥ 65% raw agreement).
2. LLM verdict buckets separate 63d forward returns at least as well as the
   mechanical buckets (beat-bucket minus miss-bucket spread ≥ mechanical spread).
3. Fails either → production LLM verdicts are not built; the mechanical signals
   stand or fall on their own bars.

**Standing bar for anything buy-side:** implied incremental turnover must be
compatible with the 75%/yr budget when overlaid on a 25-name book (operationalised
as ≤ 15%/yr above, i.e. at most one-fifth of the budget consumed by earnings
signals, leaving room for the existing six sell disciplines).

---

## 11. Honest register of judgement calls

1. **India as pure out-of-sample with US-frozen parameters** — chosen because 12
   quarters cannot support walk-forward; the alternative (India-only tuning) is
   curve-fitting a single regime. If the US and India markets genuinely differ
   in anomaly structure, this design will understate India performance.
2. **45/60-day availability default** — conservative by construction; §7.8
   argues the direction of bias but cannot prove it with assumed dates.
3. **Cross-sectional SUE as India's primary** — trades the classic estimator for
   one that survives short histories; it measures "surprising vs peers this
   month", which is related to but not identical with "surprising vs own history".
4. **Excluding financials from margin legs** — clean data beats complete coverage.
5. **Monthly cohorts, rf = 0 Sharpe, SPY as US total-return proxy** — simplicity
   chosen over precision where the precision was not decision-relevant.
6. **S4's contamination** — acknowledged in §8.5; no design fix exists with
   today's models.
