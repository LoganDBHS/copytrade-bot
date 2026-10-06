"""Risk gate: every check fails closed, in order."""

from datetime import date, datetime, timedelta

import pytest

from execution.broker import OptionContract
from execution.fake import FakeBroker
from parsing.models import ParsedAlert
from risk.gate import DayState, gate, resolve_expiry

NOW = datetime(2026, 8, 17, 7, 0, 0)  # Monday, mid-morning PT
TODAY = NOW.date()


def alert(ticker="SPY", strike=746.0, side="CALL", expiry="DTE0",
          price=None, ts=NOW) -> ParsedAlert:
    return ParsedAlert("bravo", ticker, strike, side, expiry, price,
                       [], "@alert test", ts=ts)


@pytest.fixture()
def broker():
    b = FakeBroker(buying_power=10_000)
    b.now = NOW
    c = OptionContract("SPY", 746.0, "CALL", TODAY)
    b.add_chain(c)
    b.set_quote(c, bid=0.68, ask=0.72)
    return b


@pytest.fixture()
def state():
    return DayState(day=TODAY)


async def test_clean_entry(broker, state, cfg):
    d = await gate(alert(), broker, cfg.raw, state, NOW)
    assert d.action == "enter"
    assert d.qty == 2 and not d.tp_only
    assert d.limit_price == 0.72  # priceless policy: enter at ask


async def test_stale_alert_skipped(broker, state, cfg):
    d = await gate(alert(ts=NOW - timedelta(seconds=90)), broker, cfg.raw, state, NOW)
    assert d.action == "skip" and "stale alert" in d.reason


async def test_contract_missing_from_chain(broker, state, cfg):
    # Midweek no-expiry MSFT: assumes today, but today isn't a Friday expiry.
    d = await gate(alert(ticker="MSFT", strike=500.0, expiry=""), broker, cfg.raw, state, NOW)
    assert d.action == "skip" and "chain" in d.reason


async def test_no_quote_fails_closed(broker, state, cfg):
    c = OptionContract("SPY", 747.0, "CALL", TODAY)
    broker.add_chain(c)  # in chain but no quote
    d = await gate(alert(strike=747.0), broker, cfg.raw, state, NOW)
    assert d.action == "skip" and "no quote" in d.reason


async def test_wide_spread_skipped(broker, state, cfg):
    c = OptionContract("SPY", 746.0, "CALL", TODAY)
    broker.set_quote(c, bid=0.50, ask=0.72)  # ~36% of mid
    d = await gate(alert(), broker, cfg.raw, state, NOW)
    assert d.action == "skip" and "spread" in d.reason


async def test_price_band(broker, state, cfg):
    # ask 0.72 > 0.60 * 1.10 → skip
    d = await gate(alert(price=0.60), broker, cfg.raw, state, NOW)
    assert d.action == "skip" and "band" in d.reason
    # ask 0.72 <= 0.70 * 1.10 = 0.77 → enter, limit = min(ask, band) = ask
    d = await gate(alert(price=0.70), broker, cfg.raw, state, NOW)
    assert d.action == "enter" and d.limit_price == 0.72


async def test_below_alert_price_always_fine(broker, state, cfg):
    d = await gate(alert(price=5.00), broker, cfg.raw, state, NOW)
    assert d.action == "enter" and d.limit_price == 0.72  # min(ask, 5.50)


async def test_single_contract_fallback_is_tp_only(state, cfg):
    b = FakeBroker(buying_power=100)  # enough for 1 contract at 0.72, not 2
    b.now = NOW
    c = OptionContract("SPY", 746.0, "CALL", TODAY)
    b.add_chain(c)
    b.set_quote(c, bid=0.68, ask=0.72)
    d = await gate(alert(), b, cfg.raw, state, NOW)
    assert d.action == "enter" and d.qty == 1 and d.tp_only


async def test_no_buying_power_at_all(state, cfg):
    b = FakeBroker(buying_power=10)
    b.now = NOW
    c = OptionContract("SPY", 746.0, "CALL", TODAY)
    b.add_chain(c)
    b.set_quote(c, bid=0.68, ask=0.72)
    d = await gate(alert(), b, cfg.raw, state, NOW)
    assert d.action == "skip" and "buying power" in d.reason


async def test_max_entries_per_day(broker, state, cfg):
    state.entries_today = 3
    d = await gate(alert(), broker, cfg.raw, state, NOW)
    assert d.action == "skip" and "max_entries_per_day" in d.reason


async def test_same_day_realert_skipped(broker, state, cfg):
    state.played_keys.add(OptionContract("SPY", 746.0, "CALL", TODAY).key)
    d = await gate(alert(), broker, cfg.raw, state, NOW)
    assert d.action == "skip" and "re-alert" in d.reason


async def test_too_close_to_flatten(broker, state, cfg):
    late = NOW.replace(hour=11, minute=30)  # 30 min before 12:00 flatten
    broker.set_quote(OptionContract("SPY", 746.0, "CALL", TODAY),
                     bid=0.68, ask=0.72, ts=late)
    d = await gate(alert(ts=late), broker, cfg.raw, state, late)
    assert d.action == "skip" and "flatten" in d.reason


async def test_halted_after_kill(broker, state, cfg):
    state.halted = True
    d = await gate(alert(), broker, cfg.raw, state, NOW)
    assert d.action == "skip" and "halted" in d.reason


class TestResolveExpiry:
    def test_dte0_and_empty_assume_today(self):
        assert resolve_expiry(alert(expiry="DTE0"), TODAY, 1, True) == TODAY
        assert resolve_expiry(alert(expiry=""), TODAY, 1, True) == TODAY

    def test_empty_without_assume_is_none(self):
        assert resolve_expiry(alert(expiry=""), TODAY, 1, False) is None

    def test_dte1_skips_weekend(self):
        friday = date(2026, 8, 14)
        assert resolve_expiry(alert(expiry="DTE1"), friday, 1, True) == date(2026, 8, 17)

    def test_dated(self):
        assert resolve_expiry(alert(expiry="DATE8/21"), TODAY, 5, True) == date(2026, 8, 21)


async def test_dated_beyond_max_dte(broker, state, cfg):
    d = await gate(alert(expiry="DATE8/21"), broker, cfg.raw, state, NOW)  # 4 DTE > 1
    assert d.action == "skip" and "max_dte" in d.reason
