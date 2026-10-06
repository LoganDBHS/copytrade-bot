"""Launch the bot: python run.py  (paper mode unless config+ack say otherwise)."""

import asyncio
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from dotenv import load_dotenv  # noqa: E402


async def main() -> None:
    # Singleton guard: a second instance would double-trade every alert.
    # Holding a bound localhost port is a lock the OS releases on ANY exit.
    # Retry briefly: after a `restart` command the predecessor frees the port
    # within a couple of seconds, so the successor just waits it out.
    lock = socket.socket()
    for attempt in range(15):
        try:
            lock.bind(("127.0.0.1", 47653))
            break
        except OSError:
            if attempt == 14:
                sys.exit("another CopyTrade Bot instance is already running — exiting.")
            time.sleep(1)

    load_dotenv()
    from app import App
    from audit.db import AuditDB
    from control.bot import DiscordControlBot
    from execution.paper import PaperBroker
    from execution.webull import WebullAdapter
    from ingestion.toast import WindowsToastSource
    from settings import load_config

    cfg = load_config("config.yaml")

    if cfg.mode == "live" and os.environ.get("LIVE_TRADING_ACK") != "yes":
        sys.exit("mode: live requires LIVE_TRADING_ACK=yes in .env — refusing.")

    app_key = os.environ.get("WEBULL_APP_KEY", "")
    app_secret = os.environ.get("WEBULL_APP_SECRET", "")
    if not app_key or not app_secret:
        sys.exit("WEBULL_APP_KEY/WEBULL_APP_SECRET missing from .env")
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        sys.exit("DISCORD_BOT_TOKEN missing from .env")

    webull = WebullAdapter(
        app_key, app_secret,
        account_type=cfg.raw.get("broker_account_type", "CASH"),
    )
    if cfg.mode == "live":
        broker = webull  # real orders on the real account (double-acked above)
        print("*** LIVE MODE — real orders will be placed. `kill` in the "
              "control channel flattens everything. ***")
    else:
        broker = PaperBroker(webull)
    audit = AuditDB(cfg.raw["audit"]["db_path"], mode=cfg.mode)
    source = WindowsToastSource(cfg.enabled_alerters, cfg.timezone)

    dc = cfg.raw["discord_control"]
    handlers: dict = {}
    control = DiscordControlBot(
        token, dc["guild_id"], dc["channel_id"], dc["authorized_user_id"], handlers
    )
    app = App(cfg, broker, source, control, audit)
    handlers.update(
        kill=app.cmd_kill, status=app.cmd_status,
        pause=app.cmd_pause, resume=app.cmd_resume, close=app.cmd_close,
        restart=app.cmd_restart, help=app.cmd_help,
        alerters=app.cmd_alerters, follow=app.cmd_follow, unfollow=app.cmd_unfollow,
    )

    bot_task = asyncio.create_task(control.start())
    try:
        await asyncio.wait_for(control.ready.wait(), timeout=60)
        await app.run()
    finally:
        for t in app.tasks:
            t.cancel()
        await control.close()
        bot_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("stopped")
