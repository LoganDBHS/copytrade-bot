"""One-shot Webull OpenAPI probe — read-only + order PREVIEW, never places.

Run:  .venv\\Scripts\\python.exe tools\\webull_probe.py
Confirms: auth, account id, buying power, today's SPY chain, an ATM quote,
and an order preview. Prints raw payloads so field names can be tuned.
"""

import asyncio
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402


async def main() -> None:
    load_dotenv()
    from execution.broker import OptionContract
    from execution.webull import WebullAdapter

    key, secret = os.environ.get("WEBULL_APP_KEY"), os.environ.get("WEBULL_APP_SECRET")
    if not key or not secret:
        sys.exit("WEBULL_APP_KEY/SECRET missing from .env")

    wb = WebullAdapter(key, secret)
    print("1) account id:", await wb.account_id())
    print("2) buying power:", await wb.buying_power())

    today = date.today()
    chain = await wb.get_chain("SPY", today)
    print(f"3) SPY chain for {today}: {len(chain)} contracts")
    if not chain:
        print("   (empty — market holiday, weekend, or entitlement issue)")
        return

    calls = sorted(s for s, side in chain if side == "CALL")
    atm = calls[len(calls) // 2]
    c = OptionContract("SPY", atm, "CALL", today)
    q = await wb.get_option_quote(c)
    print(f"4) quote {c}: bid={q.bid} ask={q.ask} mid={q.mid:.2f}")

    acct = await wb.account_id()
    preview = {
        "client_order_id": f"probe{datetime.now():%H%M%S}",
        "combo_type": "NORMAL",
        "order_type": "LIMIT",
        "quantity": "1",
        "limit_price": f"{max(q.bid, 0.01):.2f}",
        "option_strategy": "SINGLE",
        "side": "BUY",
        "time_in_force": "DAY",
        "entrust_type": "QTY",
        "legs": [wb._leg(c, "BUY", 1)],
    }
    data = await wb._call(wb._trade.order_v2.preview_option, acct, [preview])
    print("5) preview_option:", data)
    print("\nprobe complete — no orders were placed.")


if __name__ == "__main__":
    asyncio.run(main())
