"""PaperBroker: real market data, simulated fills, local paper account.

Fills are conservative: buys fill at the ask (never better than a real
marketable limit), sells fill at the bid. Non-marketable limits rest and are
re-checked against a fresh quote on every status poll — same shape as the
FakeBroker used in tests, but quotes come from a live adapter (Webull).
"""

from __future__ import annotations

import itertools
from datetime import date

from execution.broker import (
    BrokerAdapter,
    BrokerError,
    OptionContract,
    OrderState,
    OrderStatus,
    Quote,
)


class PaperBroker(BrokerAdapter):
    supports_disaster_stop = False  # stops stay bot-managed in paper

    def __init__(self, quotes: BrokerAdapter, starting_buying_power: float = 5_000.0):
        self.quotes = quotes
        self._bp = starting_buying_power
        self.orders: dict[str, OrderState] = {}
        self.meta: dict[str, dict] = {}
        self._ids = itertools.count(1)

    async def get_option_quote(self, contract: OptionContract) -> Quote:
        return await self.quotes.get_option_quote(contract)

    async def get_chain(self, ticker: str, expiry: date) -> set[tuple[float, str]]:
        return await self.quotes.get_chain(ticker, expiry)

    async def _place(self, action: str, contract: OptionContract, qty: int,
                     limit_price: float, client_order_id: str) -> OrderState:
        for st in self.orders.values():
            if st.client_order_id == client_order_id:
                return st
        oid = f"PAPER{next(self._ids)}"
        st = OrderState(client_order_id, oid, OrderStatus.NEW)
        self.orders[oid] = st
        self.meta[oid] = {"action": action, "contract": contract, "qty": qty,
                          "limit": limit_price}
        await self._try_fill(oid)
        return self.orders[oid]

    async def _try_fill(self, oid: str) -> None:
        st = self.orders[oid]
        if st.status is not OrderStatus.NEW:
            return
        m = self.meta[oid]
        try:
            q = await self.quotes.get_option_quote(m["contract"])
        except BrokerError:
            return  # no quote — order rests, nothing fills on a guess
        if m["action"] == "BUY" and m["limit"] >= q.ask > 0:
            fill = q.ask
            cost = fill * 100 * m["qty"]
            if cost > self._bp:
                st.status = OrderStatus.REJECTED
                return
            self._bp -= cost
            st.status, st.filled_qty, st.avg_fill_price = OrderStatus.FILLED, m["qty"], fill
        elif m["action"] == "SELL" and 0 < m["limit"] <= q.bid:
            fill = q.bid
            self._bp += fill * 100 * m["qty"]
            st.status, st.filled_qty, st.avg_fill_price = OrderStatus.FILLED, m["qty"], fill

    async def buy_to_open(self, contract, qty, limit_price, client_order_id):
        return await self._place("BUY", contract, qty, limit_price, client_order_id)

    async def sell_to_close(self, contract, qty, limit_price, client_order_id):
        return await self._place("SELL", contract, qty, limit_price, client_order_id)

    async def cancel(self, broker_order_id: str) -> None:
        st = self.orders.get(broker_order_id)
        if st and st.status is OrderStatus.NEW:
            st.status = OrderStatus.CANCELLED

    async def order_status(self, broker_order_id: str) -> OrderState:
        await self._try_fill(broker_order_id)
        return self.orders[broker_order_id]

    async def buying_power(self) -> float:
        return self._bp
