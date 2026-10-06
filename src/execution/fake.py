"""FakeBroker for tests and dry paper runs: deterministic quotes and fills."""

from __future__ import annotations

import itertools
from datetime import date, datetime

from execution.broker import (
    BrokerAdapter,
    BrokerError,
    OptionContract,
    OrderState,
    OrderStatus,
    Quote,
)


class FakeBroker(BrokerAdapter):
    """Limit orders fill immediately when marketable against the current quote:
    buys fill at min(limit, ask), sells at max(limit, bid). Non-marketable
    orders rest until the quote moves or they are cancelled."""

    supports_disaster_stop = True

    def __init__(self, buying_power: float = 10_000.0) -> None:
        self._bp = buying_power
        self.quotes: dict[str, Quote] = {}
        self.chains: dict[tuple[str, date], set[tuple[float, str]]] = {}
        self.orders: dict[str, OrderState] = {}
        self.order_meta: dict[str, dict] = {}
        self.disaster_stops: list[dict] = []
        self._ids = itertools.count(1)
        self.now: datetime | None = None  # injectable quote timestamp
        self.holdings: dict[str, int] | None = None  # None = broker can't say

    # -- test helpers -------------------------------------------------------
    def set_quote(self, contract: OptionContract, bid: float, ask: float,
                  ts: datetime | None = None) -> None:
        self.quotes[contract.key] = Quote(bid, ask, ts or self.now or datetime.now())
        self._try_fill_resting()

    def add_chain(self, contract: OptionContract) -> None:
        self.chains.setdefault((contract.ticker, contract.expiry), set()).add(
            (contract.strike, contract.side)
        )

    def _try_fill_resting(self) -> None:
        for oid, st in self.orders.items():
            if st.status is not OrderStatus.NEW:
                continue
            meta = self.order_meta[oid]
            q = self.quotes.get(meta["contract"].key)
            if q is None:
                continue
            if meta["action"] == "BUY" and meta["limit"] >= q.ask:
                st.status = OrderStatus.FILLED
                st.filled_qty = meta["qty"]
                st.avg_fill_price = min(meta["limit"], q.ask)
            elif meta["action"] == "SELL" and meta["limit"] <= q.bid:
                st.status = OrderStatus.FILLED
                st.filled_qty = meta["qty"]
                st.avg_fill_price = max(meta["limit"], q.bid)

    # -- BrokerAdapter ------------------------------------------------------
    async def get_option_quote(self, contract: OptionContract) -> Quote:
        q = self.quotes.get(contract.key)
        if q is None:
            raise BrokerError(f"no quote for {contract}")
        return q

    async def get_chain(self, ticker: str, expiry: date) -> set[tuple[float, str]]:
        return self.chains.get((ticker, expiry), set())

    async def positions(self) -> dict[str, int] | None:
        return self.holdings

    async def _place(self, action: str, contract: OptionContract, qty: int,
                     limit_price: float, client_order_id: str) -> OrderState:
        for st in self.orders.values():
            if st.client_order_id == client_order_id:  # idempotency
                return st
        oid = f"FB{next(self._ids)}"
        st = OrderState(client_order_id, oid, OrderStatus.NEW)
        self.orders[oid] = st
        self.order_meta[oid] = {
            "action": action, "contract": contract, "qty": qty, "limit": limit_price,
        }
        self._try_fill_resting()
        return self.orders[oid]

    async def buy_to_open(self, contract, qty, limit_price, client_order_id):
        return await self._place("BUY", contract, qty, limit_price, client_order_id)

    async def sell_to_close(self, contract, qty, limit_price, client_order_id):
        return await self._place("SELL", contract, qty, limit_price, client_order_id)

    async def place_disaster_stop(self, contract, qty, stop_price, limit_price,
                                  client_order_id):
        oid = f"FB{next(self._ids)}"
        st = OrderState(client_order_id, oid, OrderStatus.NEW)
        self.orders[oid] = st
        self.order_meta[oid] = {
            "action": "STOP", "contract": contract, "qty": qty, "limit": limit_price,
        }
        self.disaster_stops.append(
            {"contract": contract, "qty": qty, "stop": stop_price, "id": oid}
        )
        return st

    async def cancel(self, broker_order_id: str) -> None:
        st = self.orders.get(broker_order_id)
        if st and st.status is OrderStatus.NEW:
            st.status = OrderStatus.CANCELLED

    async def order_status(self, broker_order_id: str) -> OrderState:
        return self.orders[broker_order_id]

    async def buying_power(self) -> float:
        return self._bp
