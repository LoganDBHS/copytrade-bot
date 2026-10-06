"""WebullAdapter — official `webull-openapi-python-sdk` (v2.x, module `webull`).

Verified against SDK 2.0.17 (2026-08-17): US option orders ARE supported via
trade.order_v2.place_option with legs marked market="US" (the SDK ships a US
TSLA option sample). Quirks honored here:
- LIMIT only for entries/exits; STOP_LOSS_LIMIT allowed → disaster stop works.
- Option SELL orders are DAY time-in-force only.
- Option market-data REST ≈60 req/min — poll spacing handled by the manager.

Option symbols are never guessed: the chain lookup (get_option_contracts)
returns each contract's tradable symbol, which we cache and use for quotes.
Response field names are parsed defensively; the first live probe with
Logan's keys may still tune _pick fields.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime

from execution.broker import (
    BrokerAdapter,
    BrokerError,
    OptionContract,
    OrderState,
    OrderStatus,
    Quote,
)

_STATUS_MAP = {
    "NEW": OrderStatus.NEW,
    "PENDING": OrderStatus.NEW,
    "SUBMITTED": OrderStatus.NEW,
    "OPEN": OrderStatus.NEW,
    "WORKING": OrderStatus.NEW,
    "PARTIAL_FILLED": OrderStatus.PARTIAL,
    "PARTIALLY_FILLED": OrderStatus.PARTIAL,
    "FILLED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "CANCELED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "FAILED": OrderStatus.REJECTED,
}


def _pick(payload: dict, *names, required=False, label=""):
    for n in names:
        v = payload.get(n)
        if v not in (None, ""):
            return v
    if required:
        raise BrokerError(f"missing {label or names} in payload keys={sorted(payload)}")
    return None


def _fmt_strike(strike: float) -> str:
    return str(int(strike)) if strike == int(strike) else str(strike)


_OCC_SYMBOL = re.compile(r"([A-Z]{1,6})(\d{6})([CP])(\d{8})")


def _standard_symbol(sym: str, ticker: str, expiry: date) -> bool:
    """Accept only the standard OCC series for this underlying. After a
    corporate action Webull's chain also lists adjusted contracts (roots like
    2AAPL or AAPL1) at the same strike/side/expiry; they collide with our
    cache key and their symbols are rejected by get_option_snapshot. Index
    weekly roots (SPXW, NDXP) are standard and must pass."""
    m = _OCC_SYMBOL.fullmatch(sym.strip().replace(" ", ""))
    if m is None or m.group(2) != f"{expiry:%y%m%d}":
        return False
    root = m.group(1)
    if not root.startswith(ticker.upper()):
        return False
    suffix = root[len(ticker):]
    return suffix == "" or (len(suffix) == 1 and suffix.isalpha())


class WebullAdapter(BrokerAdapter):
    supports_disaster_stop = True  # STOP_LOSS_LIMIT is allowed on options

    def __init__(self, app_key: str, app_secret: str, region: str = "us",
                 account_type: str = "CASH") -> None:
        # CASH by default: no PDT rule, which a 3-entries/day 0DTE strategy
        # needs under $25k. Logan's login has both CASH and MARGIN accounts.
        self._app_key = app_key
        self._app_secret = app_secret
        self._region = region
        self._account_type = account_type.upper()
        self._trade = None
        self._data = None
        self._account_id: str | None = None
        self._symbol_cache: dict[str, str] = {}  # contract.key -> option symbol
        self._orders: dict[str, str] = {}  # broker_order_id -> client_order_id
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- plumbing
    def _connect(self) -> None:
        from webull.core.client import ApiClient
        from webull.data.data_client import DataClient
        from webull.trade.trade_client import TradeClient

        client = ApiClient(self._app_key, self._app_secret, self._region)
        self._trade = TradeClient(client)
        self._data = DataClient(client)

    async def _ensure(self) -> None:
        if self._trade is None:
            async with self._lock:
                if self._trade is None:
                    try:
                        await asyncio.to_thread(self._connect)
                    except Exception as e:
                        raise BrokerError(f"webull connect failed: {e}") from e

    async def _call(self, fn, *args, retries: int = 0, **kwargs) -> dict | list:
        """retries: extra attempts for IDEMPOTENT reads only (a transient
        Webull 401/5xx cost a real entry on 2026-08-19). Writes (place/cancel)
        must stay at 0 — a 'failed' place may still have landed at the broker,
        and a blind retry would double the order."""
        await self._ensure()

        def run():
            resp = fn(*args, **kwargs)
            status = getattr(resp, "status_code", 200)
            if status != 200:
                body = getattr(resp, "text", "")[:300]
                raise BrokerError(f"webull {fn.__name__} HTTP {status}: {body}")
            return resp.json()

        attempt = 0
        while True:
            try:
                return await asyncio.to_thread(run)
            except Exception as e:
                err = e if isinstance(e, BrokerError) else BrokerError(
                    f"webull {fn.__name__}: {e}"
                )
                if attempt >= retries:
                    raise err from e
                attempt += 1
                await asyncio.sleep(0.5 * attempt)

    @staticmethod
    def _rows(data) -> list[dict]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("data", "results", "contracts", "items", "list"):
                v = data.get(k)
                if isinstance(v, list):
                    return v
            return [data]
        return []

    # -------------------------------------------------------------- account
    async def account_id(self) -> str:
        if self._account_id is None:
            await self._ensure()
            data = await self._call(self._trade.account_v2.get_account_list, retries=2)
            rows = self._rows(data)
            if not rows:
                raise BrokerError("no webull accounts visible to this API key")
            match = [
                r for r in rows
                if str(r.get("account_type", "")).upper() == self._account_type
            ]
            if not match:
                types = [r.get("account_type") for r in rows]
                raise BrokerError(
                    f"no {self._account_type} account found (have: {types}) — "
                    "set the configured account type to one of these"
                )
            self._account_id = str(
                _pick(match[0], "account_id", "accountId", required=True, label="account_id")
            )
        return self._account_id

    async def buying_power(self) -> float:
        acct = await self.account_id()
        data = await self._call(
            self._trade.account_v2.get_account_balance, acct, retries=2
        )
        payload = data if isinstance(data, dict) else (self._rows(data) or [{}])[0]
        # Options buy with option buying power, listed per currency.
        assets = payload.get("account_currency_assets") or []
        usd = next((a for a in assets if a.get("currency") == "USD"), assets[0] if assets else {})
        v = _pick(usd, "option_buying_power", "day_buying_power", "cash_balance")
        if v is None:
            v = _pick(payload, "total_cash_balance", "total_net_liquidation_value",
                      required=True, label="buying_power")
        return float(v)

    async def positions(self) -> dict[str, int] | None:
        try:
            acct = await self.account_id()
            data = await self._call(
                self._trade.account_v2.get_account_position, acct, retries=2
            )
        except BrokerError:
            return None
        rows = self._rows(data)
        if len(rows) == 1 and isinstance(rows[0], dict):
            for k in ("positions", "holdings", "items"):
                if isinstance(rows[0].get(k), list):
                    rows = rows[0][k]
                    break
        out: dict[str, int] = {}
        try:
            for r in rows:
                # some shapes group legs under "items" per instrument
                items = r["items"] if isinstance(r.get("items"), list) else [r]
                for it in items:
                    itype = str(_pick(it, "instrument_type", "instrumentType") or "").upper()
                    if "OPTION" not in itype:
                        continue
                    under = _pick(it, "underlying_symbol", "underlyingSymbol", "ticker")
                    strike = _pick(it, "strike_price", "strikePrice")
                    otype = _pick(it, "option_type", "optionType")
                    exp = _pick(it, "option_expire_date", "optionExpireDate", "expire_date")
                    qty = _pick(it, "quantity", "qty", "position", "total_quantity")
                    if None in (under, strike, otype, exp, qty):
                        return None  # unfamiliar shape — report UNKNOWN, never flat
                    c = OptionContract(str(under).upper(), float(strike),
                                       str(otype).upper(), date.fromisoformat(str(exp)[:10]))
                    out[c.key] = out.get(c.key, 0) + int(float(qty))
        except Exception:
            return None
        return out

    # ---------------------------------------------------------- market data
    async def get_chain(self, ticker: str, expiry: date) -> set[tuple[float, str]]:
        await self._ensure()  # bound methods below need a live client
        chain: set[tuple[float, str]] = set()
        last_id = None
        day = expiry.isoformat()
        for _ in range(40):  # paginate defensively, never loop forever
            data = await self._call(
                self._data.instrument.get_option_contracts,
                category="US_OPTION",
                underlying_symbols=ticker,
                start_date=day,
                end_date=day,
                page_size=200,
                last_instrument_id=last_id,
                retries=2,
            )
            rows = self._rows(data)
            if not rows:
                break
            for r in rows:
                strike = _pick(r, "strike_price", "strikePrice")
                otype = _pick(r, "option_type", "optionType")
                sym = _pick(r, "symbol", "option_symbol", "optionSymbol")
                if strike is None or otype is None:
                    continue
                if sym and not _standard_symbol(str(sym), ticker, expiry):
                    continue  # adjusted series — would clobber the standard symbol
                side = str(otype).upper()
                key = OptionContract(ticker, float(strike), side, expiry).key
                if sym:
                    self._symbol_cache[key] = str(sym)
                chain.add((float(strike), side))
            if len(rows) < 200:
                break
            last_id = _pick(rows[-1], "instrument_id", "instrumentId", "id")
            if last_id is None:
                break
        return chain

    async def _option_symbol(self, contract: OptionContract) -> str:
        sym = self._symbol_cache.get(contract.key)
        if sym is None:
            await self.get_chain(contract.ticker, contract.expiry)
            sym = self._symbol_cache.get(contract.key)
        if sym is None:
            raise BrokerError(f"{contract} not found in webull option chain")
        return sym

    async def get_option_quote(self, contract: OptionContract) -> Quote:
        sym = await self._option_symbol(contract)
        try:
            data = await self._call(
                self._data.option_market_data.get_option_snapshot, sym, "US_OPTION",
                retries=2,
            )
        except BrokerError as e:
            if "INVALID_SYMBOL" in str(e):
                # don't reuse a symbol the snapshot endpoint rejects — force a
                # fresh chain lookup on the next attempt
                self._symbol_cache.pop(contract.key, None)
            raise
        rows = self._rows(data)
        if not rows:
            raise BrokerError(f"empty option snapshot for {contract}")
        p = rows[0]
        bid = _pick(p, "bid_price", "bidPrice", "bid", required=True, label="bid")
        ask = _pick(p, "ask_price", "askPrice", "ask", required=True, label="ask")
        return Quote(bid=float(bid), ask=float(ask), ts=datetime.now(UTC))

    # ---------------------------------------------------------------- orders
    def _leg(self, contract: OptionContract, side: str, qty: int) -> dict:
        return {
            "side": side,
            "quantity": str(qty),
            "symbol": contract.ticker,
            "strike_price": _fmt_strike(contract.strike),
            "option_expire_date": contract.expiry.isoformat(),
            "instrument_type": "OPTION",
            "option_type": contract.side,
            "market": "US",
        }

    async def _place_option(self, order: dict, client_order_id: str) -> OrderState:
        acct = await self.account_id()
        data = await self._call(self._trade.order_v2.place_option, acct, [order])
        rows = self._rows(data)
        payload = rows[0] if rows else {}
        broker_id = str(
            _pick(payload, "order_id", "orderId", "client_order_id", "clientOrderId")
            or client_order_id
        )
        self._orders[broker_id] = client_order_id
        return OrderState(client_order_id, broker_id, OrderStatus.NEW)

    async def buy_to_open(self, contract, qty, limit_price, client_order_id) -> OrderState:
        order = {
            "client_order_id": client_order_id.replace("-", "")[:32],
            "combo_type": "NORMAL",
            "order_type": "LIMIT",
            "quantity": str(qty),
            "limit_price": f"{limit_price:.2f}",
            "option_strategy": "SINGLE",
            "side": "BUY",
            "time_in_force": "DAY",
            "entrust_type": "QTY",
            "legs": [self._leg(contract, "BUY", qty)],
        }
        return await self._place_option(order, order["client_order_id"])

    async def sell_to_close(self, contract, qty, limit_price, client_order_id) -> OrderState:
        order = {
            "client_order_id": client_order_id.replace("-", "")[:32],
            "combo_type": "NORMAL",
            "order_type": "LIMIT",
            "quantity": str(qty),
            "limit_price": f"{limit_price:.2f}",
            "option_strategy": "SINGLE",
            "side": "SELL",
            "time_in_force": "DAY",  # option sells are DAY-only at Webull
            "entrust_type": "QTY",
            "legs": [self._leg(contract, "SELL", qty)],
        }
        return await self._place_option(order, order["client_order_id"])

    async def place_disaster_stop(self, contract, qty, stop_price, limit_price,
                                  client_order_id) -> OrderState | None:
        order = {
            "client_order_id": client_order_id.replace("-", "")[:32],
            "combo_type": "NORMAL",
            "order_type": "STOP_LOSS_LIMIT",
            "quantity": str(qty),
            "stop_price": f"{stop_price:.2f}",
            "limit_price": f"{limit_price:.2f}",
            "option_strategy": "SINGLE",
            "side": "SELL",
            "time_in_force": "DAY",
            "entrust_type": "QTY",
            "legs": [self._leg(contract, "SELL", qty)],
        }
        try:
            return await self._place_option(order, order["client_order_id"])
        except BrokerError:
            return None  # manager announces the missing parachute

    async def cancel(self, broker_order_id: str) -> None:
        acct = await self.account_id()
        coid = self._orders.get(broker_order_id, broker_order_id)
        await self._call(self._trade.order_v2.cancel_option, acct, coid)

    async def order_status(self, broker_order_id: str) -> OrderState:
        acct = await self.account_id()
        coid = self._orders.get(broker_order_id, broker_order_id)
        data = await self._call(
            self._trade.order_v2.get_order_detail, acct, coid, retries=2
        )
        # Verified live 2026-08-18: detail is {client_order_id, combo_order_id,
        # orders: [{status, filled_quantity, ...}]}.
        if isinstance(data, dict) and isinstance(data.get("orders"), list) and data["orders"]:
            p = data["orders"][0]
        else:
            rows = self._rows(data)
            p = rows[0] if rows else {}
        raw_status = str(
            _pick(p, "order_status", "orderStatus", "status", required=True, label="status")
        ).upper()
        status = _STATUS_MAP.get(raw_status)
        if status is None:
            raise BrokerError(f"unknown webull order status {raw_status!r}")
        filled = _pick(p, "filled_quantity", "filledQuantity", "filled_qty") or 0
        avg = _pick(p, "avg_fill_price", "avgFillPrice", "filled_price", "avg_price")
        return OrderState(
            coid, broker_order_id, status,
            filled_qty=int(float(filled)),
            avg_fill_price=float(avg) if avg is not None else None,
        )
