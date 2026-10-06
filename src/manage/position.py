"""One PositionManager per play. Soft stops on the quote MID every poll;
exits are always marketable limits repriced until filled. The only resting
order at the broker is the disaster stop-limit parachute (if supported)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, time

from execution.broker import BrokerAdapter, BrokerError, OptionContract, OrderStatus


@dataclass
class Play:
    alerter_id: str
    contract: OptionContract
    qty: int
    fill_price: float
    tp_only: bool = False
    state: str = "OPEN"  # OPEN -> RUNNER (after trim) -> CLOSED; or TP_ONLY
    stop: float = 0.0
    peak_mid: float = 0.0
    disaster_order_id: str | None = None
    realized: float = 0.0  # $ P&L accumulated on closes
    db_id: int | None = None  # positions row; set at entry, reused on recovery

    def __post_init__(self) -> None:
        if self.tp_only:
            self.state = "TP_ONLY"


class PositionManager:
    def __init__(self, broker: BrokerAdapter, cfg: dict, play: Play,
                 control=None, audit=None) -> None:
        self.broker = broker
        self.cfg = cfg["exits"]
        self.play = play
        self.control = control
        self.audit = audit
        p = play
        # A recovered play arrives with its stop/peak already set — keep them
        # (RUNNER stops have ratcheted up; resetting would widen the risk).
        if p.stop == 0.0:
            p.stop = round(p.fill_price * (1 - self.cfg["initial_stop_pct"]), 2)
        if p.peak_mid == 0.0:
            p.peak_mid = p.fill_price
        self._last_persisted: tuple | None = None
        # A manual `close` can race the poll loop's own stop/trim sell; the
        # lock plus the qty re-check below make the second seller a no-op.
        self._sell_lock = asyncio.Lock()

    async def _post(self, msg: str) -> None:
        if self.control is not None:
            await self.control.post(msg)

    def persist(self) -> None:
        """Mirror the play into the positions table (if it has a row). Cheap:
        writes only when something actually changed since the last persist."""
        p = self.play
        if self.audit is None or p.db_id is None:
            return
        snap = (p.state, p.qty, p.stop, p.peak_mid, p.disaster_order_id)
        if snap == self._last_persisted:
            return
        self.audit.update_position(
            p.db_id, state=p.state, qty=p.qty, stop_mid=p.stop,
            peak_mid=p.peak_mid, disaster_order_id=p.disaster_order_id,
        )
        self._last_persisted = snap

    async def arm_disaster_stop(self) -> None:
        p = self.play
        stop = round(p.fill_price * (1 - self.cfg["disaster_stop_pct"]), 2)
        limit = round(stop * (1 - self.cfg["exit_cross_pct"]), 2)
        st = await self.broker.place_disaster_stop(
            p.contract, p.qty, stop, limit, f"disaster-{uuid.uuid4()}"
        )
        if st is None:
            await self._post(
                f"⚠️ {p.contract}: broker can't hold a disaster stop — "
                f"soft stop at {p.stop:.2f} is the only stop"
            )
        else:
            p.disaster_order_id = st.broker_order_id

    async def _cancel_disaster_stop(self) -> None:
        if self.play.disaster_order_id:
            try:
                await self.broker.cancel(self.play.disaster_order_id)
            except BrokerError:
                pass  # already filled/cancelled; the sell loop reconciles qty
            self.play.disaster_order_id = None

    async def _sell(self, qty: int, reason: str) -> None:
        """Marketable limit at mid × (1 − exit_cross_pct), repriced until filled."""
        async with self._sell_lock:
            await self._sell_locked(qty, reason)

    async def _sell_locked(self, qty: int, reason: str) -> None:
        p = self.play
        if p.state == "CLOSED" or p.qty == 0:
            return  # a concurrent seller already flattened this play
        qty = min(qty, p.qty)
        await self._cancel_disaster_stop()
        remaining = qty
        while remaining > 0:
            quote = await self.broker.get_option_quote(p.contract)
            limit = round(quote.mid * (1 - self.cfg["exit_cross_pct"]), 2)
            st = await self.broker.sell_to_close(
                p.contract, remaining, limit, f"exit-{uuid.uuid4()}"
            )
            st = await self.broker.order_status(st.broker_order_id)
            if st.status is OrderStatus.FILLED:
                fill = st.avg_fill_price or limit
                p.realized += (fill - p.fill_price) * 100 * st.filled_qty
                p.qty -= st.filled_qty
                remaining -= st.filled_qty
                if self.audit is not None:
                    self.audit.record_decision(
                        p.alerter_id, "exit", f"{reason} @ {fill:.2f} x{st.filled_qty}"
                    )
                    self._audit_exit_fill(st, limit, fill)
                await self._post(
                    f"SOLD {st.filled_qty}x {p.contract} @ {fill:.2f} ({reason}) "
                    f"— realized {p.realized:+.2f}"
                )
            else:
                await self.broker.cancel(st.broker_order_id)
                await asyncio.sleep(self.cfg["reprice_seconds"])
        if p.qty == 0:
            p.state = "CLOSED"
        # After a trim, whatever remains is a runner protected at breakeven+.
        if p.qty > 0 and p.disaster_order_id is None:
            await self.arm_disaster_stop()
        self.persist()

    def _audit_exit_fill(self, st, limit: float, fill: float) -> None:
        """Audit-first: sell orders and their fills land in SQLite too."""
        p = self.play
        self.audit.conn.execute(
            "INSERT INTO orders (mode, alerter_id, client_order_id, contract, side, qty,"
            " limit_price, status, created_at) VALUES (?,?,?,?,?,?,?,?, datetime('now'))",
            (self.audit.mode, p.alerter_id, st.client_order_id, p.contract.key, "STC",
             st.filled_qty, limit, "filled"),
        )
        self.audit.conn.execute(
            "INSERT INTO fills (alerter_id, order_id, qty, price, fill_ts, created_at)"
            " SELECT ?, id, ?, ?, datetime('now'), datetime('now') FROM orders"
            " WHERE client_order_id=?",
            (p.alerter_id, st.filled_qty, fill, st.client_order_id),
        )
        self.audit.conn.commit()

    def _flatten_due(self, now: datetime) -> bool:
        hh, mm = map(int, str(self.cfg["flatten_time_local"]).split(":"))
        return now.timetz().replace(tzinfo=None) >= time(hh, mm)

    async def step(self, now: datetime) -> bool:
        """One poll. Returns True when the position is closed."""
        done = await self._step(now)
        self.persist()  # stop ratchets / trims / closes survive a restart
        return done

    async def _step(self, now: datetime) -> bool:
        p = self.play
        if p.state == "CLOSED" or p.qty == 0:
            p.state = "CLOSED"
            return True

        if self._flatten_due(now):
            await self._sell(p.qty, "hard flatten")
            return True

        try:
            quote = await self.broker.get_option_quote(p.contract)
        except BrokerError as e:
            await self._post(f"⚠️ {p.contract}: quote failed mid-flight ({e}); holding stop")
            return False
        mid = quote.mid
        p.peak_mid = max(p.peak_mid, mid)

        if p.state == "TP_ONLY":
            target = p.fill_price * (1 + self.cfg["single_contract_take_profit_pct"])
            if mid >= target:
                await self._sell(p.qty, f"tp_only target {target:.2f}")
                return p.state == "CLOSED"
            if mid <= p.stop:
                await self._sell(p.qty, f"stop {p.stop:.2f}")
                return p.state == "CLOSED"
            return False

        if p.state == "RUNNER":
            # Ratchet only: breakeven floor, trail below peak.
            p.stop = max(p.stop, p.fill_price,
                         round(p.peak_mid * (1 - self.cfg["trail_pct"]), 2))

        if mid <= p.stop:
            await self._sell(p.qty, f"stop {p.stop:.2f}")
            return p.state == "CLOSED"

        if p.state == "OPEN" and mid >= p.fill_price * (1 + self.cfg["trim_trigger_pct"]):
            await self._sell(1, f"trim +{self.cfg['trim_trigger_pct']:.0%}")
            if p.qty > 0:
                p.state = "RUNNER"
                p.stop = p.fill_price  # breakeven
                await self._post(
                    f"{p.contract}: runner x{p.qty}, stop to breakeven {p.stop:.2f}"
                )
            return p.state == "CLOSED"

        return False

    async def run(self, poll_interval_fn, clock=None) -> None:
        clock = clock or datetime.now
        while True:
            if await self.step(clock()):
                return
            await asyncio.sleep(poll_interval_fn())
