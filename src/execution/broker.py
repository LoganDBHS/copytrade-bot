"""BrokerAdapter interface — keep this clean so Alpaca/Schwab adapters can
slot in later without touching risk/ or manage/."""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class OptionContract:
    ticker: str
    strike: float
    side: str  # "CALL" | "PUT"
    expiry: date

    @property
    def key(self) -> str:
        s = str(int(self.strike)) if self.strike == int(self.strike) else str(self.strike)
        return f"{self.ticker} {self.expiry:%y%m%d}{self.side[0]}{s}"

    @classmethod
    def from_key(cls, key: str) -> OptionContract:
        ticker, rest = key.split(" ", 1)
        expiry = datetime.strptime(rest[:6], "%y%m%d").date()
        side = "CALL" if rest[6] == "C" else "PUT"
        return cls(ticker, float(rest[7:]), side, expiry)

    def __str__(self) -> str:  # e.g. "SPY 746C 08/17 "
        s = str(int(self.strike)) if self.strike == int(self.strike) else str(self.strike)
        return f"{self.ticker} {s}{'C' if self.side == 'CALL' else 'P'} {self.expiry:%m/%d}"


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    ts: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct_of_mid(self) -> float:
        return (self.ask - self.bid) / self.mid if self.mid > 0 else float("inf")


class OrderStatus(enum.Enum):
    NEW = "new"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class OrderState:
    client_order_id: str
    broker_order_id: str
    status: OrderStatus
    filled_qty: int = 0
    avg_fill_price: float | None = None


class BrokerError(Exception):
    """Any broker-side failure. Callers fail closed on this."""


class BrokerAdapter(abc.ABC):
    """All order methods are LIMIT-only by design — never market orders."""

    supports_disaster_stop: bool = False

    @abc.abstractmethod
    async def get_option_quote(self, contract: OptionContract) -> Quote:
        """Raise BrokerError if unavailable — never return a guess."""

    @abc.abstractmethod
    async def get_chain(self, ticker: str, expiry: date) -> set[tuple[float, str]]:
        """(strike, side) pairs actually tradable for this ticker/expiry."""

    @abc.abstractmethod
    async def buy_to_open(
        self, contract: OptionContract, qty: int, limit_price: float, client_order_id: str
    ) -> OrderState: ...

    @abc.abstractmethod
    async def sell_to_close(
        self, contract: OptionContract, qty: int, limit_price: float, client_order_id: str
    ) -> OrderState: ...

    async def positions(self) -> dict[str, int] | None:
        """Open option positions as {contract.key: qty}, or None when the
        broker can't say (unsupported, or a response shape surprise). None
        means UNKNOWN — callers must keep managing, never assume flat."""
        return None

    async def place_disaster_stop(
        self,
        contract: OptionContract,
        qty: int,
        stop_price: float,
        limit_price: float,
        client_order_id: str,
    ) -> OrderState | None:
        """Resting stop-limit parachute at the broker; None if unsupported."""
        return None

    @abc.abstractmethod
    async def cancel(self, broker_order_id: str) -> None: ...

    @abc.abstractmethod
    async def order_status(self, broker_order_id: str) -> OrderState: ...

    @abc.abstractmethod
    async def buying_power(self) -> float: ...
