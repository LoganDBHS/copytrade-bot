"""NotificationSource interface — everything upstream of the parser."""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RawMessage:
    source_id: str  # dedupe key (notification id, or file:line for replays)
    alerter_id: str
    ts: datetime
    author: str
    text: str


class NotificationSource(abc.ABC):
    """A stream of raw messages routed to an alerter profile."""

    @abc.abstractmethod
    def stream(self) -> AsyncIterator[RawMessage]:
        """Yield messages as they arrive. Implementations must dedupe."""
        raise NotImplementedError
