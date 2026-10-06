"""Control-channel bot: a bot account in Logan's OWN private server
(ToS-compliant — never a user token). Posts every event; obeys kill/status/
pause/resume from the authorized user only."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import discord

Handler = Callable[[str], Awaitable[str]]


class ControlChannel:
    """Interface the rest of the app talks to; tests swap in a fake."""

    async def post(self, msg: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def route_command(
    text: str, author_id: int, authorized_user_id: int
) -> str | None:
    """Pure routing so authorization is unit-testable without Discord.
    Returns the normalized command (args preserved for e.g. `close spy 746c`),
    "unauthorized", or None for non-command chatter."""
    cmd = " ".join(text.strip().lower().split())
    bare = ("kill", "status", "pause", "resume", "close", "restart", "help",
            "alerters", "follow", "unfollow")
    if cmd not in bare and not cmd.startswith(
        ("close ", "restart ", "follow ", "unfollow ")
    ):
        return None
    if author_id != authorized_user_id:
        return "unauthorized"
    return cmd


class DiscordControlBot(ControlChannel):
    def __init__(self, token: str, guild_id: int, channel_id: int,
                 authorized_user_id: int, handlers: dict[str, Handler]) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        self.token = token
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.authorized_user_id = authorized_user_id
        self.handlers = handlers
        self.ready = asyncio.Event()
        self._channel: discord.abc.Messageable | None = None

        @self.client.event
        async def on_ready() -> None:
            self._channel = self.client.get_channel(self.channel_id)
            if self._channel is None:
                self._channel = await self.client.fetch_channel(self.channel_id)
            self.ready.set()

        @self.client.event
        async def on_message(message: discord.Message) -> None:
            if message.author == self.client.user:
                return
            if message.channel.id != self.channel_id:
                return
            cmd = route_command(
                message.content, message.author.id, self.authorized_user_id
            )
            if cmd is None:
                return
            if cmd == "unauthorized":
                await message.channel.send(self.ECHO_PREFIX + "not authorized")
                return
            name, _, args = cmd.partition(" ")
            handler = self.handlers.get(name)
            if handler is not None:
                # Command replies get the echo prefix too — they toast on the
                # desktop like any bot post and must never reach the parser.
                await message.channel.send(
                    (self.ECHO_PREFIX + await handler(args))[:1900]
                )

    async def start(self) -> None:
        await self.client.start(self.token)

    async def close(self) -> None:
        await self.client.close()

    # Every control post carries this prefix so the toast listener can tell
    # the bot's own posts apart from alerter messages (the control channel
    # may share a name with an alert channel — never parse our own output).
    ECHO_PREFIX = "» "

    async def post(self, msg: str) -> None:
        await self.ready.wait()
        assert self._channel is not None
        await self._channel.send((self.ECHO_PREFIX + msg)[:1900])


class Watchdog:
    """Alert-post if the heartbeat stalls > 3 × heartbeat_seconds."""

    def __init__(self, control: ControlChannel, heartbeat_seconds: float) -> None:
        self.control = control
        self.heartbeat_seconds = heartbeat_seconds
        self._last = time.monotonic()

    def beat(self) -> None:
        self._last = time.monotonic()

    async def run(self) -> None:
        self.beat()
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            stall = time.monotonic() - self._last
            if stall > 3 * self.heartbeat_seconds:
                await self.control.post(
                    f"⚠️ WATCHDOG: heartbeat stalled {stall:.0f}s "
                    f"(> {3 * self.heartbeat_seconds:.0f}s) — check the bot"
                )
