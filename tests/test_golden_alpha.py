"""Alpha onboarding gate: the parser must reproduce the golden record.

The fixtures are synthetic — generated from tests/fixtures/_corpus.py by
tests/fixtures/_generate.py, which renders the corpus into a
DiscordChatExporter HTML export. The corpus carries one hand-authored
message per grammar feature the parser is required to handle, so this file
is the contract for the formal, separator-heavy alert grammar.

    export.html -> entries.csv: 58 unique entries, 1 same-day repeat
    flagged, 2 ambiguous (both deliberate: a two-strike and a two-ticker
    message, each of which must fail closed).

Regenerate with:  python tests/fixtures/_generate.py
"""

import csv

import pytest

from conftest import FIXTURES
from ingestion.replay import ReplaySource
from parsing.history import CSV_HEADER, mine_history, to_csv_rows

ALPHA = FIXTURES / "alpha"


@pytest.fixture(scope="module")
def alpha(cfg):
    return cfg.alerters["alpha"]


@pytest.fixture(scope="module")
def mined_full(cfg, alpha):
    msgs = ReplaySource(ALPHA / "export.html", "alpha").messages()
    return mine_history(msgs, alpha, cfg.parser)


def golden_rows():
    with (ALPHA / "entries.csv").open(encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.reader(fh) if r]
    assert rows[0] == CSV_HEADER
    return rows[1:]


def test_counts(mined_full):
    assert len(mined_full.entries) == 58
    assert len(mined_full.repeats) == 1
    # Both ambiguous messages are deliberate fail-closed cases. A parser
    # change that grows this list is losing entries.
    assert len(mined_full.ambiguous) == 2


def test_reproduces_golden_csv_exactly(mined_full):
    mined = to_csv_rows(mined_full)[1:]
    golden = golden_rows()
    assert len(mined) == len(golden) == 58
    for i, (m, g) in enumerate(zip(mined, golden, strict=True), start=1):
        assert m == g, f"row {i} differs: mined={m} golden={g}"


def test_representative_formats(mined_full):
    by_raw = {e.alert.raw: e.alert for e in mined_full.entries}

    # Formal grammar with informal @price.
    a = by_raw["$SPY 612C 03/01 @0.60"]
    assert (a.ticker, a.strike, a.side, a.alert_price) == ("SPY", 612.0, "CALL", 0.6)

    # 〡-separator grammar with a profit-target % and trailing commentary:
    # the % guard is soft, so a fully specified message is still an entry.
    a = next(
        v for k, v in by_raw.items()
        if k.startswith("SPY 〡 614 Call〡 03/01 〡 0.50 Fill")
    )
    assert (a.ticker, a.strike, a.side, a.expiry, a.alert_price) == (
        "SPY", 614.0, "CALL", "DATE3/1", 0.5,
    )

    # Stop clause truncated off the spine; leading-dot price kept.
    a = by_raw["QQQ 526C 03/02 .78 FILL 0.6 STOP @alert"]
    assert (a.strike, a.alert_price) == (526.0, 0.78)

    # Uppercase-only ticker (ServiceNow) matched from ALL-CAPS uses only.
    now = [e.alert for e in mined_full.entries if e.alert.ticker == "NOW"]
    assert {a.strike for a in now} == {109.0, 110.0}
    assert all("NOW" in a.raw for a in now)

    # SPX lotto; each fill range collapsed to its lower bound.
    spx = [e.alert for e in mined_full.entries if e.alert.ticker == "SPX"]
    assert {(a.strike, a.alert_price) for a in spx} == {
        (6150.0, 1.0), (6075.0, 4.0),
    }

    # A parenthetical aside's number must not become a second price.
    a = next(v for k, v in by_raw.items() if k.startswith("NVDA 0.80 FILL"))
    assert (a.ticker, a.strike, a.alert_price) == ("NVDA", 142.0, 0.8)

    # A slash number next to "risk" is a scale, not an expiry.
    a = next(v for k, v in by_raw.items() if "8/10 risk" in k)
    assert (a.ticker, a.expiry) == ("AVGO", "DATE3/10")


def test_custom_emoji_alt_text_survives_without_breaking_the_spine(mined_full):
    """The HTML export carries custom emoji as <img alt=":name:">. The alt
    text is kept in the raw message; it must not disturb the parse."""
    with_emoji = [e.alert for e in mined_full.entries if ":rocket:" in e.alert.raw]
    assert with_emoji, "fixture should contain a message with a custom emoji"
    for a in with_emoji:
        assert a.ticker and a.strike and a.side


def test_claims_are_credited_only_to_the_named_contract(mined_full):
    by_key = {(e.alert.ticker, e.alert.strike): e for e in mined_full.entries}
    # "GOOGL 176 call hit 85% so far" names the contract -> credited.
    assert by_key[("GOOGL", 176.0)].claimed_max_pct == 85.0
    # "AMZN 232 put up 120% now, trim" names the contract -> credited.
    assert by_key[("AMZN", 232.0)].claimed_max_pct == 120.0
    # "up 240% on the day overall" names no contract -> credited to nothing.
    assert all(
        (e.claimed_max_pct or 0) != 240.0 for e in mined_full.entries
    )


def test_non_alpha_authors_excluded(cfg, alpha):
    msgs = ReplaySource(ALPHA / "export.html", "alpha").messages()
    others = [m for m in msgs if "alpha alerts" not in m.author.lower()]
    assert others, "fixture should contain non-alerter chatter"
    mined = mine_history(msgs, alpha, cfg.parser)
    raws = {e.alert.raw for e in mined.entries}
    for m in others:
        assert m.text not in raws


def test_system_notification_without_timestamp_is_skipped():
    """DiscordChatExporter emits pin/join notices with no timestamp title;
    load_export drops them rather than dating them to the epoch."""
    msgs = ReplaySource(ALPHA / "export.html", "alpha").messages()
    assert all(m.ts.year == 2027 for m in msgs)
    assert not any("pinned a message" in m.text for m in msgs)
