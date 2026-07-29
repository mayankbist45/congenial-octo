"""Angel One SmartAPI client.

SmartAPI sessions are short-lived: the login uses the account PIN plus a TOTP
generated from the secret shown when API 2FA is enabled, and the resulting
token is only good for the trading day. The client therefore logs in lazily at
the start of each run and caches the session in memory.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import pyotp

from src.broker.base import (
    BrokerClient,
    BrokerError,
    BrokerFunds,
    BrokerHolding,
    OrderRequest,
    OrderResult,
)
from src.broker.instruments import Instrument, get_instrument_master
from src.config import Config, get_config
from src.logging_setup import get_logger

logger = get_logger(__name__)

# SmartAPI documents 10 orders/second; data endpoints are tighter. A small
# delay between calls keeps well clear of throttling on a 500-name universe.
_MIN_CALL_INTERVAL = 0.25
_MAX_RETRIES = 3

_INTERVAL_MAP = {
    "ONE_MINUTE": timedelta(minutes=1),
    "FIVE_MINUTE": timedelta(minutes=5),
    "FIFTEEN_MINUTE": timedelta(minutes=15),
    "ONE_HOUR": timedelta(hours=1),
    "ONE_DAY": timedelta(days=1),
}


class AngelOneClient(BrokerClient):
    """Live broker client backed by ``smartapi-python``."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._secrets = self._config.secrets
        self._api: Optional[Any] = None
        self._session: Optional[Dict[str, Any]] = None
        self._session_date: Optional[date] = None
        self._instruments = get_instrument_master(self._config)
        self._last_call_at = 0.0

    @property
    def name(self) -> str:
        return "AngelOne"

    @property
    def is_connected(self) -> bool:
        return self._api is not None and self._session_date == date.today()

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        if self.is_connected:
            return True

        if not self._secrets.has_broker_credentials:
            raise BrokerError(
                "Angel One credentials incomplete. Set ANGELONE_API_KEY, "
                "ANGELONE_CLIENT_CODE, ANGELONE_PIN and ANGELONE_TOTP_SECRET "
                "in .env"
            )

        from SmartApi.smartConnect import SmartConnect

        self._api = SmartConnect(api_key=self._secrets.angelone_api_key)

        try:
            totp = pyotp.TOTP(self._secrets.angelone_totp_secret).now()
        except Exception as exc:
            raise BrokerError(
                f"Could not generate TOTP - check ANGELONE_TOTP_SECRET is the "
                f"base32 secret, not a 6-digit code: {exc}"
            ) from exc

        response = self._api.generateSession(
            self._secrets.angelone_client_code,
            self._secrets.angelone_pin,
            totp,
        )

        if not isinstance(response, dict) or not response.get("status"):
            message = ""
            if isinstance(response, dict):
                message = response.get("message") or response.get("errorcode") or ""
            raise BrokerError(f"Angel One login failed: {message or response}")

        self._session = response.get("data") or {}
        self._session_date = date.today()
        logger.info("Angel One session established for %s", self._secrets.angelone_client_code)

        # Loading the instrument master here means every later call can resolve
        # tokens without a network round trip.
        self._instruments.load()
        return True

    def disconnect(self) -> None:
        if self._api is None:
            return
        try:
            self._api.terminateSession(self._secrets.angelone_client_code)
            logger.info("Angel One session terminated")
        except Exception as exc:
            logger.debug("Session termination failed (harmless): %s", exc)
        finally:
            self._api = None
            self._session = None
            self._session_date = None

    def _ensure_connected(self) -> Any:
        if not self.is_connected:
            self.connect()
        if self._api is None:
            raise BrokerError("Angel One client is not connected")
        return self._api

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_call_at
        if elapsed < _MIN_CALL_INTERVAL:
            time.sleep(_MIN_CALL_INTERVAL - elapsed)
        self._last_call_at = time.time()

    def _call(self, method_name: str, *args, **kwargs) -> Dict[str, Any]:
        """Invoke a SmartAPI method with throttling and bounded retries."""
        api = self._ensure_connected()
        method = getattr(api, method_name)

        last_error: Optional[str] = None
        for attempt in range(1, _MAX_RETRIES + 1):
            self._throttle()
            try:
                response = method(*args, **kwargs)
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "%s failed (attempt %d/%d): %s",
                    method_name,
                    attempt,
                    _MAX_RETRIES,
                    exc,
                )
                time.sleep(min(2**attempt, 8))
                continue

            if isinstance(response, dict):
                if response.get("status"):
                    return response
                last_error = (
                    response.get("message")
                    or response.get("errorcode")
                    or str(response)
                )
                # Auth expiry mid-run: re-login once and retry.
                if "token" in str(last_error).lower() and attempt < _MAX_RETRIES:
                    logger.info("Session appears expired; re-authenticating")
                    self._session_date = None
                    self._api = None
                    api = self._ensure_connected()
                    method = getattr(api, method_name)
                    continue
                logger.warning("%s returned failure: %s", method_name, last_error)
                break

            last_error = f"unexpected response type {type(response)!r}"
            break

        raise BrokerError(f"{method_name} failed: {last_error}")

    # ------------------------------------------------------------------
    # Portfolio
    # ------------------------------------------------------------------
    def get_holdings(self) -> List[BrokerHolding]:
        response = self._call("holding")
        rows = response.get("data") or []
        holdings: List[BrokerHolding] = []

        for row in rows:
            if not isinstance(row, dict):
                continue
            quantity = int(float(row.get("quantity") or 0))
            if quantity <= 0:
                continue
            symbol = (row.get("tradingsymbol") or "").upper()
            if symbol.endswith("-EQ"):
                symbol = symbol[:-3]
            holdings.append(
                BrokerHolding(
                    symbol=symbol,
                    quantity=quantity,
                    average_price=float(row.get("averageprice") or 0.0),
                    last_price=float(row.get("ltp") or 0.0),
                    isin=(row.get("isin") or "").strip(),
                    exchange=(row.get("exchange") or "NSE").upper(),
                )
            )

        logger.info("Fetched %d holdings from Angel One", len(holdings))
        return holdings

    def get_funds(self) -> BrokerFunds:
        response = self._call("rmsLimit")
        data = response.get("data") or {}

        def _num(*keys: str) -> float:
            for key in keys:
                value = data.get(key)
                if value not in (None, "", "null"):
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        continue
            return 0.0

        return BrokerFunds(
            available_cash=_num("availablecash", "net"),
            net_value=_num("net", "availablecash"),
            utilised_margin=_num("utiliseddebits", "utilisedpayout"),
            raw={k: str(v) for k, v in data.items()},
        )

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def get_last_price(self, symbol: str) -> Optional[float]:
        instrument = self._instruments.get(symbol)
        if instrument is None:
            logger.warning("No instrument token for %s", symbol)
            return None
        try:
            response = self._call(
                "ltpData", instrument.exchange, instrument.symbol, instrument.token
            )
        except BrokerError as exc:
            logger.warning("LTP fetch failed for %s: %s", symbol, exc)
            return None
        data = response.get("data") or {}
        try:
            return float(data.get("ltp"))
        except (TypeError, ValueError):
            return None

    def get_last_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Batch quote fetch. SmartAPI accepts up to 50 tokens per call."""
        resolved = self._instruments.resolve_many(symbols)
        if not resolved:
            return {}

        by_token = {inst.token: sym for sym, inst in resolved.items()}
        tokens = list(by_token.keys())
        prices: Dict[str, float] = {}

        for start in range(0, len(tokens), 50):
            chunk = tokens[start : start + 50]
            try:
                response = self._call("getMarketData", "LTP", {"NSE": chunk})
            except BrokerError as exc:
                logger.warning("Batch quote failed for %d tokens: %s", len(chunk), exc)
                continue

            data = response.get("data") or {}
            for row in data.get("fetched") or []:
                token = str(row.get("symbolToken") or "")
                symbol = by_token.get(token)
                if not symbol:
                    continue
                try:
                    prices[symbol] = float(row.get("ltp"))
                except (TypeError, ValueError):
                    continue

            for row in data.get("unfetched") or []:
                logger.debug(
                    "Quote unavailable for token %s: %s",
                    row.get("symbolToken"),
                    row.get("message"),
                )

        logger.info("Fetched %d/%d quotes from Angel One", len(prices), len(symbols))
        return prices

    def get_candles(
        self,
        symbol: str,
        from_date: datetime,
        to_date: datetime,
        interval: str = "ONE_DAY",
    ) -> List[Dict[str, Any]]:
        """Historical OHLCV. Angel One caps each request by interval, so long
        ranges are fetched in windows and concatenated."""
        instrument = self._instruments.get(symbol)
        if instrument is None:
            logger.warning("No instrument token for %s", symbol)
            return []

        # Daily candles are limited to roughly 2000 bars per request.
        window = timedelta(days=1800) if interval == "ONE_DAY" else timedelta(days=25)
        candles: List[Dict[str, Any]] = []
        cursor = from_date

        while cursor < to_date:
            chunk_end = min(cursor + window, to_date)
            params = {
                "exchange": instrument.exchange,
                "symboltoken": instrument.token,
                "interval": interval,
                "fromdate": cursor.strftime("%Y-%m-%d %H:%M"),
                "todate": chunk_end.strftime("%Y-%m-%d %H:%M"),
            }
            try:
                response = self._call("getCandleData", params)
            except BrokerError as exc:
                logger.warning("Candle fetch failed for %s: %s", symbol, exc)
                break

            for row in response.get("data") or []:
                if not isinstance(row, list) or len(row) < 6:
                    continue
                candles.append(
                    {
                        "timestamp": row[0],
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                    }
                )
            cursor = chunk_end

        return candles

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------
    def _round_to_tick(self, price: float, instrument: Instrument) -> float:
        tick = instrument.tick_size or 0.05
        return round(round(price / tick) * tick, 2)

    def place_order(self, request: OrderRequest) -> OrderResult:
        instrument = self._instruments.get(request.symbol)
        if instrument is None:
            return OrderResult(
                success=False,
                symbol=request.symbol,
                side=request.side,
                quantity=request.quantity,
                error=f"No NSE instrument token for {request.symbol}",
            )

        if request.quantity <= 0:
            return OrderResult(
                success=False,
                symbol=request.symbol,
                side=request.side,
                quantity=request.quantity,
                error="Quantity must be positive",
            )

        price = request.price
        if request.order_type.upper() == "LIMIT":
            if price is None or price <= 0:
                return OrderResult(
                    success=False,
                    symbol=request.symbol,
                    side=request.side,
                    quantity=request.quantity,
                    error="LIMIT order requires a price",
                )
            price = self._round_to_tick(price, instrument)

        params = {
            "variety": request.variety,
            "tradingsymbol": instrument.symbol,
            "symboltoken": instrument.token,
            "transactiontype": request.side.upper(),
            "exchange": instrument.exchange,
            "ordertype": request.order_type.upper(),
            "producttype": request.product_type.upper(),
            "duration": request.duration.upper(),
            "price": f"{price:.2f}" if price else "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(int(request.quantity)),
        }

        logger.info(
            "Placing %s %s x%d @ %s",
            request.side,
            instrument.symbol,
            request.quantity,
            params["price"],
        )

        try:
            response = self._call("placeOrderFullResponse", params)
        except BrokerError as exc:
            return OrderResult(
                success=False,
                symbol=request.symbol,
                side=request.side,
                quantity=request.quantity,
                price=price,
                error=str(exc),
            )

        data = response.get("data") or {}
        order_id = str(data.get("orderid") or data.get("uniqueorderid") or "")

        return OrderResult(
            success=bool(order_id),
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            order_id=order_id or None,
            price=price,
            status="submitted" if order_id else "unknown",
            error=None if order_id else "Broker did not return an order id",
            raw={"response": str(response)[:2000]},
        )

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Look up one order in the order book."""
        try:
            response = self._call("orderBook")
        except BrokerError as exc:
            return {"status": "unknown", "error": str(exc)}

        for row in response.get("data") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("orderid")) == str(order_id):
                return {
                    "status": (row.get("status") or "").lower(),
                    "filled_quantity": int(float(row.get("filledshares") or 0)),
                    "average_price": float(row.get("averageprice") or 0.0),
                    "text": row.get("text") or "",
                }
        return {"status": "not_found"}
