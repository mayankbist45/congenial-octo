"""Route intraday entries/exits through the configured broker when mode=broker."""

from __future__ import annotations

from typing import Optional, Tuple

from src.broker.base import BrokerClient, BrokerError, OrderRequest
from src.config import Config
from src.intraday.models import PaperPosition
from src.logging_setup import get_logger

logger = get_logger(__name__)

BROKER_MODES = frozenset({"broker", "live"})


def intraday_uses_broker(config: Config) -> bool:
    mode = str(config.get("intraday.mode", "paper") or "paper").lower()
    return mode in BROKER_MODES


def has_broker_credentials(config: Config) -> bool:
    provider = config.broker_provider
    if provider == "alpaca":
        return config.secrets.has_alpaca_credentials
    if provider == "angelone":
        return config.secrets.has_broker_credentials
    return False


class IntradayBrokerExecutor:
    """Submit intraday entry/exit orders via the persona's broker connector."""

    def __init__(self, config: Config):
        self._config = config
        self._broker: Optional[BrokerClient] = None
        self._connected = False
        if intraday_uses_broker(config) and not has_broker_credentials(config):
            logger.warning(
                "intraday.mode=%s but %s credentials missing; broker orders disabled",
                config.get("intraday.mode"),
                config.broker_provider,
            )

    @property
    def active(self) -> bool:
        return intraday_uses_broker(self._config) and has_broker_credentials(self._config)

    def equity(self, fallback: float) -> float:
        if not self.active:
            return fallback
        try:
            funds = self._ensure_broker().get_funds()
            value = float(funds.net_value or funds.available_cash or 0.0)
            return value if value > 0 else fallback
        except Exception as exc:
            logger.warning("Intraday broker equity lookup failed: %s", exc)
            return fallback

    def submit_entry(self, position: PaperPosition) -> Tuple[bool, Optional[str]]:
        return self._place(_entry_request(self._config, position), position, "entry")

    def submit_exit(self, position: PaperPosition, exit_price: float) -> Tuple[bool, Optional[str]]:
        return self._place(_exit_request(self._config, position, exit_price), position, "exit")

    def flatten(self, position: PaperPosition, exit_price: float) -> Tuple[bool, Optional[str]]:
        """Emergency market exit after a failed paired entry."""
        return self.submit_exit(position, exit_price)

    def _ensure_broker(self) -> BrokerClient:
        if self._broker is None:
            from src.broker.factory import build_broker

            self._broker = build_broker(self._config)
        if not self._connected:
            if not self._broker.connect():
                raise BrokerError(f"{self._broker.name} connect failed")
            self._connected = True
        return self._broker

    def _place(
        self,
        request: OrderRequest,
        position: PaperPosition,
        leg: str,
    ) -> Tuple[bool, Optional[str]]:
        try:
            result = self._ensure_broker().place_order(request)
        except BrokerError as exc:
            logger.warning(
                "Intraday %s order failed %s %s x%d: %s",
                leg,
                request.side,
                request.symbol,
                request.quantity,
                exc,
            )
            return False, str(exc)
        except Exception as exc:
            logger.warning(
                "Intraday %s order error %s %s x%d: %s",
                leg,
                request.side,
                request.symbol,
                request.quantity,
                exc,
            )
            return False, str(exc)

        if not result.success:
            err = result.error or f"{leg} rejected"
            logger.warning(
                "Intraday %s rejected %s %s x%d: %s",
                leg,
                request.side,
                request.symbol,
                request.quantity,
                err,
            )
            return False, err

        if leg == "entry":
            position.entry_order_id = result.order_id
        else:
            position.exit_order_id = result.order_id

        logger.info(
            "Intraday %s %s %s x%d @ %s -> order_id=%s status=%s",
            leg,
            request.side,
            request.symbol,
            request.quantity,
            request.price or "MKT",
            result.order_id,
            result.status,
        )
        return True, None


def _exchange(config: Config) -> str:
    return str(config.get("execution.exchange") or config.market_region or "NSE")


def _product_type(config: Config) -> str:
    return str(config.get("execution.product_type", "INTRADAY") or "INTRADAY")


def entry_order_request(config: Config, position: PaperPosition) -> OrderRequest:
    return _entry_request(config, position)


def exit_order_request(config: Config, position: PaperPosition, exit_price: float) -> OrderRequest:
    return _exit_request(config, position, exit_price)


def _entry_request(config: Config, position: PaperPosition) -> OrderRequest:
    order_type = str(
        config.get("intraday.entry_order_type")
        or config.get("execution.order_type", "LIMIT")
        or "LIMIT"
    ).upper()
    return OrderRequest(
        symbol=position.symbol,
        side=position.side,
        quantity=position.quantity,
        order_type=order_type,
        price=position.entry_price if order_type == "LIMIT" else None,
        product_type=_product_type(config),
        exchange=_exchange(config),
    )


def _exit_request(config: Config, position: PaperPosition, exit_price: float) -> OrderRequest:
    close_side = "SELL" if position.side == "BUY" else "BUY"
    order_type = str(config.get("intraday.exit_order_type", "MARKET") or "MARKET").upper()
    price = round(exit_price, 2) if order_type == "LIMIT" else None
    return OrderRequest(
        symbol=position.symbol,
        side=close_side,
        quantity=position.quantity,
        order_type=order_type,
        price=price,
        product_type=_product_type(config),
        exchange=_exchange(config),
    )
