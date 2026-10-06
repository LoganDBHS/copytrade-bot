"""Position manager state machine: OPEN → TRIM → RUNNER, tp_only, flatten."""

from datetime import date, datetime

import pytest

from execution.broker import OptionContract
from execution.fake import FakeBroker
from manage.position import Play, PositionManager

NOW = datetime(2026, 8, 17, 7, 0, 0)
C = OptionContract("SPY", 746.0, "CALL", date(2026, 8, 17))


class FakeControl:
    def __init__(self):
        self.posts = []

    async def post(self, msg):
        self.posts.append(msg)


@pytest.fixture()
def setup(cfg):
    broker = FakeBroker()
    broker.now = NOW
    control = FakeControl()
    play = Play("bravo", C, qty=2, fill_price=1.00)
    mgr = PositionManager(broker, cfg.raw, play, control=control)
    return broker, control, play, mgr


def q(broker, bid, ask):
    broker.set_quote(C, bid=bid, ask=ask, ts=NOW)


async def test_initial_stop(setup):
    _, _, play, _ = setup
    assert play.stop == 0.75  # 1.00 × (1 − 0.25)


async def test_soft_stop_exits_everything(setup):
    broker, control, play, mgr = setup
    q(broker, 0.74, 0.76)  # mid 0.75 <= stop
    assert await mgr.step(NOW) is True
    assert play.qty == 0 and play.state == "CLOSED"
    assert play.realized < 0
    assert any("stop" in p for p in control.posts)


async def test_trim_to_runner_and_trail(setup):
    broker, control, play, mgr = setup
    q(broker, 1.24, 1.26)  # mid 1.25 = +25% → trim
    assert await mgr.step(NOW) is False
    assert play.qty == 1 and play.state == "RUNNER"
    assert play.stop == 1.00  # breakeven
    # run up: peak 2.00 → trail stop 1.70, ratchet only
    q(broker, 1.99, 2.01)
    assert await mgr.step(NOW) is False
    assert play.stop == pytest.approx(1.70)
    q(broker, 1.79, 1.81)  # pullback but above stop — stop must not move down
    assert await mgr.step(NOW) is False
    assert play.stop == pytest.approx(1.70)
    q(broker, 1.64, 1.66)  # mid 1.65 <= 1.70 → runner exits
    assert await mgr.step(NOW) is True
    assert play.state == "CLOSED"
    assert play.realized > 0
    assert any("runner" in p for p in control.posts)


async def test_tp_only_sells_at_target(cfg):
    broker = FakeBroker()
    broker.now = NOW
    play = Play("bravo", C, qty=1, fill_price=1.00, tp_only=True)
    mgr = PositionManager(broker, cfg.raw, play, control=FakeControl())
    q(broker, 1.10, 1.12)  # below +25% target
    assert await mgr.step(NOW) is False
    q(broker, 1.25, 1.27)  # mid 1.26 >= 1.25 target
    assert await mgr.step(NOW) is True
    assert play.state == "CLOSED" and play.realized > 0


async def test_hard_flatten_overrides_everything(setup):
    broker, control, play, mgr = setup
    q(broker, 1.10, 1.12)  # healthy position, no trigger
    noon = NOW.replace(hour=12, minute=0)
    assert await mgr.step(noon) is True
    assert play.qty == 0 and play.state == "CLOSED"
    assert any("hard flatten" in p for p in control.posts)


async def test_disaster_stop_armed_and_cancelled_on_exit(setup):
    broker, control, play, mgr = setup
    await mgr.arm_disaster_stop()
    assert play.disaster_order_id is not None
    assert broker.disaster_stops[0]["stop"] == 0.50  # 1.00 × (1 − 0.50)
    q(broker, 0.74, 0.76)
    await mgr.step(NOW)
    st = await broker.order_status(broker.disaster_stops[0]["id"])
    assert st.status.value == "cancelled"


async def test_unsupported_disaster_stop_is_announced(cfg):
    broker = FakeBroker()
    broker.now = NOW
    broker.supports_disaster_stop = False

    async def no_stop(*a, **k):
        return None

    broker.place_disaster_stop = no_stop
    control = FakeControl()
    play = Play("bravo", C, qty=2, fill_price=1.00)
    mgr = PositionManager(broker, cfg.raw, play, control=control)
    await mgr.arm_disaster_stop()
    assert any("disaster stop" in p for p in control.posts)
