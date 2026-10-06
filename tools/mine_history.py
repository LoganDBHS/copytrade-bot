"""Mine an alerter's exported chat history into an entries CSV — the
onboarding flow for a new alerter profile.

Run:  .venv\\Scripts\\python.exe tools\\mine_history.py <alerter> [--write]

Reads the export from the profile's fixtures directory (first .html, then
.txt), parses every message through the real parser, and prints a report:
entry/repeat/noise/ambiguous counts, every ambiguous message (each one is a
potential missed trade — eyeball them), and every entry row. --write saves
the rows to <fixtures>/entries.csv for review and the golden test.
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ingestion.replay import load_export  # noqa: E402
from parsing.history import mine_history, to_csv_rows  # noqa: E402
from settings import load_config  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("alerter")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--export", help="export file (default: fixtures dir)")
    ap.add_argument("--write", action="store_true", help="write entries.csv")
    args = ap.parse_args()

    cfg = load_config(args.config)
    profile = cfg.alerters[args.alerter]
    fixtures = Path(profile.fixtures)
    if args.export:
        export = Path(args.export)
    else:
        candidates = sorted(fixtures.glob("*.html")) + sorted(fixtures.glob("*_full.txt"))
        if not candidates:
            sys.exit(f"no export found in {fixtures}")
        export = candidates[0]

    msgs = load_export(export, args.alerter)
    mined = mine_history(msgs, profile, cfg.parser)

    print(f"export: {export}  messages: {len(msgs)}")
    print(
        f"entries: {len(mined.entries)}  same-day repeats: {len(mined.repeats)}  "
        f"ambiguous: {len(mined.ambiguous)}  noise: {mined.noise_count}"
    )
    if mined.ambiguous:
        print("\n--- AMBIGUOUS (fail-closed; check none is a missed entry) ---")
        for r in mined.ambiguous:
            print(f"  {r.reason}\n    raw: {r.raw!r}")
    print("\n--- ENTRIES ---")
    rows = to_csv_rows(mined)
    for row in rows[1:]:
        print("  " + " | ".join(row[:8]))
    if mined.repeats:
        print("\n--- SAME-DAY REPEATS (logged, never traded) ---")
        for e in mined.repeats:
            a = e.alert
            print(f"  {a.ts} {a.ticker} {a.strike} {a.side} :: {a.raw[:70]!r}")

    if args.write:
        out = fixtures / "entries.csv"
        with out.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
