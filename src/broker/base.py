"""Broker abstraction.

The fund talks to a broker only through this interface, so the live Angel One
client and the simulated broker used before go-live are interchangeable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class BrokerError(RuntimeError):
    """Any broker-side failure: auth, network, rejected order."""


@dataclass
class BrokerHolding:
    """One line of the broker's demat holdings."""

    symbol: str
    quantity: float
    average_price: float
    last_price: float
    isin: str = ""
    exchange: str = "NSE"

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price


@dataclass
class BrokerFunds:
    available_cash: float = 0.0
    net_value: float = 0.0
    utilised_margin: float = 0.0
    raw: Dict[str, str] = field(default_factory=dict)


@dataclass
class OrderRequest:
    symbol: str
    side: str  # BUY or SELL
    quantity: float = 0.0
    order_type: str = "LIMIT"
    price: Optional[float] = None
    notional: Optional[float] = None
    product_type: str = "DELIVERY"
    exchange: str = "NSE"
    variety: str = "NORMAL"
    duration: str = "DAY"


@dataclass
class OrderResult:
    success: bool
    symbol: str
    side: str
    quantity: float
    order_id: Optional[str] = None
    price: Optional[float] = None
    status: str = ""
    error: Optional[str] = None
    dry_run: bool = False
    raw: Dict[str, object] = field(default_factory=dict)


class BrokerClient(ABC):
    """Minimum surface the fund needs from a broker."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def connect(self) -> bool:
        """Authenticate. Returns True when a usable session exists."""

    @abstractmethod
    def get_holdings(self) -> List[BrokerHolding]:
        ...

    @abstractmethod
    def get_funds(self) -> BrokerFunds:
        ...

    @abstractmethod
    def get_last_price(self, symbol: str) -> Optional[float]:
        ...

    @abstractmethod
    def get_last_prices(self, symbols: List[str]) -> Dict[str, float]:
        ...

    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderResult:
        ...

    def disconnect(self) -> None:
        """Optional session teardown."""
        return None
