"""Load config.yaml into typed objects. Secrets come from .env, never from here."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("config.yaml")


@dataclass(frozen=True)
class ParserConfig:
    ticker_whitelist: tuple[str, ...]
    fuzzy_max_edit_distance: int = 2
    # Whitelist members that double as English words: matched only when
    # written in ALL CAPS, and excluded from fuzzy matching entirely.
    uppercase_only_tickers: tuple[str, ...] = ()


@dataclass(frozen=True)
class AlerterProfile:
    alerter_id: str
    enabled: bool
    channel_hint: str
    alert_prefix: str
    fixtures: str
    ticker_aliases: dict[str, str] = field(default_factory=dict)
    known_tags: tuple[str, ...] = ()
    trade_all_tags: bool = True
    # Substrings identifying the alerter's own messages (author names in
    # exports / notification titles) — more than one when the alerter renamed.
    # Used to scope history/claim mining to the alerter and ignore lookalike
    # accounts (his personal account's numbers don't count for the record).
    author_hints: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    mode: str
    timezone: str
    parser: ParserConfig
    alerters: dict[str, AlerterProfile]
    raw: dict

    @property
    def enabled_alerters(self) -> list[AlerterProfile]:
        return [a for a in self.alerters.values() if a.enabled]


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    parser = ParserConfig(
        ticker_whitelist=tuple(t.upper() for t in data["parser"]["ticker_whitelist"]),
        fuzzy_max_edit_distance=int(data["parser"].get("fuzzy_max_edit_distance", 2)),
        uppercase_only_tickers=tuple(
            t.upper() for t in data["parser"].get("uppercase_only_tickers") or ()
        ),
    )
    alerters: dict[str, AlerterProfile] = {}
    for name, a in (data.get("alerters") or {}).items():
        alerters[name] = AlerterProfile(
            alerter_id=name,
            enabled=bool(a.get("enabled", False)),
            channel_hint=a.get("channel_hint", ""),
            alert_prefix=a.get("alert_prefix", ""),
            fixtures=a.get("fixtures", ""),
            ticker_aliases={
                k.lower(): v.upper() for k, v in (a.get("ticker_aliases") or {}).items()
            },
            known_tags=tuple(a.get("known_tags") or ()),
            trade_all_tags=bool(a.get("trade_all_tags", True)),
            author_hints=tuple(a.get("author_hints") or ()),
        )
    return Config(
        mode=data.get("mode", "paper"),
        timezone=data.get("timezone", "America/Los_Angeles"),
        parser=parser,
        alerters=alerters,
        raw=data,
    )
