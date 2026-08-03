# Quarterly Earnings Backtest — Analyst Review

**To:** Portfolio Manager  
**From:** Fundamental Equity (author, `backtest_spec.md` v1.0)  
**Date:** 2026-08-01  
**Re:** Phase 2 results memo (`data/backtest/report/report.md`) — hostile review against §10 bars

---

## 1. Executive verdict

**No signal clears the binding §10 go/no-go bars.** S1, S2, and S3 each fail at least one non-negotiable criterion in both the event study and the fund-overlay tests as specified. The quant's verdicts match the spec; I am not reopening implementation disputes the memo already documents.

The project is **not dead**, but **production integration as originally scoped is.** The earnings module should not flip `earnings.enabled: true` or emit tradeable triggers on the back of this evidence. What survives is a **narrower, advisory layer**: persist quarterly facts, surface them in the daily briefing, and optionally fold a heavily gated S2 read-through into deep-review candidate ranking — with no new sell triggers and no PEAD sleeve.

S3's failure is the most consequential for this fund: the highest-priority sell-side hypothesis (mechanical deterioration → forward underperformance) **inverts**. That does not invalidate monitoring deterioration; it invalidates **automated exit on mechanical CONFIRMED state alone**. S2 shows genuine cross-sectional alpha in V1 at +126d (US 4.12%, FM t = 3.26; India 3.36%, t = 2.38) but is **operationally incompatible** with a 75%/yr turnover budget and 16-month holding period once overlaid (65.9–77.0%/yr incremental turnover). S1 confirms what the spec already warned: standalone PEAD is a timing flag at best, not a strategy — and even the flag lacks statistical spine on mechanical surprise (US primary spread 0.14%, t = 0.30).

**Bottom line for the book:** keep `earnings.enabled: false`; build the data pipe and briefing if we want earnings-aware humans in the loop; do not proceed to Phase 2 production triggers.

---

## 2. Per-signal review

### S1 — PEAD

| Bar | Result | Verdict |
| --- | --- | --- |
| Spread + FM t @21d, both markets | US 0.14%, t = 0.30; India (CS) 0.67%, t = 0.35 | **FAIL** |
| Top-bucket positive years >60% | US 64%; India 33% | **FAIL** (India; US marginal pass) |
| Overlay excess + turnover ≤15%/yr | US +1.29%, 11.3%/yr; India +1.37%, 8.8%/yr | **PASS** |

**Diagnosis:** Predominantly a **true economic null** on the spec's mechanical surprise definition, amplified by **design/data limitations**, not an implementation artifact.

- **Economic null:** At +21d the US top−bottom spread is 0.14% with FM t = 0.30 — indistinguishable from noise across 119 cohorts. SUE-TS-drift reaches t = 2.07 on a 0.80% spread, but that variant was not the primary bar and still fails India. The quant's U-shaped tail observation is real: US bottom-decile net CAR@21d is **+0.53%**, so "worst surprise" names do not underperform — they are often high-beta, narrative-driven names where mechanical YoY EPS miss ≠ consensus miss.
- **Design flaw (spec-acknowledged):** Mechanical SRW surprise is not analyst consensus surprise. Mega-cap guidance beats can sit in the worst SUE decile and rally — the spec flagged this in §6; the data confirms it dominates short-horizon spreads.
- **Data limitation:** India runs on SUE-CS with **9 FM cohorts** (t = 0.35); +63d spread widens to 5.03% (t = 2.48) but that is outside the primary bar. The 45/60-day lag and restated Screener history **understate** buy-side drift per §7.8 — so if anything, the failure is conservative, not an artifact of aggressive dating.
- **Leakage (US only):** Pre-window top-bucket drift +0.62% suggests some information is priced before `filed`; post-entry CAR **understates** tradeable PEAD. Even granting that, t = 0.30 leaves no anomaly to harvest at +21d after entry.

**Overlay PASS is a red herring for production:** positive incremental excess with 11.3%/yr turnover reflects a **mis-specified use case** — overlay buys top-bucket names at deep review, which is not standalone PEAD trading but still failed bars 1–2. Do not treat overlay PASS as rehabilitation of S1.

---

### S2 — Quality earnings momentum

| Bar | Result (V1 primary) | Verdict |
| --- | --- | --- |
| Firing cohort @126d, FM t ≥ 2, both markets | US 4.12%, t = 3.26; India 3.36%, t = 2.38 | **PASS** |
| Quintile monotonicity | US top−bottom 1.48% but rank corr **−0.40**; India top−bottom 3.75%, rank corr **−0.60** | **FAIL** |
| State exit beats time exit | US 7.75% vs 4.12%; India 8.12% vs 3.36% | **PASS** |
| Overlay turnover ≤15%/yr | US 65.9%/yr; India 77.0%/yr | **FAIL** |

**Diagnosis:** **Real cross-sectional edge in the firing cohort (V1)**, undermined by **ranking structure** and **fund constraints** — not a null, not primarily a data bug.

- **Economic content:** V1's AND rule with walk-forward-selected thresholds (often θ_rev = 0%, θ_opm = 0%, persistence = 1–2) identifies names with simultaneous revenue and margin improvement. Mean +126d excess of 4.12% (US) / 3.36% (India) with FM t above 2 is credible. State-based exit doubling the cohort mean (US 7.75%) confirms the **thesis persistence** story — you want to hold while the combination holds.
- **Monotonicity FAIL is a spec/design tension, not noise:** V2 quintiles show bucket 4 (top composite z) at 2.09% US mean while bucket 0 is 3.58% — an inverted-U. The signal works as a **binary filter**, not a linear score. Blending z-scores into a continuous factor assumes monotonicity the data reject. That is a **design flaw in how we planned production (§9.1 blend)**, not proof the underlying economics are fake.
- **Turnover FAIL is operational truth:** Quarterly persistence with state-based exit rotates the overlay book at 65.9%/yr (US) and 77.0%/yr (India) — **4–5× the 15%/yr bar** and nearly the entire 75%/yr fund budget. The same persistence that generates alpha in the event study **is** the churn in the 25-name book. Slower entry (persistence ≥ 3, hysteresis) would be a **new variant** requiring a new backtest, not a config tweak.
- **India:** V1 passes t-bar on **13 cohorts** — directionally supportive, not independently confirmatory (see §6).

---

### S3 — Earnings deterioration (sell-side)

| Bar | Result | Verdict |
| --- | --- | --- |
| CONFIRMED @126d mean ≤ −2%, FM t ≤ −2 | US **+0.96%**, t = +1.57; India **+2.98%**, t n/a | **FAIL** (wrong sign) |
| Avoidance benefit >0 in >60% of years | US **36%**; India **0%** | **FAIL** |
| Cried-wolf rate <40% | US 10.5%; India 13.8% | **PASS** |

**Diagnosis:** **True economic phenomenon, wrong trading hypothesis** — largely **spec/thesis error**, not bad code. Data limitations **understate** avoidance value per §7.8; the measured result is **positive** forward drift, so conservative dating cannot explain the failure away.

- The state machine **works as specified:** cried-wolf 10.5% (US) means confirmations are real deterioration patterns, not noise triggers.
- The trade hypothesis **fails:** CONFIRMED names drift **up** +0.96% (US) / +2.98% (India) @126d. Avoidance costs −3.49% (US) / −12.39% (India) per event. By the time two of {C1,C2,C3} align (or C1×3), the market has **re-priced** the bad quarter; many names are **late-cycle value** with mean-reversion tailwinds.
- **Not an implementation artifact:** Walk-forward consistently selects strict parameters (c = 3, cumulative C2, 200–500 bps compression). Loosening would fire earlier (more "informational") but would not fix the sign without evidence.
- **India:** 92 CONFIRMED events, 5 FM cohorts, 0% positive avoidance years — directional only, but **same sign as US**, which is what matters for the thesis.

---

## 3. The S3 surprise — CONFIRMED deteriorators drift up

**Best fundamental explanation:** Mechanical CONFIRMED captures **lagging, multi-quarter fundamental decay that is already visible to the market** before our entry date (especially India: entry = assumed availability + 1 day, often 45+ days post quarter-end). The fund's components — consecutive YoY profit declines, margin compression, revenue deceleration — are **slow, accounting-visible facts**, not forward-looking estimate revisions. By confirmation:

1. **The bad quarter(s) have been telegraphed** (guidance cuts, peer read-through, multiple compression).
2. **Confirmation selects survivors** — names still in Nifty 500 / S&P 500 after sustained deterioration tend to be **cheap, contrarian, or turn-around candidates** where bad news triggers value-buying, not continued liquidation.
3. **Long-only avoidance sim is asymmetric:** we simulate selling into cash and rebuying at CLEAR; CLEAR requires two clean quarters — often **after** the bounce off trough valuations. We crystallize the bottom and miss the recovery.

**Recommendation:** **Do not abandon deterioration monitoring; abandon deterioration-as-automated-sell.**

| Option | View |
| --- | --- |
| Auto-sell on CONFIRMED | **Reject** — evidence is unambiguous (positive forward excess, negative avoidance). |
| Redesign (valuation + deterioration) | **Worth one targeted research pass**, not immediate build. Hypothesis: CONFIRMED **and** stretched valuation (e.g. premium vs own history / peers) may still predict relative underperformance. That was **not tested**; it is a new signal. |
| Informational only | **Default path:** WATCH/CONFIRMED in briefing → LLM + PM judgment at deep review or ad hoc REVIEW. Aligns with existing `fundamental_deterioration` REVIEW severity in §9.1. |

Do **not** ship `TriggerType.EARNINGS_DETERIORATION` on mechanical CONFIRMED. If anything, CONFIRMED should **delay** automatic sells from other disciplines until the PM reads the quarter — the data say forced exit **destroys** value.

---

## 4. S2 as the "closest" signal — one production path

S2 V1 is the only variant with **both-market FM t ≥ 2** on the firing cohort and state-exit beating time-exit. It still **fails overall** on monotonicity and overlay turnover.

**Recommended path (single):** **Advisory + optional deep-review candidate tilt — not a trigger, not a standalone factor blend at 30% weight.**

Rationale against fund constraints:

- **75%/yr turnover, ~16-month hold:** S2 overlay consumes **66–77%/yr incremental** turnover — effectively a quarterly rebalance strategy grafted onto a semiannual deep-review process. That is structurally incoherent; the fund would become an earnings-momentum closet index.
- **Monotonicity failure:** Production §9.1 proposed blending composite z into the growth factor. Quintile rank correlations of **−0.40 (US)** and **−0.60 (India)** forbid a linear blend. At most, a **binary flag** ("quality momentum active this quarter") could nudge human ranking at deep review — equivalent to a footnote, not a 30% quant weight.
- **State exit alpha (7.75% US) is real but unmappable** without re-specifying entry cadence (deep review only, no intra-quarter churn) and re-backtesting.

**Reject entirely as an automated buy signal.** **Accept as briefing context** for candidates already passing hard gates: "Revenue + margin momentum: yes/no, persistence k." PM decides at deep review. No change to `min_buy_score`, no new trigger type.

---

## 5. S1 / PEAD — standalone trading and satellite sleeve

**Confirm the quant's conclusion:** Standalone PEAD **trading** is incompatible with this fund **regardless of statistical significance** — the spec said so in §9, and the 1140%/yr unconstrained S1 turnover in the US sim is the quantitative proof. Even a significant +21d spread would be arbed away by costs and book structure at quarterly deep-review cadence.

**On significance:** Primary S1 **does not** achieve significance anyway (US t = 0.30; India t = 0.35). Overlay PASS (US +1.29% excess, 11.3%/yr incremental) does **not** rescue PEAD as a signal — it is a weak, bar-1/bar-2-failing overlay on a mis-defined surprise.

**High-turnover satellite sleeve (user question):** Theoretically, a **separate mandate** (e.g. 5–10% of capital, 200%+/yr turnover, long top decile / short bottom or long-only top decile with weekly rebalance) could harvest PEAD if the anomaly existed. This backtest provides **no evidence** to fund that sleeve on mechanical SUE: spreads near zero, bottom tail positive, India primary horizon insignificant. A satellite would also **contradict** the fund's stated edge (fundamental, low turnover, India long-only). **Do not allocate research budget to a PEAD sleeve** unless we obtain **consensus surprise data** (estimates) and re-run — mechanical SRW is the wrong object.

**Production role for S1 (if any):** **`EARNINGS_MISS` on extreme negative SUE** as OPPORTUNITY/HOLD note on **existing holdings** only — but with miss threshold −2.0 and no significant miss bucket alpha, even that is **optional briefing text**, not a validated trigger. I would omit it until a consensus-based re-test.

---

## 6. India leg credibility

| Fact | Implication |
| --- | --- |
| ~13 quarters depth, 2,940 events / 250 symbols | ~13 observations per name — **insufficient for walk-forward or stable FM inference** |
| 5–13 FM cohorts per signal; many t = n/a | India **cannot pass or fail t-bars reliably**; PASS/FAIL on t is often **undefined**, not passed |
| Scraped real dates: **45 events (≈25.6% of sensitivity base)**, 73.3% later than 45/60-day assumption | Lag assumption is **too aggressive** for most dated names — but §7.8 says that **understates** S3 avoidance and buy-side alpha. Observed S3 drift is still **positive**. |
| Screener restated history, survivorship, single "Financial Services" exclusion | Known biases; directionally conservative on S3, ambiguous on levels |

**Weighting guidance for the PM:**

- **India FAIL on sign/magnitude aligned with US (S3 up, S1 flat, S2 monotonicity broken):** treat as **corroboration of direction**, not independent proof.
- **India PASS on S2 V1 t-bar (2.38, 13 cohorts) or S1 overlay (+1.37% excess):** **do not upgrade** — sample too thin, baseline India overlay CAGR 0.63% vs benchmark makes excess comparisons noisy.
- **Binding decisions:** US walk-forward OOS is the **only leg with statistical authority**. India is an **honest OOS sanity check**, not a second vote. When India **contradicts** US (rare here), believe US. When India **agrees** on failure (S3), that agreement is meaningful despite thin N.

**Portfolio owner should put ~15–20% decision weight on India relative to US** for PASS/FAIL — enough to notice same-sign failures, not enough to flip a US-validated pass (none occurred).

---

## 7. Recommended next steps (ordered, max 5)

1. **Do not proceed to Phase 2 production integration** (triggers, `earnings.enabled: true`, growth-factor blend). Close the go/no-go chapter per §10 unless the PM explicitly amends bars.

2. **Build minimal viable earnings module — data + briefing only:** persist quarterly US/India facts per §1; weekly refresh; new briefing subsection (§9.2) with YoY deltas, SUE-CS bucket, S3 component states (WATCH/CONFIRMED/CLEAR), availability basis flag. **No triggers.** Estimated value: faster PM read of results already on Screener, uniform formatting, audit trail.

3. **Do not run S4 (LLM subsample) as a priority.** Mechanical buckets do not separate returns; κ vs a non-predictive mechanical verdict is low expected value. §8.5 contamination further limits pre-cutoff events. **Defer S4** until (a) a production briefing exists and (b) we want **post-deployment** live evaluation on fresh quarters — that is the clean test. If budget forces one LLM experiment, run **50 India holdings post-implementation** with human scorecard, not the 300-event historical subsample.

4. **Optional research fork (PM approval only):** S3 redesign — CONFIRMED **intersect** valuation stretch (historical P/E or EV/EBITDA percentile) — new spec, new bars. Do not code into production pipeline until it clears a slimmed §10.

5. **Optional research fork (PM approval only):** S2 **deep-review-only** simulation — enter at semiannual cadence, hold through state exit, measure incremental turnover vs 15%/yr. Only if PM wants a quant nudge on candidates; current evidence supports **human flag only**.

---

## 8. Suggested config defaults

**`earnings.enabled` remains `false`.** No signal cleared §10.

For the **minimal briefing module** (recommended build):

```yaml
earnings:
  enabled: false                    # DO NOT flip — no §10 pass
  availability_lag_days_q123: 45
  availability_lag_days_q4: 60
  exclude_financials_from_margin_signals: true
  briefing:
    show_sue_cs: true               # display only; India-primary CS z-score + bucket
    show_s3_state: true             # WATCH / CONFIRMED / CLEAR — informational
    show_quality_momentum_flag: true  # V1 AND flag at walk-forward modal thresholds; no score blend
  sue:
    lookback_quarters: 4            # modal US walk-forward; display TS if history allows
    min_history_quarters: 8
    surprise_threshold: 1.0         # display filter only
    miss_trigger_threshold: null    # DO NOT set — no EARNINGS_MISS trigger
  quality_momentum:
    min_revenue_growth_yoy_pct: 0.0   # modal V1; flag only at deep review
    min_opm_expansion_bps: 0
    persistence_quarters: 1
    growth_factor_blend_weight: 0.0   # explicit zero — no quant blend
  deterioration:
    consecutive_profit_declines: 3
    margin_compression_bps: -200
    cumulative_two_year_c2: true
    confirm_components: 2
    clear_after_clean_quarters: 2
    emit_trigger_on_confirmed: false  # critical — briefing only
  llm_verdict:
    enabled: false
    escalate_thesis_broken: false     # defer until live S4 / post-deploy eval
```

**Explicitly not recommended for config:** `miss_trigger_threshold: -2.0`, `growth_factor_blend_weight: 0.3`, `emit_trigger_on_confirmed: true`, or `llm_verdict.enabled: true` — each maps to a failed or untested production path.

---

## Closing

The backtest did its job: it **falsified** automated sell-on-deterioration and **disqualified** earnings-driven turnover within fund constraints. The quant's memo is honest; the surprises (S3 up, S2 churn, S1 null) are fundamental, not bugs. Next phase is **optional infrastructure for human judgment**, not signal deployment — unless the PM funds a narrow redesign with new bars.

*Reviewer: fundamental equity, spec author.*
