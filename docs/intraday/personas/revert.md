# revert — Intraday Mean Reversion Persona

**Philosophy:** Fade extremes, not trends. Prices overshoot session fair value and
revert before close.

**Sources:** Gatev (pairs z-score), Avellaneda-Lee (equilibrium reversion), Ernie
Chan (regime dependence), Kyle (liquidity provision in low-toxicity environments).

**Opposite of calan:** calan requires high gap + volume + trend; revert requires
low gap, range-bound session, distance from VWAP.

---

## 1. Universe and ranking

**Universe:** Nifty 50, high liquidity (`min_avg_turnover_inr >= 100M`).

**Inclusion filters (inverse of calan in_play):**

| Filter | Key | Value |
|--------|-----|-------|
| Max opening gap | `stocks_in_play.max_gap_pct` | 0.8 |
| Max trend cleanliness | `stocks_in_play.max_trend_cleanliness` | 0.45 |
| Min avg volume | `intraday.min_avg_volume` | 100,000 |
| Min turnover | `intraday.min_avg_turnover_inr` | 100,000,000 |

**Rank score (higher = better fade candidate):**

```
vwap_distance_score = min(|close - vwap| / vwap * 100, 3.0) * 25
liquidity_score     = min(turnover / min_turnover, 4.0) * 20
range_score         = min(range_pct / min_atr_pct, 3.0) * 15
anti_trend_score    = (1.0 - trend_cleanliness) * 25
low_gap_bonus       = 10 if |gap_pct| < 0.5 else 0
play_penalty        = -20 if in_play (calan-style) else 0

score = vwap_distance + liquidity + range + anti_trend + low_gap_bonus + play_penalty
```

---

## 2. Strategies

All strategies respect `trade_window_start: "10:00"` and `trade_window_end: "13:30"`.
Target R multiple default: `0.8`–`1.2` (tighter than calan).

### 2.1 vwap_fade

Fade price extremes relative to session VWAP.

**Indicators (per bar `t`, session data only):**

```
typical_t   = (high_t + low_t + close_t) / 3
vwap_t      = cumsum(typical_t * volume_t) / cumsum(volume_t)
residual_t  = close_t - vwap_t
sigma_t     = std(residual over last 20 bars), min 20 bars required
z_t         = residual_t / sigma_t
vol_ratio_t = volume_t / mean(volume over last 12 bars)
```

**Long entry (fade oversold):**

- `z_t <= -2.0`
- `vol_ratio_t < 1.0` (declining volume on extension)
- `t >= trade_window_start`
- `trend_cleanliness <= max_trend_cleanliness`

```
entry       = close_t
stop        = session_low_so_far * (1 - stop_buffer_pct/100)
target      = vwap_t
risk        = entry - stop
reward      = target - entry
require     reward / risk >= 1.0
force_exit  = force_exit_time (15:15)
side        = BUY
```

**Short entry (fade overbought):** symmetric with `z_t >= +2.0`, stop above
session high, target = vwap_t, side = SELL.

**Config keys:**

```yaml
revert:
  vwap_fade_z_entry: 2.0
  vwap_fade_z_exit: 0.25      # optional early exit if z crosses toward 0
  vwap_fade_sigma_lookback: 20
  target_r_multiple: 1.0
  stop_buffer_pct: 0.05
```

### 2.2 opening_gap_fade

Fade small gaps that lack volume confirmation (anti-calans).

**Preconditions (first bar of session, 09:15):**

```
gap_pct = (open - prev_close) / prev_close * 100
rvol    = volume_first_bar / avg_volume_first_bar_20d
```

**Long (fade gap down):**

- `gap_pct <= -0.3` and `gap_pct >= -1.0` (small gap down, not crash)
- `rvol < 1.5` (no volume confirmation)
- `t == first bar after 09:45` (wait for opening noise)

```
entry   = close at entry bar
stop    = session_low * (1 - stop_buffer_pct/100)
target  = prev_close  # gap fill target
side    = BUY
```

**Short (fade gap up):** symmetric with `+0.3 <= gap_pct <= +1.0`, target =
prev_close, side = SELL.

**Skip if:** `|gap_pct| > 1.0` (trend day risk) or `rvol >= 2.0`.

### 2.3 range_revert

Bollinger-style fade at established range boundaries.

**Range definition (first 60 minutes, 09:15–10:15):**

```
range_high = max(high) over range window
range_low  = min(low) over range window
range_mid  = (range_high + range_low) / 2
atr        = mean(high - low) over range window
```

**Valid only after 10:15** and when range width `>= 0.5 * atr_pct * mid / 100`.

**Long (touch lower band):**

- `close_t <= range_low + 0.1 * atr`
- `close_t > close_{t-1}` (first sign of rejection)
- `t <= trade_window_end`

```
entry  = close_t
stop   = range_low - 0.5 * atr
target = range_mid
side   = BUY
```

**Short (touch upper band):** symmetric at range_high.

---

## 3. LLM agents

### Regime analyst (catalyst role)

Classifies session before enabling fade strategies.

**Output schema:**

```json
{
  "session_regime": "trend" | "range" | "chop",
  "session_view": "2 sentences",
  "trade_day_quality": "poor" | "fair" | "good",
  "watchlist": [
    {
      "symbol": "NSE symbol",
      "fade_candidate": true,
      "direction_bias": "long" | "short" | "either",
      "confidence": 0.0-1.0,
      "rationale": "1-2 sentences"
    }
  ]
}
```

**Rules:**
- `session_regime == "trend"` → empty watchlist
- Only `range` or `chop` days produce fade candidates
- Max 5 names; empty watchlist valid

### Reversion gate (trade gate role)

**Veto if:**

- `session_regime == "trend"` (from watchlist context)
- time >= 14:00 IST
- daily loss >= max_daily_loss_pct
- trend_cleanliness > max_trend_cleanliness at signal
- reward:risk after costs < 1.0
- stop distance < 0.12% of entry

**Allowed strategies:** `vwap_fade`, `opening_gap_fade`, `range_revert`

---

## 4. Rules-only fallback

**Regime rules:**

```
avg_trend = mean(trend_cleanliness of top 8 ranked)
if avg_trend > 0.55: regime = "trend", empty watchlist
elif avg_trend < 0.35: regime = "range"
else: regime = "chop"
```

Watchlist: top 5 ranked with `|vwap_distance_pct| >= 1.0` and not in_play.

**Gate rules:** Same as calan gate but inverted trend check (veto if trend too
high) and min R:R = 1.0 for all strategies.

---

## 5. Risk profile

| Parameter | Value |
|-----------|-------|
| capital_inr | 300,000 |
| risk_per_trade_pct | 0.25 |
| max_trades_per_day | 5 |
| max_open_positions | 2 |
| max_daily_loss_pct | 1.5 |
| trade_window | 10:00 – 13:30 |

---

## 6. Config reference

See `personas.revert` in [`config/settings.yaml`](../../../config/settings.yaml).
