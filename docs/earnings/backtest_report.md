# Quarterly Earnings Backtest — Results Memo

Generated 2026-08-01 04:05. Spec: `docs/earnings/backtest_spec.md` v1.0. All US headline numbers are stitched walk-forward out-of-sample (§7.7); all India numbers use US-frozen parameters with zero India-side tuning.

---

## Market: US

Events evaluated: 25,725 across 499 symbols. Exclusion ledger: `{'us_pre_xbrl_lag_rows': 6187, 'entry_halt_or_no_price': 89, 'growth_base_noise': 297}`

### S1 PEAD (primary variant: SUE-TS, horizon +21d, buckets=10)

**SUE-ts** — spread top−bottom 0.14% (FM t = 0.30, 119 cohorts); top-bucket mean 0.38%, hit 50.9%, positive years 64%; n_top=1321, n_bottom=953

Walk-forward choices (winner per test year):

| test_year | m | thr |
|-----------|---|-----|
| 2015      | 4 | 1   |
| 2016      | 4 | 1   |
| 2017      | 4 | 1   |
| 2018      | 8 | 1   |
| 2019      | 8 | 1   |
| 2020      | 8 | 1   |
| 2021      | 4 | 2   |
| 2022      | 6 | 2   |
| 2023      | 4 | 2   |
| 2024      | 6 | 2   |
| 2025      | 6 | 2   |

Robustness +42d: spread 0.15% (FM t 0.26)
Robustness +63d: spread 0.40% (FM t 0.51)

**SUE-ts_drift** — spread top−bottom 0.80% (FM t = 2.07, 123 cohorts); top-bucket mean 0.47%, hit 51.1%, positive years 64%; n_top=1377, n_bottom=1262

Walk-forward choices (winner per test year):

| test_year | m | thr |
|-----------|---|-----|
| 2015      | 6 | 0   |
| 2016      | 6 | 0   |
| 2017      | 4 | 0   |
| 2018      | 4 | 0   |
| 2019      | 4 | 1   |
| 2020      | 4 | 1   |
| 2021      | 4 | 1   |
| 2022      | 4 | 1   |
| 2023      | 4 | 1   |
| 2024      | 4 | 0   |
| 2025      | 4 | 2   |

**SUE-cs** — spread top−bottom 0.78% (FM t = 1.19, 113 cohorts); top-bucket mean 1.52%, hit 54.2%, positive years 73%; n_top=699, n_bottom=682

Walk-forward choices (winner per test year):

| test_year | thr |
|-----------|-----|
| 2015      | 1   |
| 2016      | 1   |
| 2017      | 2   |
| 2018      | 2   |
| 2019      | 1   |
| 2020      | 1   |
| 2021      | 1   |
| 2022      | 1   |
| 2023      | 1   |
| 2024      | 1   |
| 2025      | 1   |

![S1 CAR us](us_s1_car.png)

US leakage diagnostic: top-bucket pre-window (availability−20 → entry) mean CAR drift 0.62%. Pre-announcement run-up would mean the post-entry CAR understates the anomaly (§7.1); noted, not traded.

### S2 Quality earnings momentum (primary horizon +126d)

**V1** — firing cohort mean 4.12% (FM t = 3.26, 132 cohorts), hit 52.0%, positive years 100%, n=2213; state-exit mean 7.75% vs time-exit 4.12% (state beats time: True)

| test_year | theta_rev | theta_opm_bps | persistence |
|-----------|-----------|---------------|-------------|
| 2015      | 0         | 0             | 1           |
| 2016      | 0         | 0             | 1           |
| 2017      | 0         | 0             | 1           |
| 2018      | 0         | 0             | 2           |
| 2019      | 0         | 0             | 2           |
| 2020      | 0.05      | 200.0         | 2           |
| 2021      | 0.1       | 200.0         | 2           |
| 2022      | 0.05      | 200.0         | 2           |
| 2023      | 0.1       | 200.0         | 1           |
| 2024      | 0.1       | 200.0         | 1           |
| 2025      | 0.1       | 200.0         | 1           |

**V2** — firing cohort mean 4.70% (FM t = 3.48, 130 cohorts), hit 51.0%, positive years 91%, n=1915; state-exit mean 9.79% vs time-exit 4.70% (state beats time: True)

| test_year | persistence |
|-----------|-------------|
| 2015      | 1           |
| 2016      | 1           |
| 2017      | 1           |
| 2018      | 3           |
| 2019      | 3           |
| 2020      | 2           |
| 2021      | 2           |
| 2022      | 2           |
| 2023      | 2           |
| 2024      | 2           |
| 2025      | 3           |

V2 quintile monotonicity: top−bottom 1.48%, rank corr -0.40

| bucket | mean    | median  | count   |
|--------|---------|---------|---------|
| 0      | 0.0358  | 0.0016  | 3,400.0 |
| 1      | 0.0059  | -0.0042 | 3,334.0 |
| 2      | 0.001   | -0.005  | 3,332.0 |
| 3      | -0.0038 | -0.0066 | 3,336.0 |
| 4      | 0.0209  | -0      | 3,261.0 |

![S2 CAR us](us_s2_car.png)

### S3 Earnings deterioration (CONFIRMED cohort, primary +126d)

Confirmed mean 0.96% (FM t = 1.57, 107 cohorts), n=796; avoidance benefit mean -3.49%, positive-benefit years 36%, cried-wolf rate 10.5% (censored-no-CLEAR events: 81)

| test_year | consecutive_declines | margin_compression_bps | cumulative_c2 |
|-----------|----------------------|------------------------|---------------|
| 2015      | 3                    | 500.0                  | True          |
| 2016      | 3                    | 200.0                  | True          |
| 2017      | 3                    | 200.0                  | True          |
| 2018      | 3                    | 500.0                  | True          |
| 2019      | 3                    | 200.0                  | True          |
| 2020      | 3                    | 200.0                  | True          |
| 2021      | 3                    | 200.0                  | True          |
| 2022      | 3                    | 200.0                  | True          |
| 2023      | 3                    | 200.0                  | True          |
| 2024      | 3                    | 200.0                  | True          |
| 2025      | 3                    | 300.0                  | True          |

Robustness +63d: mean 0.71% (FM t 1.09)
Robustness +189d: mean 2.36% (FM t 1.58)
Robustness +252d: mean 2.11% (FM t 1.81)

![S3 CAR us](us_s3_car.png)

### Portfolio simulations

| sim                 | CAGR   | excess CAGR (geo) | Sharpe(rf=0) | maxDD  | turnover (P+S)/2 | turnover min-churn | avg pos | avg exposure |
|---------------------|--------|-------------------|--------------|--------|------------------|--------------------|---------|--------------|
| s1_unconstrained    | 15.63% | 1.94%             | 0.77         | -51.7% | 1140.2%          | 1135.6%            | 10.1    | 98%          |
| s1_overlay          | 14.90% | 1.29%             | 0.88         | -30.0% | 29.7%            | 25.2%              | 25.0    | 95%          |
| s1_overlay_baseline | 18.56% | 4.52%             | 0.96         | -33.9% | 18.6%            | 14.0%              | 25.0    | 95%          |
| s2_unconstrained    | 17.32% | 3.43%             | 0.94         | -35.6% | 89.9%            | 85.6%              | 143.8   | 99%          |
| s2_overlay          | 18.83% | 4.76%             | 0.90         | -37.6% | 84.8%            | 79.8%              | 25.0    | 95%          |
| s2_overlay_baseline | 18.56% | 4.52%             | 0.96         | -33.9% | 18.6%            | 14.0%              | 25.0    | 95%          |
| s3_overlay          | 25.69% | 10.81%            | 1.24         | -36.4% | 40.9%            | 35.8%              | 24.2    | 92%          |
| s3_overlay_baseline | 18.56% | 4.52%             | 0.96         | -33.9% | 18.6%            | 14.0%              | 25.0    | 95%          |

S1 overlay: excess CAGR 1.29% vs baseline 4.52%; incremental turnover 11.3%/yr (min-churn convention).

![s1 overlay us](us_s1_overlay_nav.png)

S2 overlay: excess CAGR 4.76% vs baseline 4.52%; incremental turnover 65.9%/yr (min-churn convention).

![s2 overlay us](us_s2_overlay_nav.png)

S3 overlay: excess CAGR 10.81% vs baseline 4.52%; incremental turnover 21.8%/yr (min-churn convention).

![s3 overlay us](us_s3_overlay_nav.png)

---

## Market: INDIA

Events evaluated: 2,940 across 250 symbols. Exclusion ledger: `{'entry_halt_or_no_price': 233, 'growth_base_noise': 24}`

### S1 PEAD (primary variant: SUE-CS, horizon +21d, buckets=5)

**SUE-ts** — spread top−bottom 0.16% (FM t = n/a, 5 cohorts); top-bucket mean 0.79%, hit 49.5%, positive years 75%; n_top=210, n_bottom=124

**SUE-ts_drift** — spread top−bottom 0.36% (FM t = n/a, 5 cohorts); top-bucket mean 0.75%, hit 48.3%, positive years 50%; n_top=205, n_bottom=201

**SUE-cs** — spread top−bottom 0.67% (FM t = 0.35, 9 cohorts); top-bucket mean 0.53%, hit 48.9%, positive years 33%; n_top=92, n_bottom=84

Robustness +42d: spread 3.49% (FM t 1.47)
Robustness +63d: spread 5.03% (FM t 2.48)

![S1 CAR india](india_s1_car.png)

### S2 Quality earnings momentum (primary horizon +126d)

**V1** — firing cohort mean 3.36% (FM t = 2.38, 13 cohorts), hit 46.6%, positive years 67%, n=876; state-exit mean 8.12% vs time-exit 3.36% (state beats time: True)

**V2** — firing cohort mean 4.75% (FM t = 1.57, 6 cohorts), hit 44.4%, positive years 33%, n=238; state-exit mean 12.34% vs time-exit 4.75% (state beats time: True)

V2 quintile monotonicity: top−bottom 3.75%, rank corr -0.60

| bucket | mean   | median  | count |
|--------|--------|---------|-------|
| 0      | 0.0717 | -0.0116 | 260.0 |
| 1      | 0.0449 | 0.0002  | 258.0 |
| 2      | 0.0192 | -0.0006 | 257.0 |
| 3      | 0.0239 | -0.0108 | 258.0 |
| 4      | 0.0342 | 0.0099  | 256.0 |

![S2 CAR india](india_s2_car.png)

### S3 Earnings deterioration (CONFIRMED cohort, primary +126d)

Confirmed mean 2.98% (FM t = n/a, 5 cohorts), n=92; avoidance benefit mean -12.39%, positive-benefit years 0%, cried-wolf rate 13.8% (censored-no-CLEAR events: 54)

Robustness +63d: mean 2.48% (FM t 2.07)
Robustness +189d: mean 14.13% (FM t n/a)
Robustness +252d: mean 18.27% (FM t n/a)

![S3 CAR india](india_s3_car.png)

### Portfolio simulations

| sim                 | CAGR   | excess CAGR (geo) | Sharpe(rf=0) | maxDD  | turnover (P+S)/2 | turnover min-churn | avg pos | avg exposure |
|---------------------|--------|-------------------|--------------|--------|------------------|--------------------|---------|--------------|
| s1_unconstrained    | 10.94% | 9.50%             | 0.87         | -11.4% | 266.5%           | 235.0%             | 3.5     | 33%          |
| s1_overlay          | 2.71%  | 1.37%             | 0.24         | -24.3% | 32.7%            | 16.9%              | 25.0    | 94%          |
| s1_overlay_baseline | 0.63%  | -0.68%            | 0.12         | -20.2% | 23.9%            | 8.1%               | 25.0    | 94%          |
| s2_unconstrained    | 11.44% | 10.00%            | 0.70         | -22.3% | 121.7%           | 104.9%             | 103.3   | 100%         |
| s2_overlay          | 11.91% | 10.45%            | 0.68         | -24.5% | 100.6%           | 85.0%              | 25.0    | 95%          |
| s2_overlay_baseline | 0.63%  | -0.68%            | 0.12         | -20.2% | 23.9%            | 8.1%               | 25.0    | 94%          |
| s3_overlay          | 6.00%  | 4.62%             | 0.44         | -20.1% | 37.1%            | 17.5%              | 24.5    | 92%          |
| s3_overlay_baseline | 0.63%  | -0.68%            | 0.12         | -20.2% | 23.9%            | 8.1%               | 25.0    | 94%          |

S1 overlay: excess CAGR 1.37% vs baseline -0.68%; incremental turnover 8.8%/yr (min-churn convention).

![s1 overlay india](india_s1_overlay_nav.png)

S2 overlay: excess CAGR 10.45% vs baseline -0.68%; incremental turnover 77.0%/yr (min-churn convention).

![s2 overlay india](india_s2_overlay_nav.png)

S3 overlay: excess CAGR 4.62% vs baseline -0.68%; incremental turnover 9.5%/yr (min-churn convention).

![s3 overlay india](india_s3_overlay_nav.png)

---

## India scraped-dates sensitivity (§1.2)

Events with a scraped actual announcement date: 45; of those, actual date LATER than the 45/60-day assumption: 33 (73.3%) — the assumption is too aggressive for those events (§1.2 flag).

S1-CS top quintile under **assumed** dates: n=1, mean -3.59% (FM t n/a)

S1-CS top quintile under **scraped** dates: n=3, mean n/a (FM t n/a)

Coverage caveat: scraped dates exist only for a fraction of names and mostly for the most recent quarters — this subsample is small and recent-regime-only; it is a directional check on the lag assumption, not a full re-estimate.

---

## §10 Go/No-Go verdicts

Bars quoting "both markets" require the US walk-forward OOS path AND the India frozen-parameter run to clear independently. The overlay bars are evaluated on each market's own 25-name book; India's book is thin (~2 years) — flagged where binding.

### S1 PEAD
- Bar 1 — top−bottom net CAR@21d > 0 and FM t ≥ 2, both markets: **FAIL** — us: spread 0.14%, FM t 0.30; india: spread 0.67%, FM t 0.35
- Bar 2 — same-sign top-bucket excess in >60% of years, each market: **FAIL** — us: 64%; india: 33%
- Bar 3 — overlay: positive net excess CAGR and incremental turnover ≤ 15%/yr: **PASS** — us: excess 1.29%, incremental turnover 11.3%/yr; india: excess 1.37%, incremental turnover 8.8%/yr

**S1 overall: FAIL**

### S2 quality momentum
- Bar 1 (V1) — firing/top-quintile net excess > 0 @126d, FM t ≥ 2, both markets: **PASS** — us: mean 4.12%, t 3.26; india: mean 3.36%, t 2.38
- Bar 1 (V2) — firing/top-quintile net excess > 0 @126d, FM t ≥ 2, both markets: **FAIL** — us: mean 4.70%, t 3.48; india: mean 4.75%, t 1.57
- Bar 2 — quintile monotonicity (top−bottom > 0 and rank corr > 0): **FAIL** — us, india
- Bar 3 — state-based exit beats time-based, net of costs: **PASS** — us: state 7.75% vs time 4.12%; india: state 8.12% vs time 3.36%
- Bar 4 — overlay incremental turnover ≤ 15%/yr: **FAIL** — us: excess 4.76%, incremental turnover 65.9%/yr; india: excess 10.45%, incremental turnover 77.0%/yr

**S2 overall: FAIL**

### S3 deterioration (sell-side)
- Bar 1 — CONFIRMED @126d mean ≤ −2% and FM t ≤ −2, both markets: **FAIL** — us: mean 0.96%, t 1.57; india: mean 2.98%, t n/a
- Bar 2 — avoidance benefit > 0 in >60% of years: **FAIL** — us: 36%; india: 0%
- Bar 3 — cried-wolf rate < 40%: **PASS** — us: 10.5%; india: 13.8%

**S3 overall: FAIL**

S4 bars: not evaluated — study not run (no API key).

---

## What surprised us (notes for the reviewer)

- **S1's tails are U-shaped in arithmetic market-adjusted terms.** The US bottom (worst-SUE) decile's pooled net CAR@21d is 0.53% — positive, not negative — with 2020 the biggest contributor at 3.22%. Extreme-surprise names are high-attention, high-beta names; in junk-rally years the bad-news tail out-bounces the good-news tail, so the long/short spread never reaches significance even though the top bucket's own sign is right. Own-history SUE is also not a consensus surprise: several mega-cap prints sit in the worst SUE decile and rallied hard post-print on guidance/narrative.

- **S3 CONFIRMED cohorts drift UP, in both markets** (US 0.96%, India 2.98% net @126d), so the avoidance sim is worth -3.49% (US) / -12.39% (India) — i.e. avoiding deteriorating names cost money in this sample. The state machine is not crying wolf (10% US false-positive rate): it confirms genuine deterioration, but by confirmation time the bad news is already priced — mechanically-confirmed deteriorators behave like late-cycle value names and mean-revert upward. S3 as specified identifies deterioration; it does not predict further downside.

- **S2 is the only signal with both-market significance (V1), and the exit rule carries it**: state-based exit roughly doubles the cohort mean vs the fixed 126d exit (US 7.75% vs 4.12%). But the same persistence that makes it work makes the overlay churn: quarterly refills rotate the 25-name book far past the 15%/yr incremental-turnover bar. A production version would need slower entry (longer persistence, hysteresis on exit) — that is a new variant, not a parameter tweak.

- **India's evidence base is thin by construction**: ~13 quarterly observations per name means 5–13 Fama–MacBeth cohorts per signal (t-stats honestly n/a), and the scraped-dates subsample (§1.2's 'only real test' of the 45/60-day lag) has too few events to bite. Treat every India number as directional, not estimated.

---

## Calibrated production thresholds (US walk-forward modal)

```yaml
earnings:
  enabled: false   # flips only for signals whose §10 bars cleared
  sue:
    lookback_quarters: 4
    min_history_quarters: 8
    surprise_threshold: 1.0
    miss_trigger_threshold: -2.0
  quality_momentum:
    min_revenue_growth_yoy_pct: 0.0
    min_opm_expansion_bps: 0
    persistence_quarters: 1
    growth_factor_blend_weight: 0.3
  deterioration:
    consecutive_profit_declines: 3
    margin_compression_bps: -200
    cumulative_two_year_c2: True
    confirm_components: 2
    clear_after_clean_quarters: 2
```

These are the modal cells across the 11 walk-forward folds; per-fold choices and their spread are in the fold CSVs. `enabled` stays false per the spec until the relevant §10 bars clear.

---

## Deviations from spec & judgement calls (implementation register)

- Spec §1.1 names the revenue tag `...ExcludingAssessedCost` — the actual XBRL tag is `...ExcludingAssessedTax`; the data layer used the real tag with the spec's fallback order.
- Walk-forward selection fixes each signal's PRIMARY horizon (S1 +21d, S2/S3 +126d) and grids the rest; other horizons are robustness at the selected parameters. A jointly-selected horizon would mix horizons along the stitched OOS path and make the §10 bars (defined at the primary horizon) uninterpretable.
- Revenue SUE (§3.1 robustness) evaluated at modal primary parameters only.
- India fiscal Q4 for the 60-day lag = March quarter-ends (dominant NSE convention); non-March fiscal-year names get 45d on their true Q4 — mildly aggressive for a handful of names, disclosed.
- S3 confirmation evaluates C1-at-c=3 as a standalone confirmer alongside the grid's c (literal §5.2 reading); C2 cumulative variant matches q−8 by sequence position with a sanity band (spec silent).
- Overlay mechanics where §7.2(b) is silent: initial book = 25 highest trailing-turnover names at sim start; unsignalled holdings score 0 (neutral) in the replacement ranking; S3 refills ranked by latest SUE-CS; review-day trades at that day's close; S3 exits are mandatory and bypass the turnover budget (src/risk/turnover.py semantics), deep-review trades are discretionary and budget-blocked.
- Event-study excess returns are arithmetic (event-study standard); portfolio-sim excess is geometric per §7.5.
- S2-V2's state-based exit re-checks a single name's legs (quintile re-ranking ex-post is not meaningful for one name) — approximation flagged; V1's state exit is exact.
- India financials exclusion uses the single NSE 'Financial Services' label (broader than banks/NBFC/housing — includes insurers/AMCs whose OPM is equally meaningless); count reported in the market sections.
- EPS Basic fallback (spec §1.1 order) was added to the EDGAR parser and the US cache re-parsed; eps coverage ~95%.
- SUE-TS trailing windows take the m most recent non-NaN availability-guarded YoY deltas (spec: no partial windows).
- US signals span 2015–2025 per §1.1 (folds train from 2010); 2026 data is unused for US headline numbers. India uses all available quarters.
- Turnover is enforced with the production min(buys,sells)/avg-NAV convention and ALSO reported as §7.5's (buys+sells)/2; the two differ little in these books.
- Known-bias register (spec §7.8) applies unchanged: survivorship overstates absolute returns in both legs; India 45/60-day assumption understates buy-side drift and S3 avoidance value (conservative); Screener restated history disclosed; S4 contamination deferred.

---

## S4 LLM-as-analyst study

**Not run — no `OPENAI_API_KEY` on this machine.** The harness (`src/backtest/llm_study.py`) implements the §8 300-event stratified sampler, the verbatim system prompt, strict JSON schema validation with one retry, raw-response logging, and a cost-estimate printout; it refuses to start without a key. §8.5's training-data contamination caveat stands for any future run.
