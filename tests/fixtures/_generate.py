"""Render the synthetic corpus into the three export shapes, then mine goldens.

    python tests/fixtures/_generate.py

Writes, deterministically:

    tests/fixtures/alpha/export.html      DiscordChatExporter HTML
    tests/fixtures/alpha/entries.csv      golden record mined from it
    tests/fixtures/bravo/alerts_full.txt  bracketed text export
    tests/fixtures/bravo/sample_pasted.txt  Discord copy-paste export
    tests/fixtures/bravo/entries.csv      golden record mined from alerts_full

Every byte of fixture content originates in _corpus.py. Nothing here is
derived from a real channel: the renderers reproduce the *structure* of each
export format (the part `ingestion.replay` has to cope with), and the corpus
supplies invented message text.
"""

from __future__ import annotations

import csv
import html
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(HERE)]

from _corpus import (  # noqa: E402
    BRAVO_PASTED,
    BRAVO_PASTED_DAY,
    Msg,
    alpha_messages,
    bravo_messages,
)

from ingestion.replay import load_export  # noqa: E402
from parsing.history import mine_history, to_csv_rows  # noqa: E402
from settings import load_config  # noqa: E402

HTML_TS = "%A, %B %d, %Y %I:%M %p"
GROUP_GAP = timedelta(minutes=7)   # Discord groups messages this close together


# ---------------------------------------------------------------------------
# DiscordChatExporter HTML
# ---------------------------------------------------------------------------

_HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{channel}</title>
</head>
<body>
<div class="preamble">
  <div class="preamble__entry">Synthetic Test Server</div>
  <div class="preamble__entry preamble__entry--small">{channel}</div>
</div>
<div class="chatlog">
"""

_HTML_TAIL = """</div>
</body>
</html>
"""


def _emoji_img(name: str) -> str:
    """A custom-emoji <img>; the extractor keeps its alt text, not the src."""
    return f'<img class="chatlog__emoji" src="emoji/{name}.png" alt=":{name}:">'


def render_html(messages: list[Msg], channel: str) -> str:
    """Reproduce the structural features parse_html_export has to survive.

    - messages grouped under one author header, the author carried forward
    - full timestamps on group leaders, short timestamps inside a group
    - a reply preview, a reaction bar, and an "(edited)" marker, all of which
      must be excluded from the message text
    - a custom-emoji <img> whose alt text must be kept
    - a system notification with no timestamp title, which must be skipped
    """
    out = [_HTML_HEAD.format(channel=html.escape(channel))]
    mid = 900_000_000_000_000_000
    prev_author = None
    prev_ts: datetime | None = None

    # A pinned-message system notification: no timestamp title anywhere in the
    # container, so load_export drops it.
    out.append(
        f'<div class="chatlog__message-container" data-message-id="{mid}">\n'
        '  <div class="chatlog__system-notification">\n'
        '    <span class="chatlog__system-notification-author">ServerAdmin</span>\n'
        '    <span class="chatlog__system-notification-content">'
        "pinned a message.</span>\n"
        "  </div>\n"
        "</div>\n"
    )
    mid += 1

    for i, m in enumerate(messages):
        leads = (
            prev_author != m.author
            or prev_ts is None
            or m.ts - prev_ts > GROUP_GAP
        )
        body = html.escape(m.text).replace("\n", "<br>\n")
        # Sprinkle the structural extras deterministically rather than
        # randomly, so the fixture is byte-stable across regenerations.
        reply = i == 4
        edited = i % 11 == 3
        reactions = i % 7 == 2
        emoji = i % 13 == 5

        out.append(
            f'<div class="chatlog__message-container" data-message-id="{mid}">\n'
            f'  <div class="chatlog__message">\n'
        )
        if reply:
            out.append(
                '    <div class="chatlog__reply">\n'
                '      <span class="chatlog__reply-author">ServerAdmin</span>\n'
                '      <span class="chatlog__reply-content">'
                "quoted text that is not part of this message</span>\n"
                "    </div>\n"
            )
        if leads:
            out.append(
                '    <div class="chatlog__message-aside">\n'
                f'      <img class="chatlog__avatar" src="avatars/{i % 3}.png" alt="">\n'
                "    </div>\n"
                '    <div class="chatlog__message-primary">\n'
                '      <div class="chatlog__header">\n'
                f'        <span class="chatlog__author" title="{html.escape(m.author)}">'
                f"{html.escape(m.author)}</span>\n"
                f'        <span class="chatlog__timestamp" '
                f'title="{m.ts.strftime(HTML_TS)}">'
                f"{m.ts.strftime('%m/%d/%Y %I:%M %p')}</span>\n"
                "      </div>\n"
            )
        else:
            out.append(
                '    <div class="chatlog__message-aside">\n'
                f'      <span class="chatlog__short-timestamp" '
                f'title="{m.ts.strftime(HTML_TS)}">'
                f"{m.ts.strftime('%I:%M %p')}</span>\n"
                "    </div>\n"
                '    <div class="chatlog__message-primary">\n'
            )
        out.append(f'      <div class="chatlog__content"><span>{body}</span>')
        if emoji:
            out.append(" " + _emoji_img("rocket"))
        if edited:
            out.append(
                '<span class="chatlog__edited-timestamp" '
                f'title="{m.ts.strftime(HTML_TS)}">(edited)</span>'
            )
        out.append("</div>\n")
        if reactions:
            out.append(
                '      <div class="chatlog__reactions">\n'
                '        <div class="chatlog__reaction">'
                '<img class="chatlog__emoji" src="emoji/fire.png" alt=":fire:">'
                '<span class="chatlog__reaction-count">12</span></div>\n'
                "      </div>\n"
            )
        out.append("    </div>\n  </div>\n</div>\n")

        prev_author, prev_ts = m.author, m.ts
        mid += 1

    out.append(_HTML_TAIL)
    return "".join(out)


# ---------------------------------------------------------------------------
# Bracketed text export
# ---------------------------------------------------------------------------

_DISCRIMINATOR = {
    "Bravo Alerts": "#1561",
    "bravo_old": "#4420",
    "ServerAdmin": "#0001",
    "MemberOne": "#7783",
}


def render_bracketed(messages: list[Msg]) -> str:
    """Bracketed export: both date forms, a backslash date, '---' separators."""
    blocks = []
    for i, m in enumerate(messages):
        if i % 9 == 4:                       # long weekday form
            ts = m.ts.strftime("%A, %B %d, %Y %I:%M %p")
        elif i % 9 == 7:                     # backslash form
            ts = m.ts.strftime("%m\\%d\\%Y %I:%M %p")
        else:
            ts = m.ts.strftime("%-m/%-d/%Y %I:%M %p") if sys.platform != "win32" \
                else m.ts.strftime("%#m/%#d/%Y %I:%M %p")
        tag = _DISCRIMINATOR.get(m.author, "")
        blocks.append(f"[{ts}] {m.author}{tag}: {m.text}")
    return "\n---\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# Discord copy-paste export
# ---------------------------------------------------------------------------

def render_pasted(messages: list[Msg], export_day: datetime) -> str:
    """Copy-paste export: APP headers, a plain header, a time-only header,
    a curator comment, and a leading message whose header scrolled out."""
    lines: list[str] = ["# synthetic copy-paste sample — see _corpus.py"]
    for i, m in enumerate(messages):
        if i == 0:
            # Header scrolled out of the copied region: content only. The
            # parser backfills this from the first authored message.
            lines.extend(m.text.splitlines())
            continue
        if i % 3 == 1:
            lines += [m.author, "APP", f" — {m.ts.strftime('%-m/%-d/%y, %I:%M %p')}"
                      if sys.platform != "win32"
                      else f" — {m.ts.strftime('%#m/%#d/%y, %I:%M %p')}"]
        elif i % 3 == 2:
            stamp = (m.ts.strftime("%-m/%-d/%y, %I:%M %p") if sys.platform != "win32"
                     else m.ts.strftime("%#m/%#d/%y, %I:%M %p"))
            lines.append(f"{m.author} — {stamp}")
        else:
            # Time-only header: Discord renders these for the export day.
            lines += [m.author, "APP", f" — {m.ts.strftime('%I:%M %p')}"]
        lines.extend(m.text.splitlines())
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Drive it
# ---------------------------------------------------------------------------

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"  wrote {path.relative_to(ROOT)} ({len(text):,} bytes)")


def _write_golden(path: Path, export: Path, alerter: str, cfg, export_date=None) -> None:
    msgs = load_export(export, alerter, export_date=export_date)
    mined = mine_history(msgs, cfg.alerters[alerter], cfg.parser)
    with path.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, lineterminator="\n").writerows(to_csv_rows(mined))
    print(
        f"  wrote {path.relative_to(ROOT)}: {len(mined.entries)} entries, "
        f"{len(mined.repeats)} repeats, {len(mined.ambiguous)} ambiguous, "
        f"{mined.noise_count} noise"
    )


def main() -> None:
    cfg = load_config(ROOT / "config.example.yaml")

    print("alpha:")
    alpha_html = HERE / "alpha" / "export.html"
    _write(alpha_html, render_html(alpha_messages(), "#alpha-alerts"))
    _write_golden(HERE / "alpha" / "entries.csv", alpha_html, "alpha", cfg)

    print("bravo:")
    bravo_txt = HERE / "bravo" / "alerts_full.txt"
    _write(bravo_txt, render_bracketed(bravo_messages()))
    _write_golden(HERE / "bravo" / "entries.csv", bravo_txt, "bravo", cfg)
    _write(
        HERE / "bravo" / "sample_pasted.txt",
        render_pasted(BRAVO_PASTED, BRAVO_PASTED_DAY),
    )


if __name__ == "__main__":
    main()
