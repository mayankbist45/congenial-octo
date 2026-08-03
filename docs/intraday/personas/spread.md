# spread — Intraday Pairs / Relative Value Persona

**Philosophy:** Trade the spread, not the stock. Two cointegrated names diverge
temporarily; bet on convergence.

**Sources:** Gatev (pairs trading), Avellaneda-Lee (factor-neutral residuals),
Kyle (pair legs reduce single-name flow risk), Fama-French (beta hedging).

---

## 1. Pair universe

Pre-selected static pairs (Nifty 50 liquid names):

| Pair ID | Leg A | Leg B | Sector |
|---------|-------|-------|--------|
| bank | HDFCBANK | ICICIBANK | Banking |
| it | TCS | INFY | IT |
| energy | RELIANCE | ONGC | Energy |
| auto | MARUTI | TATAMOTORS | Auto |

Config:

```yaml
intraday:
  pairs:
    - [HDFCBANK, ICICIBANK]
    - [TCS, INFY]
    - [RELIANCE, ONGC]
    - [MARUTI, TATAMOTORS]
```

`sector_spread` (stock vs sector ETF) is deferred — no reliable sector ETF OHLCV
on Angel One for all sectors.

---

## 2. Cointegration pre-test (offline)

Run **before** enabling the persona in production. Not executed at runtime.

### 2.1 Data

- Interval: `FIVE_MINUTE`
- History: 60 trading days
- Ratio: `r_t = log(price_A_t / price_B_t)`

### 2.2 Engle-Granger test

For each pair:

1. OLS: `log(A) = alpha + beta * log(B) + epsilon`
2. ADF test on residuals `epsilon` with H0: unit root
3. **Pass if** p-value < 0.05 on ADF

### 2.3 Johansen test (confirmation)

- Bivariate Johansen on `[log(A), log(B)]` with lag=1
4. **Pass if** trace statistic rejects r=0 at 5%

### 2.4 Half-life filter (Avellaneda-Lee / Chan)

On spread `s_t = log(A_t) - beta * log(B_t)`:

```
delta_s_t = s_t - s_{t-1}
regress: delta_s_t = lambda * s_{t-1} + noise
half_life = -log(2) / lambda
```

**Pass if** `half_life <= 5 trading days` (intraday persona needs fast reversion).

### 2.5 Pre-test script (future)

```
scripts/pairs_cointegration.py --pairs config --days 60 --interval FIVE_MINUTE
```

Output: `data/intraday/pairs_cointegration.json` with beta, half-life, pass/fail
per pair. Runtime loads betas from this file.

---

## 3. Spread construction (runtime)

Given pre-computed `beta` for pair (A, B):

```
spread_t = log(close_A_t) - beta * log(close_B_t)
mu       = mean(spread over session so far, min 30 bars)
sigma    = std(spread over session so far, min 30 bars), floor 1e-9
z_t      = (spread_t - mu) / sigma
```

**Dollar-neutral leg sizing** (paper book):

```
notional_per_leg = capital * risk_per_trade_pct / 100 / 2
qty_A = notional_per_leg / close_A
qty_B = notional_per_leg / close_B * beta   # beta-adjusted hedge
```

---

## 4. Strategy: pair_zscore

### Entry

**Short spread (rich A, cheap B)** — `z_t >= pair_entry_z` (default 2.0):

```
leg_A: SELL qty_A
leg_B: BUY  qty_B
entry_spread = spread_t
```

**Long spread (cheap A, rich B)** — `z_t <= -pair_entry_z`:

```
leg_A: BUY  qty_A
leg_B: SELL qty_B
```

**Preconditions:**

- Both legs have turnover >= min_avg_turnover_inr
- Neither leg on earnings day (from briefing / calendar)
- `|z_t - z_{t-3}| <= 0.5` (spread not still widening violently)
- Cointegration pre-test passed for this pair

### Exit

| Condition | Action |
|-----------|--------|
| `|z_t| <= pair_exit_z` (default 0.25) | Close both legs (convergence) |
| `|z_t| >= pair_stop_z` (default 3.0) | Close both legs (stop) |
| time >= force_exit_time | Close both legs |
| daily loss limit hit | Close all pairs |

### P&L

Track at **pair level**, not leg level:

```
pair_pnl = qty_A * (exit_A - entry_A) * side_A + qty_B * (exit_B - entry_B) * side_B
         - costs_A - costs_B
```

---

## 5. Ranking

Rank **pairs**, not individual stocks:

```
spread_score   = min(|z_t|, 4.0) * 35
liquidity      = min(min(turnover_A, turnover_B) / 1e8, 4.0) * 25
stable_spread  = 15 if |z_t - z_{t-3}| <= 0.3 else -10
coint_ok       = 25 if pre_test_pass else -999

score = spread_score + liquidity + stable_spread + coint_ok
```

Top `max_trades_per_day` pairs considered.

---

## 6. LLM agents

### Spread analyst (catalyst role)

**Output schema:**

```json
{
  "session_view": "2 sentences",
  "trade_day_quality": "poor" | "fair" | "good",
  "pair_opportunities": [
    {
      "leg_a": "SYMBOL",
      "leg_b": "SYMBOL",
      "spread_z": 0.0,
      "divergence_type": "fundamental" | "technical",
      "cointegration_ok": true,
      "direction": "short_spread" | "long_spread",
      "confidence": 0.0-1.0,
      "rationale": "1-2 sentences"
    }
  ]
}
```

**Rules:**
- Only `divergence_type == "technical"` and `cointegration_ok == true`
- Veto pair if either leg has earnings/catalyst (fundamental divergence)
- Max 2 pair opportunities

### Convergence gate (trade gate role)

**Veto if:**

- `divergence_type == "fundamental"`
- spread widening: `z_t - z_{t-3} > 0.5` (absolute value, direction-aware)
- either leg turnover below minimum
- two-leg margin exceeds `max_position_value_pct` per leg
- daily loss limit hit
- time >= 14:00

**Allowed strategies:** `pair_zscore`

Gate receives **pair signal** (both legs) not single-leg signal.

---

## 7. Two-leg execution model

New infrastructure required in `state.py`, `watch.py`, `simulator.py`:

### 7.1 Data models

```python
@dataclass
class PairLeg:
    symbol: str
    side: str          # BUY | SELL
    qty: int
    entry_price: float

@dataclass
class PairPosition:
    pair_id: str       # e.g. "HDFCBANK:ICICIBANK"
    leg_a: PairLeg
    leg_b: PairLeg
    entry_spread_z: float
    entry_time: datetime
    beta: float
```

### 7.2 Entry flow

1. `pair_zscore` emits `PairSignal` (not `IntradaySignal`)
2. Spread analyst confirms pair
3. Convergence gate approves pair
4. **Atomic fill:** both legs fill at same bar close or veto entire pair
5. Partial fill on one leg → cancel other leg (no naked leg exposure)

### 7.3 State file

`data/intraday/spread_state.json`:

```json
{
  "open_pairs": [...],
  "closed_pairs": [...],
  "daily_pnl": 0.0
}
```

### 7.4 Briefing format

Spread analyst briefing includes per-pair: z-score, beta, half-life, leg
turnovers, widening flag, earnings flag.

---

## 8. Rules-only fallback

**Spread analyst:**

```
for each configured pair with cointegration_ok:
    if |z| >= pair_entry_z:
        divergence_type = "technical"  # unless earnings flag in calendar
        include with confidence = min(0.95, 0.5 + |z| * 0.1)
```

**Convergence gate:** Apply veto rules mechanically; approve if none fire.

---

## 9. Risk profile

| Parameter | Value |
|-----------|-------|
| capital_inr | 400,000 |
| risk_per_trade_pct | 0.35 |
| max_trades_per_day | 2 |
| max_open_positions | 2 (one pair = 2 legs) |
| max_position_value_pct | 15 (per leg) |
| pair_entry_z | 2.0 |
| pair_exit_z | 0.25 |
| pair_stop_z | 3.0 |

---

## 10. Config reference

See `personas.spread` in [`config/settings.yaml`](../../../config/settings.yaml).
