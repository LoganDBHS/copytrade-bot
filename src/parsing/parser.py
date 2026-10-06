"""Token-role parser.

The alerter writes {ticker, strike, side} in six different word orders, so we
never match line patterns. We take the first paragraph block, tokenize, assign
each token a role, and require exactly one ticker, one side, and one strike —
any order. Conflicting role assignments fail closed (AMBIGUOUS, no trade).
"""

from __future__ import annotations

import re

from parsing.models import ParsedAlert, ParseKind, ParseResult
from settings import AlerterProfile, ParserConfig

SIDE_WORDS = {"call": "CALL", "calls": "CALL", "put": "PUT", "puts": "PUT"}
DTE_WORDS = {"0dte": "DTE0", "odte": "DTE0", "1dte": "DTE1"}

STRIKE_MIN = 50.0
PRICE_MAX = 20.0

# Words that must never fuzzy-match a ticker (chat filler, jargon, guard words).
FUZZY_STOPWORDS = {
    "alert", "yall", "wall", "walls", "will", "fill", "cons", "bang", "play",
    "plays", "hold", "held", "risk", "risky", "level", "very", "super",
    "gamble", "lotto", "fomo", "scalp", "scalps", "light", "profits", "with",
    "this", "that", "shit", "fuck", "dont", "cant", "gonna", "lose", "lost",
    "win", "wins", "loss", "when", "then", "than", "them", "they", "your",
    "youre", "take", "took", "here", "there", "what", "want", "need", "good",
    "come", "went", "going", "look", "like", "just", "only", "over", "under",
    "back", "down", "today", "days", "week", "trade", "trades", "trading",
    "stop", "breakout", "playing", "count", "counting", "avoid", "swing",
    "swings", "hedge", "gone",
}

_PUNCT_STRIP = "#*_`~|()[]{}<>.,!?:;\"'\\/+-— "
_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/\d{2,4})?$")
_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")
_STRIKE_SIDE_RE = re.compile(r"^(\d+(?:\.\d+)?)([cp])$", re.IGNORECASE)
_AT_PRICE_RE = re.compile(r"^@(\d+(?:\.\d+)?)$")
_ALPHA_RE = re.compile(r"^[a-zA-Z]+$")

STATUS_WORD_RE = re.compile(r"\b(hit|held)\b", re.IGNORECASE)
COMMENTARY_PHRASES = ("looking at", "call it a day", "call out", "recap")
# Parenthetical asides never carry spine data, but their numbers fake it:
# "NVDA .8 FILL (typing this It just went to .96 wait for it to come back)".
_PAREN_RE = re.compile(r"\([^()]*\)")

# Vertical-bar lookalikes some alerters use as field separators, glued to the
# neighboring token ("QQQ〡 733 Call 〡 06/30 〡 1.20 Fill").
_SEPARATOR_NORMALIZE = str.maketrans({"〡": " ", "│": " ", "｜": " "})
# ".72" -> "0.72" so a bare leading-dot price isn't stripped into a fake strike.
_LEADING_DOT_RE = re.compile(r"(?<![\d.])\.(\d)")
# "1.00 - 1.10 FILL" / ".10-.45": a fill range; the conservative price is the
# first (lower) bound. Only collapses when both bounds are price-sized.
_PRICE_RANGE_RE = re.compile(r"(\d+\.\d+)\s*-\s*(\d+\.\d+)")
# Everything from a stop-loss marker onward is exit talk, not entry spine:
# "stop .45", "stop is if we break 749.00", "SL 1.00". A bare number right
# before the marker ("0.6 STOP") belongs to it too.
_STOP_MARKERS = {"stop", "stops", "sl", "stoploss"}

# Invisible formatting characters: zero-widths, word joiners, and bidi
# marks/embeddings/isolates. Windows toast text wraps mentions in U+2068/U+2069
# ("⁨@alert⁩"), which would otherwise tokenize as standalone tokens and derail
# the leading-prefix strip. Line/paragraph separators are excluded — splitlines
# needs them.
_INVISIBLE_RE = re.compile(
    "[\N{ZERO WIDTH SPACE}-\N{RIGHT-TO-LEFT MARK}"      # U+200B–U+200F
    "\N{LEFT-TO-RIGHT EMBEDDING}-\N{RIGHT-TO-LEFT OVERRIDE}"  # U+202A–U+202E
    "\N{WORD JOINER}-\N{INVISIBLE PLUS}"                # U+2060–U+2064
    "\N{LEFT-TO-RIGHT ISOLATE}-\N{POP DIRECTIONAL ISOLATE}"   # U+2066–U+2069
    "\N{ZERO WIDTH NO-BREAK SPACE}]"                    # U+FEFF
)


def first_block(text: str) -> str:
    """First paragraph: lines up to the first blank line, joined with spaces."""
    lines = [ln.rstrip() for ln in _INVISIBLE_RE.sub("", text).splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    block: list[str] = []
    for ln in lines:
        if not ln.strip():
            break
        block.append(ln.strip())
    return " ".join(block).strip()


def damerau_levenshtein(a: str, b: str) -> int:
    """Optimal string alignment distance (transpositions count as one edit)."""
    la, lb = len(a), len(b)
    if abs(la - lb) > 3:
        return abs(la - lb)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def _clean(token: str) -> str:
    t = token.strip(_PUNCT_STRIP)
    return t.lstrip("$")


def _is_risk_scale_neighbor(word: str) -> bool:
    w = word.lower()
    return w.startswith("risk") or w == "level"


def extract_tags(text: str, vocab: tuple[str, ...], max_ngram: int = 3) -> list[str]:
    """Tag extraction that mirrors how the alerter writes tags.

    - A vocab term counts when it appears all-lowercase or ALL-CAPS (mixed
      case like "VERY risky" is the alerter mid-sentence, not a tag).
    - Typo forms (riksy, riskyy, re ntry, reentry) are caught by fuzzy
      matching word n-grams against the vocab and recorded in their raw form,
      so per-tag stats keep the alerter's own vocabulary.
    """
    found: set[str] = set()
    lower = text.lower()

    for term in vocab:
        tl = term.lower()
        start = 0
        while (idx := lower.find(tl, start)) != -1:
            seg = text[idx : idx + len(term)]
            if seg == tl or seg == term.upper():
                found.add(tl)
                break
            start = idx + 1

    # Fuzzy pass over alphabetic word runs (numbers break a run).
    words: list[list[str]] = [[]]
    for tok in text.split():
        c = tok.strip(_PUNCT_STRIP)
        if _ALPHA_RE.match(c):
            words[-1].append(c)
        elif words[-1]:
            words.append([])
    for run in words:
        for n in range(1, max_ngram + 1):
            for i in range(len(run) - n + 1):
                gram_raw = " ".join(run[i : i + n])
                if len(gram_raw) < 4:
                    continue
                if not (gram_raw.islower() or gram_raw.isupper()):
                    continue
                gram = gram_raw.lower()
                for term in vocab:
                    tl = term.lower()
                    if gram == tl or gram in tl:
                        continue  # exact/substring handled by the literal pass
                    if gram == tl + "s":
                        continue  # plural inflection, not a typo form ("scalps")
                    for variant in (tl, tl.replace(" ", "")):
                        thresh = 1 if len(variant) <= 7 else 2
                        if damerau_levenshtein(gram, variant) <= thresh:
                            found.add(gram)
                            break
    return sorted(found)


def parse_message(
    text: str,
    profile: AlerterProfile,
    parser_cfg: ParserConfig,
) -> ParseResult:
    block = first_block(text)
    if not block:
        return ParseResult(ParseKind.NOISE, reason="empty", raw=block)

    lower_block = block.lower()

    # Status guards: these are never entries, no matter what else is present.
    # "%" is softer: some alerters state profit targets inside entry messages
    # ("0.50 Fill @alert Only looking for 20-30%"), so a %-message can still
    # be an entry — but only a fully specified one (spine + expiry + price),
    # and it can never raise an ambiguity alert.
    if "->" in block:
        return ParseResult(ParseKind.NOISE, reason="status_arrow", raw=block)
    if STATUS_WORD_RE.search(block):
        return ParseResult(ParseKind.NOISE, reason="status_word", raw=block)
    for phrase in COMMENTARY_PHRASES:
        if phrase in lower_block:
            return ParseResult(ParseKind.NOISE, reason="commentary", raw=block)
    has_pct = "%" in block

    norm = block.translate(_SEPARATOR_NORMALIZE)
    norm = _PAREN_RE.sub(" ", norm)
    norm = _LEADING_DOT_RE.sub(r"0.\1", norm)

    def _collapse_range(m: re.Match) -> str:
        if float(m.group(1)) < PRICE_MAX and float(m.group(2)) < PRICE_MAX:
            return m.group(1)
        return m.group(0)

    norm = _PRICE_RANGE_RE.sub(_collapse_range, norm)

    prefix = profile.alert_prefix.lower()
    if prefix:
        # Detach a glued prefix ("FILL@alert") so it tokenizes on its own.
        norm = re.sub(
            re.escape(profile.alert_prefix), f" {profile.alert_prefix} ",
            norm, flags=re.IGNORECASE,
        )

    tokens = norm.split()
    if prefix and tokens and tokens[0].lower() == prefix:
        tokens = tokens[1:]

    # Truncate at the first stop-loss marker or mid-message alert prefix —
    # everything after either is exit management or commentary, and its
    # numbers must not masquerade as strikes or prices ("stop .45",
    # "@alert end target 277").
    for i, tok in enumerate(tokens):
        c = _clean(tok).lower()
        if tok.lstrip(_PUNCT_STRIP).lower() == prefix and prefix:
            tokens = tokens[:i]
            break
        if c in _STOP_MARKERS:
            cut = i
            if cut > 0 and _NUM_RE.match(_clean(tokens[cut - 1])):
                cut -= 1  # "0.6 STOP" — the number belongs to the stop
            tokens = tokens[:cut]
            break

    cleaned = [_clean(t) for t in tokens]
    if cleaned and cleaned[0].lower() == "sto":
        return ParseResult(ParseKind.NOISE, reason="sell_to_open", raw=block)

    whitelist = {t.upper() for t in parser_cfg.ticker_whitelist}
    upper_only = {t.upper() for t in parser_cfg.uppercase_only_tickers}
    aliases = profile.ticker_aliases

    tickers: set[str] = set()
    fuzzy_tickers: set[str] = set()
    sides: set[str] = set()
    strikes: set[float] = set()
    prices: set[float] = set()
    dte: str | None = None
    dates: list[str] = []

    for i, tok in enumerate(tokens):
        c = cleaned[i]
        if not c:
            continue
        cl = c.lower()

        # Mentions/prefixes: "@alert", "@Bravo"; "@0.32" is a fill price in
        # trading shorthand regardless of the surrounding grammar.
        if tok.lstrip(_PUNCT_STRIP).startswith("@"):
            m = _AT_PRICE_RE.match(tok.strip(_PUNCT_STRIP))
            if m:
                val = float(m.group(1))
                if val < PRICE_MAX:
                    prices.add(val)
            continue

        if cl in SIDE_WORDS:
            sides.add(SIDE_WORDS[cl])
            continue
        if cl in DTE_WORDS:
            dte = DTE_WORDS[cl]
            continue

        m = _DATE_RE.match(cl)
        if m:
            mo, day = int(m.group(1)), int(m.group(2))
            if 1 <= mo <= 12 and 1 <= day <= 31:
                prev_c = cleaned[i - 1] if i > 0 else ""
                next_c = cleaned[i + 1] if i + 1 < len(cleaned) else ""
                if _is_risk_scale_neighbor(prev_c) or _is_risk_scale_neighbor(next_c):
                    continue  # "8/10 risk", "level 9/10" — a scale, not a date
                dates.append(f"DATE{mo}/{day}")
            continue

        if _NUM_RE.match(cl):
            val = float(cl)
            if val >= STRIKE_MIN:
                strikes.add(val)
            elif val < PRICE_MAX and "." in cl:
                prices.add(val)
            continue

        m = _STRIKE_SIDE_RE.match(cl)
        if m and float(m.group(1)) >= STRIKE_MIN:
            strikes.add(float(m.group(1)))
            sides.add("CALL" if m.group(2).lower() == "c" else "PUT")
            continue

        if cl in aliases:
            tickers.add(aliases[cl])
            continue
        if c.upper() in whitelist:
            # Tickers that double as English words (NOW, NDX-as-typo, ...)
            # only count written in ALL CAPS — "sell NOW" isn't ServiceNow.
            if c.upper() not in upper_only or c == c.upper():
                tickers.add(c.upper())
                continue

        # Fuzzy fallback: whitelist only, never guessing from short/filler
        # words, and never for word-like tickers (know/snow must not -> NOW).
        if (
            _ALPHA_RE.match(c)
            and len(cl) >= 4
            and cl not in FUZZY_STOPWORDS
        ):
            for wt in whitelist:
                if wt in upper_only:
                    continue
                if damerau_levenshtein(cl, wt.lower()) <= parser_cfg.fuzzy_max_edit_distance:
                    fuzzy_tickers.add(wt)
                    break

    if not tickers and len(fuzzy_tickers) == 1:
        tickers = fuzzy_tickers

    spine_counts = (len(tickers), len(sides), len(strikes))
    if has_pct:
        complete = (
            spine_counts == (1, 1, 1)
            and len(prices) == 1
            and len(dates) <= 1
            and (dte is not None or dates)
        )
        if not complete:
            return ParseResult(ParseKind.NOISE, reason="status_pct", raw=block)
    if 0 in spine_counts:
        return ParseResult(ParseKind.NOISE, reason="no_spine", raw=block)
    if any(n > 1 for n in spine_counts) or len(prices) > 1 or len(dates) > 1:
        return ParseResult(
            ParseKind.AMBIGUOUS,
            reason=f"tickers={sorted(tickers)} sides={sorted(sides)} "
            f"strikes={sorted(strikes)} prices={sorted(prices)} dates={dates}",
            raw=block,
        )

    expiry = dte if dte is not None else (dates[0] if dates else "")
    alert = ParsedAlert(
        alerter_id=profile.alerter_id,
        ticker=next(iter(tickers)),
        strike=next(iter(strikes)),
        side=next(iter(sides)),
        expiry=expiry,
        alert_price=next(iter(prices)) if prices else None,
        tags=extract_tags(block, profile.known_tags),
        raw=block,
    )
    return ParseResult(ParseKind.ENTRY, alert=alert, raw=block)
