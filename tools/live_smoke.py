"""LIVE order-path smoke test — REAL Webull account, near-zero risk by design.

What it does (one pass):
1. Buys nothing at market: places a REAL limit order to BUY 1 deep-ITM SPY
   call at $0.01 — a price it cannot trade at (the contract is worth dollars,
   floored by intrinsic value). Max theoretical risk: $1 plus ~$0.06 fees.
2. Posts to the Discord control channel and waits for Logan to type `cancel`.
3. Cancels the real order at Webull, confirms CANCELLED, posts the result.
4. Failsafe: auto-cancels after 5 minutes; a DAY order dies at the close anyway.

Run:  .venv\\Scripts\\python.exe tools\\live_smoke.py --yes
Refuses to run without --yes, outside 06:35-12:50 PT, or with BP < $2.
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

import discord  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

CANCEL_WAIT_S = 300


async def main() -> None:
    if "--yes" not in sys.argv:
        sys.exit(__doc__)
    load_dotenv()
    from execution.broker import OptionContract
    from execution.webull import WebullAdapter
    from settings import load_config

    cfg = load_config("config.yaml")
    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz)
    if not (time(6, 35) <= now.time() <= time(12, 50)):
        sys.exit(f"outside the calm window (06:35-12:50 PT); it is {now:%H:%M}")

    wb = WebullAdapter(
        os.environ["WEBULL_APP_KEY"], os.environ["WEBULL_APP_SECRET"],
        account_type=cfg.raw.get("broker_account_type", "CASH"),
    )
    bp = await wb.buying_power()
    print(f"account {await wb.account_id()}  option buying power ${bp:.2f}")
    if bp < 2:
        sys.exit("buying power under $2 — deposit not usable yet")

    today = now.date()
    chain = await wb.get_chain("SPY", today)
    calls = sorted(s for s, side in chain if side == "CALL")
    if not calls:
        sys.exit("no SPY chain today (holiday?)")
    strike = calls[0]  # deepest ITM call available
    contract = OptionContract("SPY", strike, "CALL", today)
    q = await wb.get_option_quote(contract)
    print(f"target {contract}: bid={q.bid} ask={q.ask}")
    if q.bid < 1.00:
        sys.exit(f"bid {q.bid} too low — want a contract a $0.01 limit can never touch")

    st = await wb.buy_to_open(contract, 1, 0.01, f"smoke{uuid.uuid4().hex[:8]}")
    print(f"REAL ORDER PLACED: {st.broker_order_id} status={st.status.value}")
    try:
        await run_with_order(wb, st, contract, q, cfg)
    except BaseException:
        # A real order must never outlive a crashed script.
        print("exception after placement — cancelling the real order")
        try:
            await wb.cancel(st.broker_order_id)
            final = await wb.order_status(st.broker_order_id)
            print(f"crash-guard cancel: webull says {final.status.value}")
        except Exception as ce:
            print(f"crash-guard cancel ALSO failed ({ce}) — CHECK THE WEBULL APP")
        raise


async def run_with_order(wb, st, contract, q, cfg) -> None:
    st = await wb.order_status(st.broker_order_id)
    print(f"webull reports: {st.status.value}")

    dc = cfg.raw["discord_control"]
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    done = asyncio.Event()

    async def cancel_and_report(channel, why: str) -> None:
        await wb.cancel(st.broker_order_id)
        final = await wb.order_status(st.broker_order_id)
        await channel.send(
            f"» LIVE SMOKE: cancel sent ({why}) — webull says {final.status.value.upper()} "
            + ("✅ order path verified end to end" if final.status.value == "cancelled"
               else "⚠️ unexpected status, check the app")
        )
        done.set()

    @client.event
    async def on_ready() -> None:
        channel = client.get_channel(dc["channel_id"]) or await client.fetch_channel(
            dc["channel_id"]
        )
        await channel.send(
            f"» LIVE SMOKE: REAL order resting at Webull — BUY 1 {contract} @ $0.01 "
            f"(cannot fill; contract trades at {q.bid}+). Type `cancel` to cancel it. "
            f"Auto-cancels in {CANCEL_WAIT_S // 60} min."
        )

        async def failsafe() -> None:
            try:
                await asyncio.wait_for(done.wait(), timeout=CANCEL_WAIT_S)
            except TimeoutError:
                await cancel_and_report(channel, "failsafe timeout")

        client.loop.create_task(failsafe())

    @client.event
    async def on_message(message: discord.Message) -> None:
        if (
            message.channel.id == dc["channel_id"]
            and message.author.id == dc["authorized_user_id"]
            and message.content.strip().lower() == "cancel"
        ):
            await cancel_and_report(message.channel, "cancelled by Logan via Discord")

    async def runner() -> None:
        async with client:
            task = asyncio.create_task(client.start(os.environ["DISCORD_BOT_TOKEN"]))
            await done.wait()
            await client.close()
            task.cancel()

    await runner()
    print("smoke test complete")


if __name__ == "__main__":
    asyncio.run(main())
