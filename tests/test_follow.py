"""Per-alerter follow/unfollow: unfollowed alerts are logged, never traded;
the choice persists in the audit db so a Keeper auto-revive can't undo it."""

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


def make_app(cfg, tmp_path, db_name="bot.db"):
    quotes = FakeBroker()
    quotes.now = NOW
    quotes.add_chain(C)
    quotes.set_quote(C, bid=0.68, ask=0.72, ts=NOW)
    broker = PaperBroker(quotes, starting_buying_power=5_000)
    audit = AuditDB(tmp_path / db_name)
    control = FakeControl()
    app = App(cfg, broker, NullSource(), control, audit, clock=lambda: NOW)
    return audit, control, app


def msg(text, alerter="bravo"):
    return RawMessage("test:1", alerter, NOW, "Bravo Alerts#1561", text)


async def test_both_alerters_followed_by_default(cfg, tmp_path):
    _, _, app = make_app(cfg, tmp_path)
    assert app.follows == {"bravo": True, "alpha": True}


async def test_unfollowed_alert_is_logged_never_traded(cfg, tmp_path):
    audit, control, app = make_app(cfg, tmp_path)
    assert (await app.cmd_unfollow("bravo")).startswith("⏸ unfollowed bravo")

    await app.handle_message(msg("@alert spy 746 call 0dte risky"))
    assert app.open_managers() == []
    rows = audit.conn.execute("SELECT action, reason FROM decisions").fetchall()
    assert rows == [("skip", "unfollowed")]
    # the alert row still lands (expectancy keeps accumulating)
    assert audit.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
    # and quietly — no skip post to the channel
    assert not any("SKIP" in p or "746" in p for p in control.posts)


async def test_unfollowed_ambiguous_does_not_post(cfg, tmp_path):
    audit, control, app = make_app(cfg, tmp_path)
    await app.cmd_unfollow("bravo")
    await app.handle_message(msg("@alert spy iwm 746 call 0dte"))  # two tickers
    assert not any("AMBIGUOUS" in p for p in control.posts)
    assert audit.conn.execute(
        "SELECT action FROM decisions"
    ).fetchone()[0] == "ambiguous"


async def test_refollow_trades_again(cfg, tmp_path):
    _, control, app = make_app(cfg, tmp_path)
    await app.cmd_unfollow("bravo")
    assert (await app.cmd_follow("bravo")).startswith("✅ following bravo")
    await app.handle_message(msg("@alert spy 746 call 0dte risky"))
    assert len(app.open_managers()) == 1


async def test_follow_state_survives_restart(cfg, tmp_path):
    audit, _, app = make_app(cfg, tmp_path)
    await app.cmd_unfollow("alpha")
    audit.close()
    _, _, app2 = make_app(cfg, tmp_path)  # same db — a fresh process
    assert app2.follows == {"bravo": True, "alpha": False}


async def test_unknown_alerter_rejected(cfg, tmp_path):
    _, _, app = make_app(cfg, tmp_path)
    assert "unknown alerter" in await app.cmd_follow("nobody")
    assert "usage" in await app.cmd_unfollow("")


async def test_alerters_lists_state_and_counts(cfg, tmp_path):
    _, _, app = make_app(cfg, tmp_path)
    await app.cmd_unfollow("alpha")
    await app.handle_message(msg("@alert spy 746 call 0dte risky"))
    out = await app.cmd_alerters()
    assert "bravo: ✅ following — 1 alert(s) today, 1 open play(s)" in out
    assert "alpha: ⏸ unfollowed" in out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("alerters", "alerters"),
        ("follow alpha", "follow alpha"),
        ("UNFOLLOW bravo", "unfollow bravo"),
        ("follower count", None),  # not a command
    ],
)
def test_route_command_accepts_follow_commands(text, expected):
    assert route_command(text, 1, 1) == expected


def test_route_command_still_requires_authorization():
    assert route_command("unfollow alpha", 2, 1) == "unauthorized"
