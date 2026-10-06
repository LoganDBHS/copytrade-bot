"""Position recovery: plays persist to the positions table as they change,
and a restarted App rebuilds managers from that table, reconciled against
what the broker actually holds. Built before go-live — a crash mid-position
must never leave a live position unmanaged."""

from datetime import date, datetime, timedelta

import pytest

from app import App
from audit.db import AuditDB
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


def make_app(cfg, audit, broker):
    return App(cfg, broker, NullSource(), FakeControl(), audit, clock=lambda: NOW)


def make_broker():
    quotes = FakeBroker()
    quotes.now = NOW
    quotes.add_chain(C)
    quotes.set_quote(C, bid=0.68, ask=0.72, ts=NOW)
    return quotes, PaperBroker(quotes, starting_buying_power=5_000)


def msg(text, ts=NOW):
    return RawMessage("test:1", "bravo", ts, "Bravo Alerts#1561", text)


def test_contract_key_round_trip():
    for c in (C, OptionContract("IWM", 289.5, "PUT", date(2026, 12, 4))):
        assert OptionContract.from_key(c.key) == c


async def test_entry_persists_open_position_row(cfg, tmp_path):
    audit = AuditDB(tmp_path / "bot.db")
    _, broker = make_broker()
    app = make_app(cfg, audit, broker)
    await app.handle_message(msg("@alert spy 746 call 0dte risky"))

    rows = audit.open_positions()
    assert len(rows) == 1
    row = rows[0]
    assert row["contract"] == C.key and row["qty"] == 2
    assert row["avg_price"] == pytest.approx(0.72)
    assert row["stop_mid"] == pytest.approx(0.54)  # 0.72 * (1 - 0.25)
    assert app.managers[0].play.db_id == row["id"]


async def test_close_persists_and_clears_row(cfg, tmp_path):
    audit = AuditDB(tmp_path / "bot.db")
    quotes, broker = make_broker()
    app = make_app(cfg, audit, broker)
    await app.handle_message(msg("@alert spy 746 call 0dte risky"))
    quotes.set_quote(C, bid=0.50, ask=0.52, ts=NOW)  # mid below stop
    assert await app.managers[0].step(NOW) is True
    assert audit.open_positions() == []
    state, closed_at = audit.conn.execute(
        "SELECT state, closed_at FROM positions"
    ).fetchone()
    assert state == "CLOSED" and closed_at is not None


async def test_restart_recovers_open_play_and_day_state(cfg, tmp_path):
    audit = AuditDB(tmp_path / "bot.db")
    _, broker = make_broker()
    app1 = make_app(cfg, audit, broker)
    await app1.handle_message(msg("@alert spy 746 call 0dte risky"))
    # orders.created_at is real UTC wall-clock; pin it to the simulated day
    # so recover()'s today-filter sees this entry
    audit.conn.execute("UPDATE orders SET created_at = ?", (NOW.isoformat(),))
    audit.conn.commit()
    # simulate crash: fresh App on the same db; PaperBroker can't list
    # holdings (positions() -> None = unknown) so recovery resumes managing
    _, broker2 = make_broker()
    app2 = make_app(cfg, audit, broker2)
    await app2.recover()

    assert app2.state.entries_today == 1
    assert C.key in app2.state.played_keys
    assert len(app2.open_managers()) == 1
    play = app2.managers[0].play
    assert (play.contract, play.qty) == (C, 2)
    assert play.fill_price == pytest.approx(0.72)
    assert play.stop == pytest.approx(0.54)
    assert play.db_id is not None
    assert any("recovered" in p for p in app2.control.posts)
    # same-day re-alert of the recovered contract still refuses to re-enter
    await app2.handle_message(msg("@alert spy 746 call 0dte risky"))
    assert app2.state.entries_today == 1
    for t in app2.tasks:
        t.cancel()


async def test_recovery_preserves_ratcheted_runner_stop(cfg, tmp_path):
    audit = AuditDB(tmp_path / "bot.db")
    quotes, broker = make_broker()
    app1 = make_app(cfg, audit, broker)
    await app1.handle_message(msg("@alert spy 746 call 0dte risky"))
    quotes.set_quote(C, bid=0.99, ask=1.01, ts=NOW)  # +25% -> trim to runner
    await app1.managers[0].step(NOW)
    p1 = app1.managers[0].play
    assert p1.state == "RUNNER" and p1.qty == 1

    _, broker2 = make_broker()
    app2 = make_app(cfg, audit, broker2)
    await app2.recover()
    play = app2.managers[0].play
    assert play.state == "RUNNER" and play.qty == 1
    assert play.stop == pytest.approx(p1.stop)  # breakeven+, NOT re-widened
    assert play.peak_mid == pytest.approx(p1.peak_mid)
    for t in app2.tasks:
        t.cancel()


async def test_recovery_closes_orphan_when_broker_flat(cfg, tmp_path):
    audit = AuditDB(tmp_path / "bot.db")
    _, broker = make_broker()
    app1 = make_app(cfg, audit, broker)
    await app1.handle_message(msg("@alert spy 746 call 0dte risky"))

    quotes2 = FakeBroker()
    quotes2.now = NOW
    quotes2.holdings = {}  # broker CAN say, and says: flat
    app2 = make_app(cfg, audit, quotes2)
    await app2.recover()
    assert app2.open_managers() == []
    assert audit.open_positions() == []
    assert any("orphan" in p for p in app2.control.posts)


async def test_recovery_adopts_smaller_broker_qty(cfg, tmp_path):
    audit = AuditDB(tmp_path / "bot.db")
    _, broker = make_broker()
    app1 = make_app(cfg, audit, broker)
    await app1.handle_message(msg("@alert spy 746 call 0dte risky"))

    quotes2 = FakeBroker()
    quotes2.now = NOW
    quotes2.holdings = {C.key: 1}  # disaster stop partially filled while down
    app2 = make_app(cfg, audit, quotes2)
    await app2.recover()
    assert len(app2.open_managers()) == 1
    assert app2.managers[0].play.qty == 1
    for t in app2.tasks:
        t.cancel()


async def test_recovery_closes_expired_rows_without_broker_calls(cfg, tmp_path):
    audit = AuditDB(tmp_path / "bot.db")
    old = OptionContract("SPY", 700.0, "CALL", (NOW - timedelta(days=3)).date())
    audit.record_position_open(
        "bravo", old.key, "OPEN", 2, 0.50, 0.38, 0.50, NOW.isoformat()
    )
    quotes = FakeBroker()
    quotes.now = NOW
    app = make_app(cfg, audit, quotes)
    await app.recover()
    assert app.open_managers() == []
    assert audit.open_positions() == []
    assert any("expired" in p for p in app.control.posts)
