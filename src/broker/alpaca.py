"""Alpaca Markets broker client (US equities, paper or live).

Paper trading uses ``https://paper-api.alpaca.markets``; market data uses
``https://data.alpaca.markets``. Set ``ALPACA_API_KEY`` and ``ALPACA_SECRET_KEY``
in ``.env`` (see ``.env.example``).
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.broker.base import (
    BrokerClient,
    BrokerError,
    BrokerFunds,
    BrokerHolding,
    OrderRequest,
    OrderResult,
)
from src.config import Config, get_config
from src.logging_setup import get_logger

logger = get_logger(__name__)

PAPER_TRADING_BASE = "https://paper-api.alpaca.markets"
LIVE_TRADING_BASE = "https://api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"

_INTERVAL_MAP = {
    "ONE_MINUTE": "1Min",
    "FIVE_MINUTE": "5Min",
    "FIFTEEN_MINUTE": "15Min",
    "ONE_HOUR": "1Hour",
    "ONE_DAY": "1Day",
}


class AlpacaClient(BrokerClient):
    """US equities broker via Alpaca REST API v2."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        secrets = self._config.secrets
        self._api_key = secrets.alpaca_api_key
        self._secret_key = secrets.alpaca_secret_key
        self._paper = secrets.alpaca_paper
        self._trading_base = (
            PAPER_TRADING_BASE if self._paper else LIVE_TRADING_BASE
        ).rstrip("/")
        self._data_base = DATA_BASE.rstrip("/")
        self._session = requests.Session()
        self._connected = False
        self._last_call_at = 0.0

    @property
    def name(self) -> str:
        mode = "Paper" if self._paper else "Live"
        return f"Alpaca[{mode}]"

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        """Strip NSE suffixes and uppercase for US tickers."""
        sym = symbol.upper().strip()
        for suffix in (".NS", ".US", "-EQ"):
            if sym.endswith(suffix):
                sym = sym[: -len(suffix)]
        return sym

    def _headers(self) -> Dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._secret_key,
        }

    def _throttle(self) -> None:
        interval = float(
            self._config.get("connectors.alpaca.min_call_interval_seconds")
            or self._config.get("alpaca.min_call_interval_seconds", 0.25)
            or 0.25
        )
        elapsed = time.monotonic() - self._last_call_at
        if elapsed < interval:
            time.sleep(interval - elapsed)

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    def _request(
        self,
        method: str,
        base: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        self._throttle()
        url = f"{base}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        response = self._session.request(
            method,
            url,
            headers=self._headers(),
            json=json_body,
            timeout=30,
        )
        self._last_call_at = time.monotonic()
        if response.status_code >= 400:
            detail = response.text[:500]
            raise BrokerError(
                f"Alpaca {method} {path} failed ({response.status_code}): {detail}"
            )
        if not response.content:
            return {}
        return response.json()

    def _trading(self, method: str, path: str, **kwargs) -> Any:
        return self._request(method, self._trading_base, path, **kwargs)

    def _data(self, method: str, path: str, **kwargs) -> Any:
        return self._request(method, self._data_base, path, **kwargs)

    def connect(self) -> bool:
        if not self._api_key or not self._secret_key:
            raise BrokerError(
                "Alpaca credentials missing. Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env"
            )
        account = self._trading("GET", "/v2/account")
        status = str(account.get("status") or "")
        if status.upper() not in {"ACTIVE", "ACCOUNT_UPDATED"}:
            raise BrokerError(f"Alpaca account not active: status={status!r}")
        self._connected = True
        logger.info(
            "Alpaca connected (%s) — equity $%s, buying power $%s",
            "paper" if self._paper else "live",
            account.get("equity", "?"),
            account.get("buying_power", "?"),
        )
        return True

    def get_holdings(self) -> List[BrokerHolding]:
        rows = self._trading("GET", "/v2/positions") or []
        holdings: List[BrokerHolding] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            qty = int(float(row.get("qty") or 0))
            if qty <= 0:
                continue
            symbol = self.normalize_symbol(str(row.get("symbol") or ""))
            holdings.append(
                BrokerHolding(
                    symbol=symbol,
                    quantity=qty,
                    average_price=float(row.get("avg_entry_price") or 0.0),
                    last_price=float(row.get("current_price") or row.get("lastday_price") or 0.0),
                    exchange="US",
                )
            )
        logger.info("Fetched %d positions from Alpaca", len(holdings))
        return holdings

    def get_funds(self) -> BrokerFunds:
        account = self._trading("GET", "/v2/account") or {}
        cash = float(account.get("cash") or account.get("buying_power") or 0.0)
        equity = float(account.get("equity") or account.get("portfolio_value") or cash)
        return BrokerFunds(
            available_cash=cash,
            net_value=equity,
            utilised_margin=float(account.get("initial_margin") or 0.0),
            raw={k: str(v) for k, v in account.items()},
        )

    def get_last_price(self, symbol: str) -> Optional[float]:
        prices = self.get_last_prices([symbol])
        return prices.get(self.normalize_symbol(symbol))

    def get_last_prices(self, symbols: List[str]) -> Dict[str, float]:
        normalized = [self.normalize_symbol(s) for s in symbols if s]
        if not normalized:
            return {}

        # Snapshots accept comma-separated symbols (up to hundreds).
        prices: Dict[str, float] = {}
        chunk_size = 100
        for start in range(0, len(normalized), chunk_size):
            chunk = normalized[start : start + chunk_size]
            try:
                payload = self._data(
                    "GET",
                    "/v2/stocks/snapshots",
                    params={"symbols": ",".join(chunk)},
                )
            except BrokerError as exc:
                logger.warning("Alpaca snapshot fetch failed: %s", exc)
                continue
            if not isinstance(payload, dict):
                continue
            for sym, snap in payload.items():
                if not isinstance(snap, dict):
                    continue
                trade = snap.get("latestTrade") or snap.get("latest_trade") or {}
                minute = snap.get("minuteBar") or snap.get("minute_bar") or {}
                daily = snap.get("dailyBar") or snap.get("daily_bar") or {}
                for bar in (trade, minute, daily):
                    if isinstance(bar, dict) and bar.get("p") is not None:
                        prices[self.normalize_symbol(sym)] = float(bar["p"])
                        break
                    if isinstance(bar, dict) and bar.get("c") is not None:
                        prices[self.normalize_symbol(sym)] = float(bar["c"])
                        break

        logger.info("Fetched %d/%d quotes from Alpaca", len(prices), len(normalized))
        return prices

    def get_candles(
        self,
        symbol: str,
        from_date: datetime,
        to_date: datetime,
        interval: str = "ONE_DAY",
        strict: bool = False,
    ) -> List[Dict[str, Any]]:
        """Historical OHLCV bars from Alpaca market data."""
        sym = self.normalize_symbol(symbol)
        timeframe = _INTERVAL_MAP.get(interval.upper(), "1Day")
        start = from_date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        end = to_date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            payload = self._data(
                "GET",
                f"/v2/stocks/{sym}/bars",
                params={
                    "timeframe": timeframe,
                    "start": start,
                    "end": end,
                    "limit": 10000,
                    "adjustment": "split",
                },
            )
        except BrokerError as exc:
            if strict:
                raise
            logger.warning("Alpaca bars failed for %s: %s", sym, exc)
            return []

        bars = payload.get("bars") if isinstance(payload, dict) else None
        if not bars:
            return []

        out: List[Dict[str, Any]] = []
        for bar in bars:
            if not isinstance(bar, dict):
                continue
            ts = bar.get("t") or bar.get("timestamp")
            out.append(
                {
                    "timestamp": ts,
                    "open": float(bar.get("o") or 0),
                    "high": float(bar.get("h") or 0),
                    "low": float(bar.get("l") or 0),
                    "close": float(bar.get("c") or 0),
                    "volume": float(bar.get("v") or 0),
                }
            )
        return out

    def place_order(self, request: OrderRequest) -> OrderResult:
        symbol = self.normalize_symbol(request.symbol)
        if request.quantity <= 0:
            return OrderResult(
                success=False,
                symbol=symbol,
                side=request.side,
                quantity=request.quantity,
                error="Quantity must be positive",
            )

        side = request.side.upper()
        if side not in {"BUY", "SELL"}:
            return OrderResult(
                success=False,
                symbol=symbol,
                side=request.side,
                quantity=request.quantity,
                error=f"Invalid side: {request.side}",
            )

        order_type = request.order_type.upper()
        body: Dict[str, Any] = {
            "symbol": symbol,
            "qty": str(int(request.quantity)),
            "side": side.lower(),
            "type": "limit" if order_type == "LIMIT" else "market",
            "time_in_force": "day",
        }
        price = request.price
        if order_type == "LIMIT":
            if price is None or price <= 0:
                return OrderResult(
                    success=False,
                    symbol=symbol,
                    side=side,
                    quantity=request.quantity,
                    error="LIMIT order requires a price",
                )
            body["limit_price"] = f"{float(price):.2f}"
        elif price and price > 0:
            body["limit_price"] = f"{float(price):.2f}"
            body["type"] = "limit"

        logger.info(
            "Placing Alpaca %s %s x%d @ %s",
            side,
            symbol,
            request.quantity,
            body.get("limit_price", "MKT"),
        )

        try:
            response = self._trading("POST", "/v2/orders", json_body=body)
        except BrokerError as exc:
            return OrderResult(
                success=False,
                symbol=symbol,
                side=side,
                quantity=request.quantity,
                price=price,
                error=str(exc),
            )

        order_id = str(response.get("id") or "")
        return OrderResult(
            success=bool(order_id),
            symbol=symbol,
            side=side,
            quantity=request.quantity,
            order_id=order_id or None,
            price=price,
            status=str(response.get("status") or "submitted"),
            dry_run=self._paper,
            raw={"response": str(response)[:2000]},
        )

    def disconnect(self) -> None:
        self._session.close()
        self._connected = False
