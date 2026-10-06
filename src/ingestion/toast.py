"""WindowsToastSource: reads Discord's Windows toast notifications via the
UserNotificationListener — never Discord's API with a user token.

Routing: a notification belongs to an alerter profile when the profile's
channel_hint is a substring of the toast title (Discord titles look like
"Bravo Alerts (#sun-alerts, Server Name)"). Dedupe is by notification id.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

from ingestion.source import NotificationSource, RawMessage
from settings import AlerterProfile


class ToastAccessDenied(RuntimeError):
    pass


class WindowsToastSource(NotificationSource):
    def __init__(
        self,
        profiles: list[AlerterProfile],
        timezone: str = "America/Los_Angeles",
        poll_seconds: float = 0.5,
    ) -> None:
        self.profiles = [p for p in profiles if p.enabled and p.channel_hint]
        self.tz = ZoneInfo(timezone)
        self.poll_seconds = poll_seconds

    def _route(self, title: str) -> AlerterProfile | None:
        t = title.lower()
        for p in self.profiles:
            if p.channel_hint.lower() in t:
                return p
        return None

    async def stream(self) -> AsyncIterator[RawMessage]:
        from winsdk.windows.ui.notifications import (
            KnownNotificationBindings,
            NotificationKinds,
        )
        from winsdk.windows.ui.notifications.management import (
            UserNotificationListener,
            UserNotificationListenerAccessStatus,
        )

        listener = UserNotificationListener.current
        status = await listener.request_access_async()
        if status != UserNotificationListenerAccessStatus.ALLOWED:
            raise ToastAccessDenied(
                "Windows denied notification access. Settings → Privacy & security "
                "→ Notifications → allow notification access for this app/terminal."
            )

        seen: set[int] = set()
        first_poll = True
        while True:
            notifs = await listener.get_notifications_async(NotificationKinds.TOAST)
            current_ids = set()
            for n in notifs:
                current_ids.add(n.id)
                if n.id in seen:
                    continue
                seen.add(n.id)
                if first_poll:
                    continue  # never trade toasts that predate startup
                try:
                    app_name = n.app_info.display_info.display_name if n.app_info else ""
                except OSError:
                    app_name = ""
                if "discord" not in app_name.lower():
                    continue
                binding = n.notification.visual.get_binding(
                    KnownNotificationBindings.toast_generic
                )
                if binding is None:
                    continue
                texts = [t.text for t in binding.get_text_elements()]
                title = texts[0] if texts else ""
                body = "\n".join(texts[1:]).strip()
                profile = self._route(title)
                if profile is None or not body:
                    continue
                yield RawMessage(
                    source_id=f"toast:{n.id}",
                    alerter_id=profile.alerter_id,
                    ts=datetime.now(self.tz),
                    author=title,
                    text=body,
                )
            if len(seen) > 4096:  # keep the dedupe set bounded
                seen = current_ids
            first_poll = False
            await asyncio.sleep(self.poll_seconds)
