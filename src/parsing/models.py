"""Result types for the token-role parser."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class ParseKind(enum.Enum):
    ENTRY = "entry"
    NOISE = "noise"  # status update, commentary, recap — log, never trade
    AMBIGUOUS = "ambiguous"  # spine present but conflicting — fail closed


@dataclass
class ParsedAlert:
    alerter_id: str
    ticker: str
    strike: float
    side: str  # "CALL" | "PUT"
    expiry: str  # "DTE0" | "DTE1" | "DATE<m>/<d>" | "" (none stated)
    alert_price: float | None
    tags: list[str]
    raw: str
    ts: datetime | None = None
    author: str = ""


@dataclass
class ParseResult:
    kind: ParseKind
    alert: ParsedAlert | None = None
    reason: str = ""
    raw: str = ""

    @property
    def is_entry(self) -> bool:
        return self.kind is ParseKind.ENTRY


@dataclass
class MinedEntry:
    """An entry recovered from an exported history, with post-hoc claim data."""

    alert: ParsedAlert
    claimed_max_pct: float | None = None
    is_same_day_repeat: bool = False


@dataclass
class MinedHistory:
    entries: list[MinedEntry] = field(default_factory=list)
    repeats: list[MinedEntry] = field(default_factory=list)
    ambiguous: list[ParseResult] = field(default_factory=list)
    noise_count: int = 0
