"""Mine an exported chat history into entries — the alerter-onboarding flow.

Used both for the bravo golden fixtures and for any future alerter Logan
brings: drop the export in tests/fixtures/<name>/, run this, review results.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from ingestion.source import RawMessage
from parsing.claims import extract_positive_pcts, mentions_contract
from parsing.models import MinedEntry, MinedHistory, ParseKind
from parsing.parser import parse_message
from settings import AlerterProfile, ParserConfig

CSV_HEADER = [
    "datetime", "ticker", "strike", "side", "expiry",
    "alert_price", "tags", "claimed_max_pct", "raw",
]


def _author_ok(author: str, hints: tuple[str, ...]) -> bool:
    return not hints or any(h.lower() in author.lower() for h in hints)


def _resolved_expiry(expiry: str, on: date) -> date:
    """Expiry as a real date for the repeat key — the live duplicate check
    keys on the full contract (OptionContract.key includes expiry), so two
    same-day alerts on different expirations are different plays."""
    if expiry.startswith("DATE"):
        mo, day = expiry[4:].split("/")
        d = date(on.year, int(mo), int(day))
        return d if d >= on else date(on.year + 1, int(mo), int(day))
    if expiry == "DTE1":
        return date.fromordinal(on.toordinal() + 1)
    return on  # "" and DTE0 both mean today


def mine_history(
    messages: Iterable[RawMessage],
    profile: AlerterProfile,
    parser_cfg: ParserConfig,
    author_hints: tuple[str, ...] | None = None,
) -> MinedHistory:
    hints = profile.author_hints if author_hints is None else author_hints
    msgs = [m for m in messages if _author_ok(m.author, hints)]

    mined = MinedHistory()
    seen_keys: set[tuple] = set()
    for msg in msgs:
        result = parse_message(msg.text, profile, parser_cfg)
        if result.kind is ParseKind.AMBIGUOUS:
            mined.ambiguous.append(result)
            continue
        if result.kind is ParseKind.NOISE:
            mined.noise_count += 1
            continue
        alert = result.alert
        alert.ts = msg.ts
        alert.author = msg.author
        key = (
            msg.ts.date(),
            alert.ticker,
            alert.strike,
            alert.side,
            _resolved_expiry(alert.expiry, msg.ts.date()),
        )
        entry = MinedEntry(alert=alert)
        if key in seen_keys:
            entry.is_same_day_repeat = True
            mined.repeats.append(entry)
        else:
            seen_keys.add(key)
            mined.entries.append(entry)

    # Claim mining: the alerter's own peak-marked numbers, per play, same day.
    for entry in mined.entries:
        a = entry.alert
        best: float | None = None
        for msg in msgs:
            if msg.ts.date() != a.ts.date() or msg.ts < a.ts:
                continue
            if not mentions_contract(msg.text, a.ticker, a.strike, profile):
                continue
            pcts = extract_positive_pcts(msg.text)
            if pcts:
                best = max(best or 0.0, max(pcts))
        entry.claimed_max_pct = best
    return mined


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return ""
    return str(int(v)) if v == int(v) else str(v)


def to_csv_rows(mined: MinedHistory) -> list[list[str]]:
    rows = [list(CSV_HEADER)]
    for e in mined.entries:
        a = e.alert
        rows.append([
            a.ts.strftime("%Y-%m-%d %H:%M:%S"),
            a.ticker,
            str(float(a.strike)),
            a.side,
            a.expiry,
            "" if a.alert_price is None else str(float(a.alert_price)),
            ";".join(a.tags),
            _fmt_pct(e.claimed_max_pct),
            a.raw,
        ])
    return rows
