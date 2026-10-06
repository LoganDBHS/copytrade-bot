"""_call retry semantics: transient failures on idempotent reads recover;
writes (retries=0, the default) still fail on the first error."""

import pytest

from execution.broker import BrokerError
from execution.webull import WebullAdapter


class _Resp:
    status_code = 200

    @staticmethod
    def json():
        return {"ok": True}


def _flaky(fail_times: int):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise OSError("HTTP Status: 401, Code: SDK.UnknownServerError")
        return _Resp()

    fn.__name__ = "get_account_balance"
    return fn, calls


@pytest.fixture()
def adapter():
    wb = WebullAdapter("k", "s")
    wb._trade = object()  # skip _ensure's real connect
    return wb


async def test_read_retries_through_transient_errors(adapter):
    fn, calls = _flaky(2)
    assert await adapter._call(fn, retries=2) == {"ok": True}
    assert calls["n"] == 3


async def test_read_gives_up_after_retry_budget(adapter):
    fn, calls = _flaky(3)
    with pytest.raises(BrokerError):
        await adapter._call(fn, retries=2)
    assert calls["n"] == 3


async def test_write_default_never_retries(adapter):
    fn, calls = _flaky(1)
    with pytest.raises(BrokerError):
        await adapter._call(fn)  # default retries=0 — the place/cancel path
    assert calls["n"] == 1
