"""Phase 2 gate rehearsal: alert text → paper fill + control post + audit rows,
end to end through App.handle_message with fakes (the live gate run swaps in
the toast source, Discord control, and real Webull quotes)."""

from datetime import date, datetime

import pytest

from app import App
from audit.db import AuditDB
from control.bot import route_command
from execution.broker import OptionContract
from execution.fake import FakeBroker
from execution.paper import PaperBroker
from ingestion.source import RawMessage

NOW = datetime(2026, 8, 17, 7, 0, 0)
C = OptionContract("SPY", 746.0, "CALL", date(2026, 8, 17))


class FakeControl:
    def __init__(self):
        self.posts = []

    async def post(self, msg):
        self.posts.append(msg)


class NullSource:
    async def stream(self):
        return
        yield


@pytest.fixture()
def world(cfg, tmp_path):
    quotes = FakeBroker()
    quotes.now = NOW
    quotes.add_chain(C)
    quotes.set_quote(C, bid=0.68, ask=0.72, ts=NOW)
    broker = PaperBroker(quotes, starting_buying_power=5_000)
    audit = AuditDB(tmp_path / "bot.db")
    control = FakeControl()
    app = App(cfg, broker, NullSource(), control, audit, clock=lambda: NOW)
    return quotes, broker, audit, control, app


def msg(text, ts=NOW):
    return RawMessage("test:1", "bravo", ts, "Bravo Alerts#1561", text)


async def test_alert_to_paper_fill_with_audit_trail(world):
    quotes, broker, audit, control, app = world
    await app.handle_message(msg("@alert spy 746 call 0dte risky"))

    assert len(app.open_managers()) == 1
    play = app.managers[0].play
    assert play.qty == 2 and play.fill_price == 0.72
    assert any("ENTERED" in p for p in control.posts)

    # audit-first: raw message, alert, decision, order, fill all recorded
    counts = {
        t: audit.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
        for t in ("raw_messages", "alerts", "decisions", "orders", "fills")
    }
    assert counts == {"raw_messages": 1, "alerts": 1, "decisions": 1,
                      "orders": 1, "fills": 1}
    assert app.state.entries_today == 1


async def test_noise_and_ambiguous_never_trade(world):
    quotes, broker, audit, control, app = world
    await app.handle_message(msg("@alert 100% TP WHENEVER"))
    await app.handle_message(msg("@alert spy iwm 746 call 0dte"))  # two tickers
    assert app.open_managers() == []
    assert app.state.entries_today == 0
    actions = [r[0] for r in audit.conn.execute("SELECT action FROM decisions")]
    assert actions == ["noise", "ambiguous"]
    assert any("AMBIGUOUS" in p for p in control.posts)


async def test_same_day_realert_logged_not_traded(world):
    quotes, broker, audit, control, app = world
    await app.handle_message(msg("@alert spy 746 call 0dte risky"))
    await app.handle_message(msg("@alert spy 746 call 0dte re entry"))
    assert app.state.entries_today == 1
    assert any("re-alert" in p for p in control.posts)


async def test_pause_resume_and_kill(world):
    quotes, broker, audit, control, app = world
    assert "paused" in await app.cmd_pause()
    await app.handle_message(msg("@alert spy 746 call 0dte risky"))
    assert app.state.entries_today == 0
    await app.cmd_resume()
    await app.handle_message(msg("@alert spy 746 call 0dte risky"))
    assert app.state.entries_today == 1

    out = await app.cmd_kill()
    assert "flattened 1" in out
    assert app.managers[0].play.state == "CLOSED"
    assert app.state.halted
    await app.handle_message(msg("@alert iwm 289 put 0dte risky"))
    assert app.state.entries_today == 1  # halted — nothing new


async def test_exits_are_audited_as_orders_and_fills(world):
    quotes, broker, audit, control, app = world
    await app.handle_message(msg("@alert spy 746 call 0dte risky"))
    mgr = app.open_managers()[0]
    quotes.set_quote(C, bid=0.50, ask=0.52, ts=NOW)  # mid below stop
    assert await mgr.step(NOW) is True
    rows = audit.conn.execute(
        "SELECT side, qty, status FROM orders ORDER BY id"
    ).fetchall()
    assert rows == [("BTO", 2, "filled"), ("STC", 2, "filled")]
    assert audit.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2


async def test_status_reports_positions(world):
    quotes, broker, audit, control, app = world
    await app.handle_message(msg("@alert spy 746 call 0dte risky"))
    out = await app.cmd_status()
    assert "SPY" in out and "stop" in out and "mode=paper" in out


def test_command_routing_authorization():
    assert route_command("kill", 111, 111) == "kill"
    assert route_command("  STATUS ", 111, 111) == "status"
    assert route_command("kill", 222, 111) == "unauthorized"
    assert route_command("hello", 111, 111) is None
    assert route_command("close SPY  746c", 111, 111) == "close spy 746c"
    assert route_command("close", 111, 111) == "close"
    assert route_command("close spy 746c", 222, 111) == "unauthorized"
    assert route_command("closed the door", 111, 111) is None
    assert route_command("restart", 111, 111) == "restart"
    assert route_command("Restart  FORCE", 111, 111) == "restart force"
    assert route_command("restart", 222, 111) == "unauthorized"
    assert route_command("restarting soon", 111, 111) is None
    assert route_command("help", 111, 111) == "help"
    assert route_command("help", 222, 111) == "unauthorized"


async def test_help_lists_every_wired_command(world):
    quotes, broker, audit, control, app = world
    out = await app.cmd_help()
    for cmd in ("status", "pause", "resume", "close", "kill", "restart", "help"):
        assert cmd in out
    assert len("» " + out) <= 1900  # fits in one control post untruncated


async def test_restart_refuses_with_open_position_unless_forced(world):
    quotes, broker, audit, control, app = world
    scheduled = []
    app._schedule_restart = lambda: scheduled.append(1)

    assert "restarting" in await app.cmd_restart("")  # flat: allowed
    assert scheduled == [1]

    await app.handle_message(msg("@alert spy 746 call 0dte risky"))
    out = await app.cmd_restart("")
    assert "refusing" in out and scheduled == [1]  # open play: refused
    assert "restarting" in await app.cmd_restart("force")
    assert scheduled == [1, 1]


async def test_close_command_flattens_one_position(world):
    quotes, broker, audit, control, app = world
    await app.handle_message(msg("@alert spy 746 call 0dte risky"))
    assert "usage" in await app.cmd_close("")
    assert "no open play" in await app.cmd_close("spy 999c")
    assert "no open play" in await app.cmd_close("spy 746p")  # wrong side

    out = await app.cmd_close("spy 746c")
    assert out.startswith("closed")
    assert app.open_managers() == []
    assert app.managers[0].play.state == "CLOSED"
    assert not app.state.halted  # scoped: close never halts entries
    # exit audited like any other sell
    rows = audit.conn.execute("SELECT side FROM orders ORDER BY id").fetchall()
    assert rows == [("BTO",), ("STC",)]


async def test_concurrent_sells_never_double_close(world):
    import asyncio

    quotes, broker, audit, control, app = world
    await app.handle_message(msg("@alert spy 746 call 0dte risky"))
    mgr = app.open_managers()[0]
    await asyncio.gather(
        mgr._sell(mgr.play.qty, "manual close"),
        mgr._sell(mgr.play.qty, "stop"),
    )
    stc_qty = audit.conn.execute(
        "SELECT COALESCE(SUM(qty), 0) FROM orders WHERE side='STC'"
    ).fetchone()[0]
    assert stc_qty == 2  # exactly the position size, never more
