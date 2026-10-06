"""Orchestrator: source → parse → audit → risk gate → execute → manage,
with the control channel notified of every event. Fail closed everywhere."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, time
from zoneinfo import ZoneInfo

from audit.db import AuditDB
from control.bot import ControlChannel, Watchdog
from execution.broker import BrokerAdapter, BrokerError, OptionContract, OrderStatus
from ingestion.source import NotificationSource, RawMessage
from manage.position import Play, PositionManager
from parsing.models import ParseKind
from parsing.parser import parse_message
from risk.gate import DayState, gate
from settings import Config

ENTRY_FILL_TIMEOUT_S = 20.0
MARKET_OPEN = time(6, 30)


class App:
    def __init__(
        self,
        cfg: Config,
        broker: BrokerAdapter,
        source: NotificationSource,
        control: ControlChannel,
        audit: AuditDB,
        clock=None,
    ) -> None:
        self.cfg = cfg
        self.broker = broker
        self.source = source
        self.control = control
        self.audit = audit
        self.tz = ZoneInfo(cfg.timezone)
        self.clock = clock or (lambda: datetime.now(self.tz))
        self.state = DayState(day=self.clock().date())
        # Follow state: which alerters' NEW entries get traded. config
        # `enabled` means onboarded (ingested + audited); follow means traded.
        # Persisted choices win over the config default so a Keeper auto-
        # revive can't silently re-follow someone Logan turned off.
        saved = audit.follow_states()
        self.follows: dict[str, bool] = {
            a.alerter_id: saved.get(a.alerter_id, True)
            for a in cfg.enabled_alerters
        }
        self.paused = False
        self.managers: list[PositionManager] = []
        self.tasks: list[asyncio.Task] = []
        self.watchdog = Watchdog(control, cfg.raw["watchdog"]["heartbeat_seconds"])

    # ------------------------------------------------------------------ util
    def _now(self) -> datetime:
        return self.clock()

    def _roll_day(self) -> None:
        today = self._now().date()
        if today != self.state.day:
            self.state = DayState(day=today, halted=self.state.halted)

    def open_managers(self) -> list[PositionManager]:
        return [m for m in self.managers if m.play.state != "CLOSED"]

    def poll_interval(self) -> float:
        q = self.cfg.raw["quotes"]
        n = max(1, len(self.open_managers()))
        return max(float(q["poll_seconds"]), n * 60.0 / q["broker_rate_limit_per_min"])

    # ------------------------------------------------------------- pipeline
    async def handle_message(self, msg: RawMessage) -> None:
        self.watchdog.beat()
        self._roll_day()
        if msg.text.lstrip().startswith("»"):
            return  # our own control post echoed back as a toast
        rm_id = self.audit.record_raw_message(
            msg.alerter_id, msg.source_id, msg.author, msg.ts.isoformat(), msg.text
        )
        profile = self.cfg.alerters[msg.alerter_id]
        result = parse_message(msg.text, profile, self.cfg.parser)

        if result.kind is ParseKind.NOISE:
            self.audit.record_decision(
                msg.alerter_id, "noise", result.reason, raw_message_id=rm_id
            )
            return
        followed = self.follows.get(msg.alerter_id, True)
        if result.kind is ParseKind.AMBIGUOUS:
            self.audit.record_decision(
                msg.alerter_id, "ambiguous", result.reason, raw_message_id=rm_id
            )
            if followed:  # unfollowed alerters shouldn't spam the channel
                await self.control.post(
                    f"⚠️ AMBIGUOUS alert — no trade (fail closed): {result.reason}\n"
                    f"> {result.raw[:200]}"
                )
            return

        alert = result.alert
        alert.ts = msg.ts
        alert_id = self.audit.record_alert(
            msg.alerter_id, rm_id, alert.ticker, alert.strike, alert.side,
            alert.expiry, alert.alert_price, ";".join(alert.tags), alert.raw,
        )

        if not followed:
            # Still parsed and audited (per-alerter expectancy keeps
            # accumulating) — just never traded, and quietly.
            self.audit.record_decision(
                msg.alerter_id, "skip", "unfollowed", alert_id=alert_id
            )
            return

        if self.paused:
            self.audit.record_decision(msg.alerter_id, "skip", "paused", alert_id=alert_id)
            await self.control.post(f"paused — skipping {alert.ticker} {alert.strike} {alert.side}")
            return

        decision = await gate(alert, self.broker, self.cfg.raw, self.state, self._now())
        self.audit.record_decision(
            msg.alerter_id, decision.action, decision.reason, alert_id=alert_id
        )
        if decision.action != "enter":
            await self.control.post(
                f"SKIP {alert.ticker} {alert.strike} {alert.side}: {decision.reason}"
            )
            return
        await self._enter(msg.alerter_id, alert_id, decision)

    async def _enter(self, alerter_id: str, alert_id: int, decision) -> None:
        c = decision.contract
        coid = f"entry-{uuid.uuid4()}"
        try:
            st = await self.broker.buy_to_open(c, decision.qty, decision.limit_price, coid)
        except BrokerError as e:
            await self.control.post(f"❌ entry order failed for {c}: {e}")
            return
        self.audit.conn.execute(
            "INSERT INTO orders (mode, alerter_id, client_order_id, contract, side, qty,"
            " limit_price, status, created_at) VALUES (?,?,?,?,?,?,?,?, datetime('now'))",
            (self.audit.mode, alerter_id, coid, c.key, "BTO", decision.qty,
             decision.limit_price, st.status.value),
        )
        self.audit.conn.commit()

        deadline = asyncio.get_event_loop().time() + ENTRY_FILL_TIMEOUT_S
        while st.status not in (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED):
            if asyncio.get_event_loop().time() > deadline:
                await self.broker.cancel(st.broker_order_id)
                await self.control.post(
                    f"entry for {c} unfilled after {ENTRY_FILL_TIMEOUT_S:.0f}s — "
                    "cancelled, skipping"
                )
                return
            await asyncio.sleep(0.5)
            st = await self.broker.order_status(st.broker_order_id)

        if st.status is not OrderStatus.FILLED:
            await self.control.post(f"entry for {c} {st.status.value} — no position")
            return

        fill = st.avg_fill_price or decision.limit_price
        self.audit.conn.execute(
            "INSERT INTO fills (alerter_id, order_id, qty, price, fill_ts, created_at)"
            " SELECT ?, id, ?, ?, ?, datetime('now') FROM orders WHERE client_order_id=?",
            (alerter_id, st.filled_qty, fill, self._now().isoformat(), coid),
        )
        self.audit.conn.commit()
        self.state.entries_today += 1
        self.state.played_keys.add(c.key)

        play = Play(alerter_id=alerter_id, contract=c, qty=st.filled_qty,
                    fill_price=fill, tp_only=decision.tp_only)
        mgr = PositionManager(self.broker, self.cfg.raw, play,
                              control=self.control, audit=self.audit)
        play.db_id = self.audit.record_position_open(
            alerter_id, c.key, play.state, play.qty, fill, play.stop,
            play.peak_mid, self._now().isoformat(),
        )
        self.managers.append(mgr)
        await mgr.arm_disaster_stop()
        mgr.persist()
        self.tasks.append(asyncio.create_task(mgr.run(self.poll_interval, self.clock)))
        mode = "tp_only x1" if decision.tp_only else f"x{st.filled_qty}"
        await self.control.post(
            f"ENTERED {c} {mode} @ {fill:.2f} — stop {play.stop:.2f} "
            f"(entry #{self.state.entries_today} today)"
        )

    # ------------------------------------------------------------- recovery
    async def recover(self) -> None:
        """Rebuild state after a restart: today's entry counters from the
        audit trail, and a PositionManager for every play left open in the
        positions table, reconciled against what the broker actually holds."""
        # Market hours (06:30–13:00 PT) map to the same UTC calendar date, so
        # the local date matches created_at's UTC date prefix.
        day = self._now().date().isoformat()
        entered = self.audit.conn.execute(
            "SELECT o.contract FROM orders o JOIN fills f ON f.order_id = o.id"
            " WHERE o.side='BTO' AND substr(o.created_at, 1, 10) = ?", (day,)
        ).fetchall()
        for (key,) in entered:
            self.state.played_keys.add(key)
        self.state.entries_today = len(entered)

        rows = self.audit.open_positions()
        if not rows:
            return
        try:
            held = await self.broker.positions()  # None = unknown, keep managing
        except BrokerError:
            held = None
        recovered: list[str] = []
        for row in rows:
            try:
                c = OptionContract.from_key(row["contract"])
            except (ValueError, IndexError):
                self.audit.update_position(row["id"], "CLOSED", 0, 0.0, 0.0, None)
                await self.control.post(
                    f"⚠️ recovery: unreadable contract key {row['contract']!r} — "
                    "row closed, check the broker manually"
                )
                continue
            if c.expiry < self._now().date():
                self.audit.update_position(
                    row["id"], "CLOSED", 0, row["stop_mid"] or 0.0,
                    row["peak_mid"] or 0.0, None,
                )
                await self.control.post(f"recovery: {c} expired while bot was down — row closed")
                continue
            qty = row["qty"]
            if held is not None:
                broker_qty = held.get(row["contract"], 0)
                if broker_qty <= 0:
                    self.audit.update_position(
                        row["id"], "CLOSED", 0, row["stop_mid"] or 0.0,
                        row["peak_mid"] or 0.0, None,
                    )
                    await self.control.post(
                        f"recovery: {c} not held at broker (disaster stop or manual "
                        "close while bot was down?) — orphan row closed"
                    )
                    continue
                if broker_qty < qty:
                    await self.control.post(
                        f"recovery: {c} broker holds x{broker_qty}, expected x{qty} "
                        "— managing the broker's quantity"
                    )
                    qty = broker_qty
            play = Play(
                alerter_id=row["alerter_id"], contract=c, qty=qty,
                fill_price=row["avg_price"], tp_only=(row["state"] == "TP_ONLY"),
                state=row["state"], stop=row["stop_mid"] or 0.0,
                peak_mid=row["peak_mid"] or 0.0, db_id=row["id"],
            )
            mgr = PositionManager(self.broker, self.cfg.raw, play,
                                  control=self.control, audit=self.audit)
            # Replace any stale parachute with one sized to the recovered qty.
            if row["disaster_order_id"]:
                try:
                    await self.broker.cancel(row["disaster_order_id"])
                except BrokerError:
                    pass
            play.disaster_order_id = None
            self.managers.append(mgr)
            await mgr.arm_disaster_stop()
            mgr.persist()
            self.tasks.append(asyncio.create_task(mgr.run(self.poll_interval, self.clock)))
            recovered.append(f"{c} x{qty} [{play.state}] stop {play.stop:.2f}")
        if recovered:
            await self.control.post(
                "🔁 recovered open play(s) from restart:\n" + "\n".join(recovered)
            )

    # ------------------------------------------------------------- commands
    async def cmd_close(self, args: str = "") -> str:
        """Scoped manual flatten of ONE position via the normal exit machinery
        (kill stays all-or-nothing by design). Usage: close spy 746c"""
        m = re.fullmatch(r"\s*([a-z]+)\s*(\d+(?:\.\d+)?)\s*([cp])\s*", args.lower())
        if not m:
            return "usage: close <ticker> <strike><c|p> — e.g. `close spy 746c`"
        ticker = m.group(1).upper()
        strike, side = float(m.group(2)), "CALL" if m.group(3) == "c" else "PUT"
        matches = [
            mg for mg in self.open_managers()
            if mg.play.contract.ticker == ticker
            and mg.play.contract.strike == strike
            and mg.play.contract.side == side
        ]
        if not matches:
            have = ", ".join(str(mg.play.contract) for mg in self.open_managers()) or "none"
            return f"no open play matches {args.strip()} — open: {have}"
        mgr = matches[0]
        try:
            await mgr._sell(mgr.play.qty, "manual close")
        except BrokerError as e:
            return f"❌ close failed for {mgr.play.contract}: {e}"
        return f"closed {mgr.play.contract} — realized {mgr.play.realized:+.2f}"

    # ------------------------------------------------------- follow control
    def _set_follow(self, args: str, followed: bool) -> str:
        name = args.strip().lower()
        known = ", ".join(sorted(self.follows)) or "none configured"
        if not name:
            verb = "follow" if followed else "unfollow"
            return f"usage: {verb} <alerter> — configured: {known}"
        if name not in self.follows:
            return f"unknown alerter {name!r} — configured: {known}"
        self.follows[name] = followed
        self.audit.set_followed(name, followed)
        if followed:
            return f"✅ following {name} — new entries will be traded"
        open_n = sum(1 for m in self.open_managers() if m.play.alerter_id == name)
        note = f" ({open_n} open play(s) keep being managed to exit)" if open_n else ""
        return f"⏸ unfollowed {name} — alerts still logged, no new entries{note}"

    async def cmd_follow(self, args: str = "") -> str:
        return self._set_follow(args, True)

    async def cmd_unfollow(self, args: str = "") -> str:
        return self._set_follow(args, False)

    async def cmd_alerters(self, args: str = "") -> str:
        lines = []
        for name in sorted(self.follows):
            # created_at is UTC; during market hours (06:30–13:00 PT) the UTC
            # calendar date matches the local one, so date('now') is today.
            n_alerts = self.audit.conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE alerter_id=?"
                " AND substr(created_at, 1, 10) = date('now')",
                (name,),
            ).fetchone()[0]
            open_n = sum(
                1 for m in self.open_managers() if m.play.alerter_id == name
            )
            flag = "✅ following" if self.follows[name] else "⏸ unfollowed (logged, not traded)"
            lines.append(
                f"{name}: {flag} — {n_alerts} alert(s) today, {open_n} open play(s)"
            )
        return "\n".join(lines) or "no alerters configured"

    async def cmd_help(self, args: str = "") -> str:
        return (
            "```\n"
            "status           mode, open positions + stops, realized P&L today\n"
            "alerters         each alerter: following or not, alerts today\n"
            "follow alpha     trade this alerter's new entries\n"
            "unfollow alpha   stop NEW entries (still logged; open plays managed)\n"
            "pause            stop NEW entries from everyone\n"
            "resume           resume taking entries\n"
            "close spy 746c   flatten just that one position\n"
            "kill             cancel all orders, flatten EVERYTHING, halt until restart\n"
            "restart          reload code+config (flat only; `restart force` overrides)\n"
            "help             this list\n"
            "```\n"
            "🟢 morning post = bot started clean · dashboard: tools\\dashboard.vbs\n"
            "full ops guide: docs/operations.md"
        )

    def _schedule_restart(self) -> None:
        """Spawn the successor python directly, then exit. No shell helpers —
        a cmd/timeout/wscript chain proved fragile launched from a hidden
        dying process (WSH 'not enough memory resources'). The successor's
        singleton lock retries ~15s, so it simply waits out our exit.
        Tests replace this method — the real one ends the process."""
        import os
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        py = root / ".venv" / "Scripts" / "python.exe"
        log = open(root / "data" / "restart.log", "ab")  # noqa: SIM115
        # CREATE_NO_WINDOW, not DETACHED_PROCESS: the successor must get a
        # hidden console the venv launcher's child can inherit — with no
        # console at all, the real interpreter allocates a VISIBLE one.
        subprocess.Popen(
            [str(py), "-u", "run.py"], cwd=str(root),
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        # os._exit skips cleanup by design: audit writes commit as they happen,
        # and a clean asyncio teardown isn't worth a hung shutdown here.
        asyncio.get_event_loop().call_later(2, os._exit, 0)

    async def cmd_restart(self, args: str = "") -> str:
        open_mgrs = self.open_managers()
        if open_mgrs and args.strip() != "force":
            plays = ", ".join(str(m.play.contract) for m in open_mgrs)
            return (f"refusing restart with open position(s): {plays} — "
                    "stops would go unmanaged while the process cycles. "
                    "`restart force` overrides (recovery will re-adopt them).")
        self._schedule_restart()
        return "restarting — new code/config loads now; expect 🟢 in ~30s"

    async def cmd_kill(self, args: str = "") -> str:
        self.state.halted = True
        open_mgrs = self.open_managers()
        for oid, st in list(getattr(self.broker, "orders", {}).items()):
            if st.status is OrderStatus.NEW:
                try:
                    await self.broker.cancel(oid)
                except BrokerError:
                    pass
        for m in open_mgrs:
            try:
                await m._sell(m.play.qty, "KILL")
            except BrokerError as e:
                await self.control.post(f"❌ kill: failed to flatten {m.play.contract}: {e}")
        return (f"KILL: flattened {len(open_mgrs)} position(s), cancelled resting "
                f"orders, entries halted until restart")

    async def cmd_status(self, args: str = "") -> str:
        lines = [f"mode={self.cfg.mode} paused={self.paused} halted={self.state.halted} "
                 f"entries_today={self.state.entries_today}"]
        open_mgrs = self.open_managers()
        if not open_mgrs:
            lines.append("no open positions")
        for m in open_mgrs:
            p = m.play
            try:
                q = await self.broker.get_option_quote(p.contract)
                mark = f"mid {q.mid:.2f} ({(q.mid / p.fill_price - 1):+.0%})"
            except BrokerError:
                mark = "no quote"
            lines.append(
                f"{p.contract} x{p.qty} @ {p.fill_price:.2f} [{p.state}] "
                f"stop {p.stop:.2f} — {mark} realized {p.realized:+.2f}"
            )
        total = sum(m.play.realized for m in self.managers)
        lines.append(f"realized today: {total:+.2f}")
        return "\n".join(lines)

    async def cmd_pause(self, args: str = "") -> str:
        self.paused = True
        return "paused — no new entries (open positions still managed)"

    async def cmd_resume(self, args: str = "") -> str:
        self.paused = False
        return "resumed"

    # ------------------------------------------------------------------ run
    async def startup_banner(self) -> None:
        now = self._now()
        msg = f"🟢 bot up — mode={self.cfg.mode}, {now:%Y-%m-%d %H:%M:%S %Z}"
        hh, mm = map(int, str(self.cfg.raw["exits"]["flatten_time_local"]).split(":"))
        if MARKET_OPEN < now.time() < time(hh, mm):
            msg += "\n⚠️ started mid-session — any alerts before startup were missed"
        await self.control.post(msg)

    async def _heartbeat(self) -> None:
        # Beats while the event loop is healthy; a PC sleep or process stall
        # shows up as a gap the watchdog reports on wake.
        hb = self.cfg.raw["watchdog"]["heartbeat_seconds"]
        while True:
            self.watchdog.beat()
            await asyncio.sleep(hb / 2)

    async def run(self) -> None:
        await self.startup_banner()
        await self.recover()
        self.tasks.append(asyncio.create_task(self.watchdog.run()))
        self.tasks.append(asyncio.create_task(self._heartbeat()))
        async for msg in self.source.stream():
            try:
                await self.handle_message(msg)
            except Exception as e:  # fail closed on THIS message, keep running
                self.audit.record_decision(msg.alerter_id, "error", repr(e)[:300])
                await self.control.post(f"❌ error handling message ({e!r}) — no trade")
