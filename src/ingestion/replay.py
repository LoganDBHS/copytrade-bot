"""ReplaySource: feed exported chat history through the same pipeline as live.

Understands three export shapes:
- Bracketed export lines: "[6/3/2026 6:40 AM] Bravo Alerts#1561: text",
  messages separated by "---", either short or long ("[Thursday, June 11,
  2026 6:36 AM]") date forms, and the occasional backslash date ("[8\\11\\2026").
- Discord copy-paste: "Author\\nAPP\\n — 8/3/26, 8:21 AM" or
  "Author — 8/3/26, 8:21 AM" headers with message lines between them;
  time-only headers ("— 6:49 AM") mean the export day itself.
- DiscordChatExporter HTML: one chatlog__message-container per message with
  a data-message-id (the dedupe key), full timestamps in the title attribute
  of chatlog__timestamp / chatlog__short-timestamp, author carried forward
  across a message group, custom-emoji <img> alt text kept, and the
  "(edited)" marker, replies, and reactions excluded from the text.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

from ingestion.source import NotificationSource, RawMessage

_BRACKET_HDR = re.compile(r"^\[(?P<ts>[^\]]+)\]\s+(?P<author>[^:]+):\s?(?P<rest>.*)$")
_BRACKET_TS_FORMATS = ("%m/%d/%Y %I:%M %p", "%A, %B %d, %Y %I:%M %p")
_PASTE_HDR = re.compile(r"^(?P<author>.*?),?\s*—\s*(?P<ts>[\d/]*,?\s*\d{1,2}:\d{2}\s*[AP]M)\s*$")
_PASTE_TS_FORMATS = ("%m/%d/%y, %I:%M %p", "%m/%d/%Y, %I:%M %p")
_SKIP_LINES = {"APP", "Image", ""}
_ZWSP = "​"


def _parse_bracket_ts(raw: str) -> datetime:
    raw = raw.replace("\\", "/").strip()
    for fmt in _BRACKET_TS_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognized export timestamp: {raw!r}")


def parse_bracketed_export(text: str, alerter_id: str, origin: str) -> list[RawMessage]:
    messages: list[RawMessage] = []
    block: list[str] = []
    line_no_of_block = 0
    lines = text.splitlines()

    def flush(start_line: int, chunk: list[str]) -> None:
        if not chunk:
            return
        m = _BRACKET_HDR.match(chunk[0])
        if not m:
            return  # stray continuation without a header — nothing to anchor to
        body = m.group("rest")
        rest = "\n".join(chunk[1:])
        full = (body + ("\n" + rest if rest else "")).strip()
        messages.append(
            RawMessage(
                source_id=f"{origin}:{start_line}",
                alerter_id=alerter_id,
                ts=_parse_bracket_ts(m.group("ts")),
                author=m.group("author").strip(),
                text=full.replace(_ZWSP, ""),
            )
        )

    for i, ln in enumerate(lines, start=1):
        if ln.strip() == "---":
            flush(line_no_of_block, block)
            block = []
            continue
        if not block:
            line_no_of_block = i
        block.append(ln)
    flush(line_no_of_block, block)
    return messages


def parse_pasted_export(
    text: str, alerter_id: str, origin: str, export_date: date | None = None
) -> list[RawMessage]:
    messages: list[RawMessage] = []
    author = ""
    ts = datetime(1970, 1, 1)
    buffer: list[str] = []
    buffer_line = 0

    def flush() -> None:
        nonlocal buffer
        body = "\n".join(buffer).replace(_ZWSP, "").strip()
        buffer = []
        if body:
            messages.append(
                RawMessage(
                    source_id=f"{origin}:{buffer_line}",
                    alerter_id=alerter_id,
                    ts=ts,
                    author=author,
                    text=body,
                )
            )

    def parse_ts(raw: str) -> datetime:
        raw = raw.strip()
        for fmt in _PASTE_TS_FORMATS:
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        # Time-only header: Discord shows these for "today" (the export day).
        t = datetime.strptime(raw.lstrip(", "), "%I:%M %p")
        base = export_date or ts.date()
        return datetime(base.year, base.month, base.day, t.hour, t.minute)

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        stripped = ln.replace(_ZWSP, "").strip()
        if stripped.startswith("# "):  # fixture curator comment
            i += 1
            continue
        # "Author\nAPP\n — time" app-style header
        if (
            i + 2 < len(lines)
            and lines[i + 1].strip() == "APP"
            and lines[i + 2].lstrip().startswith("—")
        ):
            flush()
            author = stripped
            ts = parse_ts(lines[i + 2].lstrip().lstrip("—"))
            i += 3
            continue
        m = _PASTE_HDR.match(stripped)
        if m and m.group("author"):
            flush()
            author = m.group("author").strip().rstrip(",")
            ts = parse_ts(m.group("ts"))
            i += 1
            continue
        if stripped in _SKIP_LINES:
            flush()
            i += 1
            continue
        if stripped.startswith("@alert"):
            flush()
            buffer_line = i + 1
            buffer.append(stripped)
            # A bare "@alert" line prefixes exactly the next content line;
            # later lines are separate messages in this export shape.
            if stripped != "@alert":
                flush()
        elif buffer:  # buffer is ["@alert"] — attach this line and close
            buffer.append(stripped)
            flush()
        else:
            buffer_line = i + 1
            buffer.append(stripped)
            flush()
        i += 1
    flush()

    # Content before the first header is the tail of the first author's
    # message group whose header scrolled out of the copied region.
    first_authored = next((m for m in messages if m.author), None)
    if first_authored:
        for idx, m in enumerate(messages):
            if m.author:
                break
            messages[idx] = RawMessage(
                source_id=m.source_id,
                alerter_id=m.alerter_id,
                ts=first_authored.ts,
                author=first_authored.author,
                text=m.text,
            )
    return messages


class _DiscordHtmlExtractor(HTMLParser):
    """Pull (message id, author, timestamp, text) out of DiscordChatExporter HTML.

    Tracks the open-tag stack so the content div's subtree is captured exactly:
    void elements (br, img, ...) never join the stack, so a stray <br> can't
    make the content region leak into the next message's header.
    """

    _VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "source", "track", "wbr",
    }
    _EXCLUDE_CLASSES = (
        "chatlog__edited-timestamp",  # "(edited)" marker inside the content div
        "chatlog__reply",             # quoted reply preview above the message
        "chatlog__reactions",         # emoji reaction counts under the message
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[dict] = []
        self._stack: list[str] = []
        self._cur: dict | None = None
        self._group_author = ""
        self._content_depth: int | None = None
        self._exclude_depth: int | None = None
        self._author_depth: int | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag not in self._VOID:
            self._stack.append(tag)
        if "chatlog__message-container" in classes and "data-message-id" in a:
            self._cur = {
                "id": a["data-message-id"],
                "author": self._group_author,
                "ts": "",
                "parts": [],
            }
            self.messages.append(self._cur)
            self._content_depth = None
            self._exclude_depth = None
            self._author_depth = None
        if self._cur is None:
            return
        depth = len(self._stack)
        if self._exclude_depth is None and any(
            c.startswith(self._EXCLUDE_CLASSES) for c in classes
        ):
            self._exclude_depth = depth
            return
        if self._exclude_depth is not None:
            return
        if "chatlog__author" in classes:
            self._author_depth = depth
        if not self._cur["ts"] and (
            "chatlog__timestamp" in classes or "chatlog__short-timestamp" in classes
        ):
            self._cur["ts"] = a.get("title", "")
        if self._content_depth is None and "chatlog__content" in classes:
            self._content_depth = depth
        elif self._content_depth is not None:
            if tag == "br":
                self._cur["parts"].append("\n")
            elif tag == "img" and a.get("alt"):
                self._cur["parts"].append(a["alt"])

    def handle_endtag(self, tag: str) -> None:
        while self._stack:
            if self._stack.pop() == tag:
                break
        depth = len(self._stack)
        if self._content_depth is not None and depth < self._content_depth:
            self._content_depth = None
        if self._exclude_depth is not None and depth < self._exclude_depth:
            self._exclude_depth = None
        if self._author_depth is not None and depth < self._author_depth:
            self._author_depth = None

    def handle_data(self, data: str) -> None:
        if self._cur is None or self._exclude_depth is not None:
            return
        if self._author_depth is not None:
            name = data.strip()
            if name:
                self._group_author = name
                self._cur["author"] = name
        elif self._content_depth is not None:
            self._cur["parts"].append(data)


_HTML_TS_FORMAT = "%A, %B %d, %Y %I:%M %p"


def parse_html_export(text: str, alerter_id: str, origin: str) -> list[RawMessage]:
    extractor = _DiscordHtmlExtractor()
    extractor.feed(text)
    messages: list[RawMessage] = []
    for m in extractor.messages:
        body = re.sub(r"\n{3,}", "\n\n", "".join(m["parts"])).replace(_ZWSP, "").strip()
        if not m["ts"]:
            continue  # system notifications (pins, joins) carry no timestamp title
        messages.append(
            RawMessage(
                source_id=f"{origin}:{m['id']}",
                alerter_id=alerter_id,
                ts=datetime.strptime(m["ts"].strip(), _HTML_TS_FORMAT),
                author=m["author"],
                text=body,
            )
        )
    return messages


def load_export(
    path: str | Path, alerter_id: str, export_date: date | None = None
) -> list[RawMessage]:
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    if first.lstrip().startswith("<"):
        return parse_html_export(text, alerter_id, p.name)
    if first.startswith("["):
        return parse_bracketed_export(text, alerter_id, p.name)
    return parse_pasted_export(text, alerter_id, p.name, export_date=export_date)


class ReplaySource(NotificationSource):
    def __init__(
        self, path: str | Path, alerter_id: str, export_date: date | None = None
    ) -> None:
        self.path = Path(path)
        self.alerter_id = alerter_id
        self.export_date = export_date

    def messages(self) -> list[RawMessage]:
        return load_export(self.path, self.alerter_id, self.export_date)

    async def stream(self) -> AsyncIterator[RawMessage]:
        for msg in self.messages():
            yield msg
