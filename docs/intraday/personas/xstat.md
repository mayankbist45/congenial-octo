# Xstat — Cross-Asset Statistical Arbitrage

Intraday persona that trades **beta-hedged residual spreads** between an index/sector
anchor (ETF proxy) and a single equity leg.

## Philosophy

Unlike `spread` (1:1 log ratio between similar peers), `xstat` models:

```
residual = log(leg) − β × log(anchor)
```

where β is rolling OLS hedge ratio (leg regressed on anchor). When the residual
z-score exceeds a threshold, enter a **market-neutral basket**: long the cheap
leg, short the rich leg (equity + ETF).

First-principles basis: Gatev et al. pairs trading; Avellaneda-Lee stat-arb with
a tradable factor (Nifty / Bank index) as the anchor.

## Configured baskets (default)

| Anchor     | Leg       | Rationale              |
|------------|-----------|------------------------|
| NIFTYBEES  | RELIANCE  | Large-cap vs Nifty     |
| NIFTYBEES  | TCS       | IT vs index            |
| BANKBEES   | HDFCBANK  | Bank vs sector ETF     |
| BANKBEES   | ICICIBANK | Bank vs sector ETF     |

## Run

```bash
python run_daily.py analyse --persona xstat --as-of 2026-08-03
python scripts/intraday_backtest.py --persona xstat --start 2026-07-21 --end 2026-08-03
```

## Key settings (`personas.xstat.intraday`)

- `cross_baskets` — list of `{anchor, leg}` or `[ANCHOR, LEG]`
- `cross_entry_z` — residual z entry (default 2.0)
- `cross_beta_lookback` — bars for rolling β (default 60)
- `cross_min_bars` — minimum session bars before z-score (default 25)

## vs spread

|           | spread              | xstat                    |
|-----------|---------------------|--------------------------|
| Legs      | Same-sector peers   | ETF anchor + single name |
| Spread    | log(A/B)            | log(leg) − β·log(anchor)|
| Beta      | Implicit 1:1        | Explicit rolling hedge   |
