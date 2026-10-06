"""ReplaySource format handling."""

from datetime import date, datetime

from ingestion.replay import parse_bracketed_export, parse_pasted_export

BRACKETED = """\
[6/3/2026 6:40 AM] Bravo Alerts#1561: @alert
iwm 289 put 0dte RISKY
---
[Thursday, June 11, 2026 6:36 AM] Bravo Alerts#1561: @alert
11% tp whenever
---
[8\\11\\2026 8:42 AM] Bravo Alerts#1561: nothing is looking good
"""

PASTED = """\
@alert
orphaned first line
Bravo Alerts
APP
 — 8/3/26, 8:21 AM
@alert
googl 372.5 call 0dte 0.80
136%
ServerAdmin,  — 8/3/26, 8:31 AM
beauty
Bravo Alerts
APP
 — 6:49 AM
@alert
tsla 347.5 call 0dte riksy 0.69
"""


def test_bracketed_formats_and_backslash_dates():
    msgs = parse_bracketed_export(BRACKETED, "bravo", "t.txt")
    assert [m.ts for m in msgs] == [
        datetime(2026, 6, 3, 6, 40),
        datetime(2026, 6, 11, 6, 36),
        datetime(2026, 8, 11, 8, 42),
    ]
    assert msgs[0].text == "@alert\niwm 289 put 0dte RISKY"
    assert msgs[0].author == "Bravo Alerts#1561"
    assert msgs[2].text == "nothing is looking good"


def test_pasted_headers_authors_and_time_only_dates():
    msgs = parse_pasted_export(PASTED, "bravo", "t.txt", export_date=date(2026, 8, 17))
    by_text = {m.text: m for m in msgs}
    entry = by_text["@alert\ngoogl 372.5 call 0dte 0.80"]
    assert entry.author == "Bravo Alerts"
    assert entry.ts == datetime(2026, 8, 3, 8, 21)
    assert by_text["beauty"].author == "ServerAdmin"
    # time-only header resolves to the export day
    late = by_text["@alert\ntsla 347.5 call 0dte riksy 0.69"]
    assert late.ts == datetime(2026, 8, 17, 6, 49)
    assert late.author == "Bravo Alerts"
    # orphaned leading lines inherit the first header's author
    assert by_text["@alert\norphaned first line"].author == "Bravo Alerts"


def test_source_ids_are_unique():
    msgs = parse_bracketed_export(BRACKETED, "bravo", "t.txt")
    ids = [m.source_id for m in msgs]
    assert len(ids) == len(set(ids))
