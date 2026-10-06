"""Adjusted option series must never reach the symbol cache. On 2026-08-20 a
live alpha AAPL 315C alert was skipped with INVALID_SYMBOL because Webull's
chain listed an adjusted contract (2AAPL260821C00315000) at the same
strike/side/expiry as the standard one and it clobbered the cache entry."""

from datetime import date
from types import SimpleNamespace

import pytest

from execution.broker import BrokerError, OptionContract
from execution.webull import WebullAdapter, _standard_symbol

EXP = date(2026, 8, 21)


@pytest.mark.parametrize(
    ("sym", "ticker", "ok"),
    [
        ("AAPL260821C00315000", "AAPL", True),
        ("2AAPL260821C00315000", "AAPL", False),   # adjusted (digit-prefixed root)
        ("AAPL1260821C00315000", "AAPL", False),   # adjusted (digit-suffixed root)
        ("SPXW260821C06500000", "SPX", True),      # index weekly root
        ("NDXP260821P23000000", "NDX", True),
        ("AAPL260822C00315000", "AAPL", False),    # embedded date != requested expiry
        ("MSFT260821C00315000", "AAPL", False),    # foreign root
    ],
)
def test_standard_symbol(sym, ticker, ok):
    assert _standard_symbol(sym, ticker, EXP) is ok


@pytest.fixture()
def adapter():
    wb = WebullAdapter("k", "s")
    wb._trade = object()  # skip _ensure's real connect
    wb._data = SimpleNamespace(
        instrument=SimpleNamespace(get_option_contracts=None),
        option_market_data=SimpleNamespace(get_option_snapshot=None),
    )
    return wb


async def test_adjusted_row_never_clobbers_standard_symbol(adapter):
    rows = [
        {"strike_price": "315", "option_type": "CALL", "symbol": "AAPL260821C00315000"},
        {"strike_price": "315", "option_type": "CALL", "symbol": "2AAPL260821C00315000"},
        {"strike_price": "320", "option_type": "CALL", "symbol": "2AAPL260821C00320000"},
    ]

    async def fake_call(fn, **kwargs):
        return rows

    adapter._call = fake_call
    chain = await adapter.get_chain("AAPL", EXP)

    key = OptionContract("AAPL", 315.0, "CALL", EXP).key
    assert adapter._symbol_cache[key] == "AAPL260821C00315000"
    assert (315.0, "CALL") in chain
    # adjusted-only strike must not pass the gate's chain-existence check
    assert (320.0, "CALL") not in chain
    assert OptionContract("AAPL", 320.0, "CALL", EXP).key not in adapter._symbol_cache


async def test_invalid_symbol_evicts_cached_symbol(adapter):
    contract = OptionContract("AAPL", 315.0, "CALL", EXP)
    adapter._symbol_cache[contract.key] = "2AAPL260821C00315000"

    async def fake_call(fn, *args, **kwargs):
        raise BrokerError(
            "webull get_option_snapshot: HTTP Status: 417, Code: INVALID_SYMBOL, "
            "Msg: Invalid Symbol:[2AAPL260821C00315000]."
        )

    adapter._call = fake_call
    with pytest.raises(BrokerError):
        await adapter.get_option_quote(contract)
    assert contract.key not in adapter._symbol_cache
