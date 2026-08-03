# Strategy Research Memo: Better Stock Selection

Date: 2026-08-02

This memo is a first research pass for improving risk-adjusted alpha versus the
Nifty 500. It is not a production strategy change and does not alter the trading
system. Its job is to turn the current evidence into a practical research
backlog for the MutualFundManager.

## Executive Recommendation

Keep the fund's core aim as quality-at-a-reasonable-price, but test a shift from
the current balanced factor mix toward a quality-momentum GARP model.

The strongest near-term candidates for improvement are:

1. Increase quality and momentum weight while reducing pure value weight.
2. Treat low volatility as a risk overlay, not as a standalone alpha factor.
3. Avoid very strict valuation gates until point-in-time fundamentals and a full
   universe backtest confirm they add value.
4. Add a decision-review loop that labels bad trades by mistake type, so future
   optimization is driven by observed errors rather than only aggregate Sharpe.
5. Refresh the research dataset before making production changes, because the
   current local price cache is too small for final conclusions.

## Current Strategy Diagnosis

The default `balanced` persona is a GARP-style Nifty 500 strategy:

- Factor weights: quality 30%, value 25%, growth 20%, momentum 15%, trend 10%.
- Buy gates: debt/equity <= 2.0, ROE >= 10%, P/E <= 80, revenue growth >= 0%.
- Portfolio target: 25 positions, 5% cash buffer, 8% max position, 25% max
  sector.
- Turnover budget: 75% annually, matching a low-frequency active fund mandate.

This is a sensible starting point. The main weakness is that stock selection is
still mostly a static cross-sectional score. The model does not yet know whether
past mistakes came from overpaying, ignoring trend quality, weak fundamentals,
overly tight risk limits, or premature selling.

The current backtest engine is useful for screening variants, but has known
limits:

- It uses simplified rule-based decisions, not the full LLM decision process.
- Cached fundamentals are not point-in-time.
- The local price cache currently contains only 7 stocks plus the benchmark.
- It does not yet calculate benchmark-relative alpha inside `BacktestResult`.

Because of those limits, the local results below are directional, not decisive.

## External Research Anchors

Indian factor products and research support testing multi-factor stock selection,
but they do not support blindly adding every factor.

- NSE lists Nifty strategy indices across quality, value, alpha, low volatility,
  momentum, and multi-factor combinations such as Nifty500 Quality 50,
  Nifty500 Value 50, Nifty500 Low Volatility 50, Nifty500 Momentum 50, and
  Nifty500 Multifactor MQVLv 50.
  Source: https://www.nseindia.com/static/products-services/indices-strategy
- MSCI's India Factor Mix A-Series integrates quality, value, and low volatility
  in equal weights, indicating that institutional multi-factor design often
  treats factor cyclicality as something to diversify.
  Source: https://www.msci.com/indexes/index/704312/msci-india-factor-mix-a-series-index-usd
- A 2026 public-data India factor-premia paper found momentum results highly
  sensitive to benchmark choice, with positive momentum premia in broad and
  Top-500 proxy universes but weak or negative low-volatility results across
  proxy universes.
  Source: https://papers.ssrn.com/sol3/Delivery.cfm/6850938.pdf?abstractid=6850938
- S&P DJI's India factor research frames low volatility, momentum, quality, and
  value as the core factor families to study in Indian equity indices.
  Source: https://www.spglobal.com/spdji/en/research/article/an-index-approach-to-factor-investing-in-india/

Research implication: the first production candidate should not be a broad
factor explosion. It should be a controlled test of quality, momentum, value
strictness, and low-volatility risk control.

## First-Pass Variant Comparison

The following in-memory variants were run against cached local data from
2024-01-02 to 2026-07-31. The benchmark proxy `^CRSLDX` returned 20.82% over the
same period. Since the cache is only a smoke-sized sample, the table is useful
for ranking hypotheses, not for making live changes.

| Variant | Total return | Excess vs benchmark | Annualised | Max drawdown | Sharpe | Trades | Turnover | Win rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline balanced | 3.40% | -17.42% | 1.31% | -1.63% | -2.641 | 19 | 23.4% | 84.6% |
| Quality-heavy GARP | 4.04% | -16.78% | 1.55% | -2.12% | -2.496 | 14 | 15.8% | 90.0% |
| Quality + momentum | 4.27% | -16.55% | 1.64% | -2.24% | -1.929 | 27 | 33.8% | 77.8% |
| Quality + value | -0.27% | -21.09% | -0.11% | -1.11% | -12.032 | 2 | 3.9% | 0.0% |
| Momentum + quality | 1.45% | -19.37% | 0.56% | -2.58% | -2.228 | 29 | 53.8% | 46.7% |
| Low-vol overlay proxy | 1.82% | -19.00% | 0.70% | -2.06% | -3.097 | 11 | 18.8% | 66.7% |
| Strict valuation gate | -0.96% | -21.78% | -0.38% | -1.12% | -11.784 | 3 | 5.5% | 0.0% |
| Strict quality gate | 2.54% | -18.28% | 0.98% | -1.49% | -3.263 | 9 | 15.0% | 80.0% |

Initial read:

- Quality + momentum produced the best total return and best Sharpe in this
  limited sample.
- Quality-heavy GARP improved return and reduced turnover versus baseline.
- Strict value and strict valuation variants traded too little and underperformed
  badly, suggesting the current data path may over-filter when valuation gates
  are tightened.
- The low-volatility proxy reduced trading but did not improve return or Sharpe
  in the smoke sample, consistent with treating low volatility as risk control
  until stronger evidence appears.
- Every variant lagged the benchmark in this limited run, so the correct next
  move is deeper research, not immediate production changes.

## Recommended Strategy Experiments

### 1. Quality-Momentum GARP Candidate

Hypothesis: In the Nifty 500, high-quality companies with confirmed price
strength outperform a static quality/value blend after risk controls.

Test configuration:

- quality 35%, value 10%, growth 15%, momentum 30%, trend 10%
- require price above 200-DMA for new buys
- keep current P/E <= 80 and debt/equity <= 2.0 gates initially
- keep annual turnover budget at 75% for the first test

Acceptance hurdle:

- beats baseline Sharpe and Sortino
- improves or matches excess return
- does not increase max drawdown by more than 2 percentage points
- turnover remains below 75% annualized

### 2. Quality-Heavy GARP Candidate

Hypothesis: More quality weight improves consistency without forcing excessive
turnover.

Test configuration:

- quality 40%, value 25%, growth 20%, momentum 10%, trend 5%
- ROE gate raised from 10% to 14%
- P/E gate tightened from 80 to 60

Acceptance hurdle:

- lower turnover than baseline
- better Sharpe than baseline
- no material reduction in candidate count

### 3. Low-Volatility Risk Overlay

Hypothesis: Low volatility is more useful as a portfolio-control layer than as a
primary stock-selection alpha factor.

Test configuration:

- add volatility percentile and beta checks to candidate diagnostics
- penalize top-volatility names only when they also worsen tracking error or
  single-position risk contribution
- do not add a standalone low-vol factor until full-universe evidence supports it

Acceptance hurdle:

- lower drawdown or tracking error without reducing excess return materially
- no large drop in candidate diversity

### 4. Valuation Gate Sensitivity

Hypothesis: The current valuation gate may be broad enough, while stricter P/E
limits may block good compounders.

Test configuration:

- compare P/E gates at 45, 60, 80, and 100
- compare sector-neutral valuation percentiles if available
- track rejected winners to identify false negatives

Acceptance hurdle:

- stricter gates must improve Sharpe or max drawdown, not only reduce trading
- candidate count must remain high enough to build 20-25 positions

### 5. Mistake-Learning Review Loop

Hypothesis: Future improvements should be driven by labeled decision mistakes,
not only aggregate return metrics.

Add a research-only decision review with these labels:

- bought_expensive_winner
- bought_weak_quality
- ignored_positive_momentum
- ignored_fundamental_deterioration
- sold_too_early
- held_loser_too_long
- risk_engine_saved_us
- risk_engine_too_strict

Acceptance hurdle:

- every executed proposal can be reviewed after 30, 90, 180, and 365 days
- lessons can be summarized into future prompt/rule changes
- review output remains advisory until validated by backtests

## Rejected Or Deferred Ideas

- Full machine learning model: defer. The current data is not rich enough and
  the fund needs auditable factor behavior first.
- Paid data dependency: defer. Public NSE and cached data are enough for the
  first research cycle.
- Low-volatility as a full factor: defer until full-universe tests justify it.
- Very strict value strategy: rejected for now. The smoke test became too
  inactive and underperformed.
- Higher-turnover pure momentum: rejected for the core fund. It may be a separate
  persona, but it conflicts with the current low-turnover fund aim.

## Next Implementation Backlog

1. Add a research script that runs named in-memory factor variants and emits a
   CSV or Markdown table.
2. Extend `BacktestResult` or the research script to calculate benchmark-relative
   return, tracking error, and information ratio.
3. Refresh historical price data for the full Nifty 500 before re-running the
   variant comparison.
4. Add a low-volatility candidate diagnostic using `volatility_annualised` and
   portfolio beta, but keep it out of production scoring until tested.
5. Add a decision-outcome review report from journaled proposals and trades.
6. Re-run the memo with full-universe results and promote only changes that beat
   the baseline across multiple windows.

## Risks And Data Limitations

- Current local backtest results are based on a very small cached symbol set.
- Cached fundamentals are current snapshots, not point-in-time financials.
- The rule-based backtest does not fully represent the LLM-assisted decision
  layer.
- Survivorship bias may exist if current Nifty 500 constituents are applied
  backward.
- External factor index performance may not translate directly to this fund
  because this fund has different sizing, turnover, gates, and execution rules.

## Bottom Line

The best next strategy direction is not a radical rebuild. It is a disciplined
research push toward quality-momentum GARP, with low volatility treated as risk
control and valuation gates tested carefully before tightening. The fund should
learn from mistakes through an explicit review loop, then use those labels to
tune rules and prompts only after the evidence repeats across time windows.
