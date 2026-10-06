"""Phase 1 gate: the parser must reproduce the golden record exactly.

The fixtures are synthetic — generated from tests/fixtures/_corpus.py by
tests/fixtures/_generate.py, which renders the corpus into a bracketed text
export and a Discord copy-paste export. The corpus carries one hand-authored
message per grammar feature, so this file is the contract for the loose,
lowercase, typo-heavy alert grammar.

    alerts_full.txt -> entries.csv: 49 unique entries (spanning an alerter
    rename mid-history: bravo_old -> Bravo Alerts, both handles in
    author_hints), every column equal, 1 same-day repeat flagged, 0 false
    entries, 1 deliberate ambiguous.

    sample_pasted.txt: the copy-paste shape parses to the same contracts
    with no phantoms.

Regenerate with:  python tests/fixtures/_generate.py
"""

import csv
from datetime import date

import pytest

from conftest import FIXTURES
from ingestion.replay import ReplaySource
from parsing.history import CSV_HEADER, mine_history, to_csv_rows

BRAVO = FIXTURES / "bravo"


@pytest.fixture(scope="module")
def mined_full(cfg, bravo):
    msgs = ReplaySource(BRAVO / "alerts_full.txt", "bravo").messages()
    return mine_history(msgs, bravo, cfg.parser)


def golden_rows():
    with (BRAVO / "entries.csv").open(encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.reader(fh) if r]
    assert rows[0] == CSV_HEADER
    return rows[1:]


def test_no_false_entries(mined_full):
    assert len(mined_full.entries) == 49
    assert len(mined_full.repeats) == 1
    # One deliberate two-strike message, which must fail closed.
    assert len(mined_full.ambiguous) == 1
    assert "or maybe the 620" in mined_full.ambiguous[0].raw


def test_reproduces_golden_csv_exactly(mined_full):
    mined = to_csv_rows(mined_full)[1:]
    golden = golden_rows()
    assert len(mined) == len(golden) == 49
    for i, (m, g) in enumerate(zip(mined, golden, strict=True), start=1):
        assert m == g, f"row {i} differs: mined={m} golden={g}"


def test_same_day_repeats_are_flagged_not_traded(mined_full):
    repeats = {
        (r.alert.ts.date().isoformat(), r.alert.ticker, r.alert.strike, r.alert.side)
        for r in mined_full.repeats
    }
    assert repeats == {("2027-04-08", "NVDA", 141.0, "CALL")}


def test_both_handles_of_a_renamed_alerter_are_mined(mined_full):
    """The alerter renamed mid-history; author_hints carries both handles,
    so entries from before and after the rename land in one record."""
    authors = {e.alert.author for e in mined_full.entries}
    assert any("bravo_old" in a for a in authors)
    assert any("Bravo Alerts" in a for a in authors)


def test_ticker_aliases_and_typos_resolve(mined_full):
    by_raw = {e.alert.raw: e.alert for e in mined_full.entries}
    assert by_raw["@alert telsa 410 put 0dte gamble"].ticker == "TSLA"
    assert by_raw["@alert aaple 246 call 0dte riksy"].ticker == "AAPL"
    assert by_raw["@alert nvdia 141 call 0dte go light"].ticker == "NVDA"
    # The tag typo is kept in the alerter's own spelling for per-tag stats.
    assert by_raw["@alert aaple 246 call 0dte riksy"].tags == ["riksy"]


def test_risk_scale_is_not_an_expiry(mined_full):
    a = next(
        e.alert for e in mined_full.entries if "level 9/10 risk" in e.alert.raw
    )
    assert (a.ticker, a.strike, a.expiry) == ("SPY", 622.0, "DTE0")


def test_pasted_sample(cfg, bravo):
    msgs = ReplaySource(
        BRAVO / "sample_pasted.txt", "bravo", export_date=date(2027, 5, 24)
    ).messages()
    mined = mine_history(msgs, bravo, cfg.parser)
    assert mined.ambiguous == []
    got = [
        (e.alert.ticker, e.alert.strike, e.alert.side, e.alert.expiry,
         e.alert.alert_price, e.alert.tags, e.claimed_max_pct)
        for e in mined.entries
    ]
    assert got == [
        # Leading message whose header scrolled out of the copied region.
        ("GOOGL", 176.5, "CALL", "DTE0", 0.8, ["go in with profits"], None),
        ("QQQ", 530.0, "CALL", "DTE0", None, ["gamble"], None),
        # Both TSLA puts are credited with 140%: the claim line names the
        # ticker but not a strike, and mentions_contract matches on ticker
        # alone. Deliberate — over-crediting is the conservative direction
        # for a record audit, and the dashboard reports claimed vs realized.
        ("TSLA", 408.0, "PUT", "DTE0", None, ["risky", "very risky"], 140.0),
        ("TSLA", 406.0, "PUT", "DTE0", None, ["risky"], 140.0),
        ("SPY", 624.0, "PUT", "DTE0", None, ["risky", "super risky"], None),
        ("TSLA", 418.0, "CALL", "DTE0", 0.69, ["riksy"], None),
        ("NVDA", 142.5, "CALL", "DTE0", None, ["not gonna count", "risky"], None),
        # Deliberate formal-grammar footer in the fixture.
        ("SCHW", 92.0, "CALL", "DATE5/28", 0.32, [], None),
    ]
