"""Round-trip cost estimates for intraday signal filtering."""

from __future__ import annotations

from src.config import Config


def round_trip_costs(config: Config, entry: float, exit: float, quantity: int) -> float:
    """Estimate INR costs for a round trip (entry + exit) on one leg."""
    qty = max(int(quantity), 1)
    buy_value = entry * qty
    sell_value = exit * qty
    turnover = buy_value + sell_value
    brokerage_pct = float(config.get("intraday.costs.brokerage_pct", 0.03) or 0.03) / 100.0
    cap = float(config.get("intraday.costs.brokerage_cap_per_order", 20.0) or 20.0)
    brokerage = min(cap, buy_value * brokerage_pct) + min(cap, sell_value * brokerage_pct)
    stt = sell_value * float(config.get("intraday.costs.stt_sell_pct", 0.025) or 0.025) / 100.0
    exchange = turnover * float(config.get("intraday.costs.exchange_txn_pct", 0.00325) or 0.00325) / 100.0
    sebi = turnover * float(config.get("intraday.costs.sebi_pct", 0.0001) or 0.0001) / 100.0
    stamp = buy_value * float(config.get("intraday.costs.stamp_buy_pct", 0.003) or 0.003) / 100.0
    gst = (brokerage + exchange + sebi) * float(config.get("intraday.costs.gst_pct", 18.0) or 18.0) / 100.0
    slippage = turnover * float(config.get("intraday.slippage_bps_per_side", 2.0) or 2.0) / 10000.0
    return brokerage + stt + exchange + sebi + stamp + gst + slippage


def passes_min_net_edge(
    config: Config,
    entry: float,
    target: float,
    quantity: int,
) -> bool:
    """Reject signals whose gross move cannot cover costs plus min edge bps."""
    min_bps = float(config.get("intraday.min_net_edge_bps", 0) or 0)
    if min_bps <= 0 or entry <= 0:
        return True
    qty = max(int(quantity), 1)
    gross = abs(target - entry) * qty
    costs = round_trip_costs(config, entry, target, qty)
    min_edge = entry * qty * min_bps / 10000.0
    return gross >= costs + min_edge


def estimate_signal_quantity(config: Config, entry: float, risk_per_share: float, equity: float) -> int:
    """Position size used for cost-edge checks before fill."""
    if entry <= 0 or risk_per_share <= 0:
        return 0
    risk_amount = equity * float(config.get("intraday.risk_per_trade_pct", 0.4) or 0.4) / 100.0
    max_value = equity * float(config.get("intraday.max_position_value_pct", 20.0) or 20.0) / 100.0
    return int(min(risk_amount / risk_per_share, max_value / entry))
