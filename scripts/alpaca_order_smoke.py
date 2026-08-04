#!/usr/bin/env python3
"""Place a tiny Alpaca paper order to verify the trading API (buy then flatten)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.base import OrderRequest
from src.broker.factory import build_broker
from src.config import get_persona_config
from src.logging_setup import setup_logging


def main() -> int:
    parser = argparse.ArgumentParser(description="Alpaca paper order smoke test.")
    parser.add_argument("--persona", default="calan_us")
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--quantity", type=int, default=1)
    args = parser.parse_args()

    config = get_persona_config(args.persona)
    setup_logging(level="INFO", log_file=config.path("logging.file", "data/fund.log"))

    broker = build_broker(config)
    broker.connect()

    sym = args.symbol.upper()
    prices = broker.get_last_prices([sym])
    px = float(prices.get(sym) or 0)
    if px <= 0:
        print(f"ERROR: no price for {sym}")
        return 1

    print(f"Placing BUY {args.quantity} {sym} @ ~{px:.2f} (limit)")
    buy = broker.place_order(
        OrderRequest(
            symbol=sym,
            side="BUY",
            quantity=args.quantity,
            order_type="LIMIT",
            price=round(px * 1.002, 2),
            product_type="INTRADAY",
            exchange="US",
        )
    )
    print(f"BUY result: success={buy.success} order_id={buy.order_id} status={buy.status} error={buy.error}")
    if not buy.success:
        return 1

    time.sleep(3)

    print(f"Placing SELL {args.quantity} {sym} (market flatten)")
    sell = broker.place_order(
        OrderRequest(
            symbol=sym,
            side="SELL",
            quantity=args.quantity,
            order_type="MARKET",
            product_type="INTRADAY",
            exchange="US",
        )
    )
    print(f"SELL result: success={sell.success} order_id={sell.order_id} status={sell.status} error={sell.error}")
    return 0 if sell.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
