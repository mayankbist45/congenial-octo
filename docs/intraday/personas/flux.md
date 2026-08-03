# flux — Order Flow Imbalance Persona

**Philosophy:** Follow signed pressure. Net buying/selling at the margin moves
price over the next few bars.

**Sources:** Cont et al. (OFI), Kyle (informed vs noise flow), DeepLOB (microstructure
patterns — deferred until tick data).

---

## 1. Data requirements

| Requirement | Detail |
|-------------|--------|
| Interval | `ONE_MINUTE` (mandatory) |
| Poll frequency | `watch_poll_seconds: 60` |
| Universe | Top 10 Nifty names by session turnover |
| Candle pool | **Required** — dual SmartAPI app via `AngelOneCandlePool` |
| LOB/tick | Not required for v1; candle proxy only |

**Rate limit note:** 10 symbols × 1-min refresh × 375 bars/session stresses a
single SmartAPI app. Configure `ANGELONE_DATA2_*` in `.env` before live watch.

---

## 2. OFI proxy (candle-based)

True OFI uses bid/ask size changes at the touch. Without LOB, use **close
location value (CLV)** signed volume:

### Per-bar signed volume

```
range_t = high_t - low_t
if range_t == 0:
    clv_t = 0
else:
    clv_t = 2 * (close_t - low_t) / range_t - 1    # in [-1, +1]

signed_vol_t = volume_t * clv_t
```

### Rolling OFI and z-score

Config keys: `ofi_lookback_bars` (default 5), `ofi_z_lookback` (default 60).

```
ofi_n_t     = sum(signed_vol over last n bars)
mu_t        = mean(ofi_n over last z_lookback bars)
sigma_t     = std(ofi_n over last z_lookback bars), floor 1e-9
ofi_z_t     = (ofi_n_t - mu_t) / sigma_t
```

### Auxiliary: price confirmation

```
prior_high  = max(high over last 3 bars excluding current)
prior_low   = min(low over last 3 bars excluding current)
price_conf_long  = close_t > prior_high
price_conf_short = close_t < prior_low
```

### Auxiliary: divergence (toxicity / exhaustion)

```
ofi_sign     = sign(ofi_z_t)
price_sign   = sign(close_t - close_{t-1})
divergence   = ofi_sign != 0 and price_sign != 0 and ofi_sign != price_sign
```

---

## 3. Strategies

Hold time target: 5–20 minutes. `force_exit` = min(entry + hold_minutes, 15:15).

### 3.1 ofi_momentum

Trade in direction of extreme OFI with price confirmation.

**Long entry:**

- `ofi_z_t >= +2.0`
- `price_conf_long == true`
- `not divergence` (flow and price aligned)
- `t` within trade window

```
entry       = close_t
stop        = close_t * (1 - stop_pct/100)     # default stop_pct = 0.20
target      = close_t * (1 + target_pct/100)   # default target_pct = 0.35
hold_max    = 15 minutes
side        = BUY
strategy    = ofi_momentum
```

**Short entry:** `ofi_z_t <= -2.0`, `price_conf_short`, symmetric stops/targets.

**Exit rules (simulator/watch):**

- Hit stop or target
- `ofi_z` crosses 0 (flow normalized)
- Time stop at hold_max

### 3.2 ofi_exhaustion

Fade OFI spike when price fails to follow (Kyle toxicity / adverse selection).

**Short entry (fade exhausted buying):**

- `ofi_z_t >= +2.5` (extreme buy pressure)
- `divergence == true` (price flat or down)
- `close_t < high_t - 0.3 * (high_t - low_t)` (rejection from bar high)

```
entry   = close_t
stop    = high_t * (1 + stop_buffer_pct/100)
target  = close_t * (1 - target_pct/100)
side    = SELL
strategy = ofi_exhaustion
```

**Long entry (fade exhausted selling):** symmetric with `ofi_z_t <= -2.5`.

---

## 4. Ranking

Rank symbols by flow opportunity:

```
ofi_score      = min(|ofi_z|, 4.0) * 30
turnover_score = min(turnover / 1e8, 5.0) * 25
tight_spread   = max(0, 2.0 - atr_pct) * 15   # penalize wide ATR noise
alignment      = 10 if not divergence else -15

score = ofi_score + turnover_score + tight_spread + alignment
```

Only top `ranked_universe_size: 10` proceed to flow analyst.

---

## 5. LLM agents

### Flow analyst (catalyst role)

**Output schema:**

```json
{
  "flow_summary": "2 sentences on session flow tone",
  "trade_day_quality": "poor" | "fair" | "good",
  "watchlist": [
    {
      "symbol": "NSE symbol",
      "flow_quality": "informed" | "noise" | "exhausted",
      "direction_bias": "long" | "short" | "either",
      "ofi_z": 0.0,
      "confidence": 0.0-1.0,
      "rationale": "1-2 sentences"
    }
  ]
}
```

**Rules:**
- Only `flow_quality == "informed"` names on watchlist
- `exhausted` names are candidates for `ofi_exhaustion` only (note in rationale)
- Max 3 names (focus book)

### Toxicity gate (trade gate role)

**Veto if:**

- `flow_quality == "noise"` for ofi_momentum signals
- `divergence == true` for ofi_momentum (flow/price misaligned)
- `atr_pct > 1.2` (spread too wide for scalping edge)
- daily loss >= 0.8% of capital
- time >= trade_window_end
- reward:risk after costs < 1.0 for ofi_momentum, < 0.8 for ofi_exhaustion
- stop distance < 0.10% of entry

**Allowed strategies:** `ofi_momentum`, `ofi_exhaustion`

**Scalping:** `veto_scalping: false` — but only for flux strategies, not generic
scalping strategy.

---

## 6. Rules-only fallback

**Flow analyst rules:**

```
for each symbol in top 10:
    if |ofi_z| >= 2.0 and not divergence:
        flow_quality = "informed"
    elif |ofi_z| >= 2.5 and divergence:
        flow_quality = "exhausted"
    else:
        flow_quality = "noise"
    include if flow_quality != "noise" and confidence >= min_watch_confidence
```

**Toxicity gate rules:** Mirror veto conditions above without LLM.

---

## 7. Risk profile

| Parameter | Value |
|-----------|-------|
| capital_inr | 200,000 |
| risk_per_trade_pct | 0.20 |
| max_trades_per_day | 8 |
| max_open_positions | 1 |
| max_daily_loss_pct | 0.8 |
| trade_window | 09:45 – 14:30 |

---

## 8. Upgrade path (tick / LOB)

When Angel One WebSocket tick feed is available:

1. Replace CLV proxy with true OFI:
   `OFI_t = (bid_size_t - bid_size_{t-1}) * I(price_up) - (ask_size_t - ask_size_{t-1}) * I(price_down)`
2. Add DeepLOB-inspired features: mid-price delta, book imbalance at L1-L5
3. Reduce `watch_poll_seconds` to 15–30

---

## 9. Config reference

See `personas.flux` in [`config/settings.yaml`](../../../config/settings.yaml).
