"""Synthetic alert corpus — the source of truth for every fixture.

No real chat content appears anywhere in tests/fixtures/. This module holds a
hand-authored corpus of invented messages, one per grammar feature the parser
is required to handle, plus a deterministic bulk generator that pads the
corpus out to a realistic channel volume.

The corpus is format-neutral: the renderers in _generate.py turn the same
messages into each of the three export shapes `ingestion.replay` understands.
Regenerate the fixtures and their golden CSVs with:

    python tests/fixtures/_generate.py

Authors are invented. ALPHA_AUTHOR / BRAVO_AUTHOR are the alerters;
BRAVO_OLD_AUTHOR preserves the "alerter renamed mid-history" case; the
OTHER_* authors are channel chatter that author_hints must exclude.

`expect` records the intended classification for each hand-authored message,
so tests/test_corpus_shape.py can assert the corpus still covers every
parser outcome after an edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

ALPHA_AUTHOR = "Alpha Alerts"
ALPHA_ALT_AUTHOR = "alpha_personal"      # lookalike account — excluded by hints
BRAVO_AUTHOR = "Bravo Alerts"
BRAVO_OLD_AUTHOR = "bravo_old"           # pre-rename handle, same alerter
OTHER_ADMIN = "ServerAdmin"
OTHER_MEMBER = "MemberOne"


@dataclass(frozen=True)
class Msg:
    ts: datetime
    author: str
    text: str
    note: str = ""          # why this message is in the corpus
    expect: str = "entry"   # entry | repeat | noise | ambiguous | excluded


def _dt(month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(2027, month, day, hour, minute)


# ---------------------------------------------------------------------------
# ALPHA — the formal grammars, delivered as a DiscordChatExporter HTML export.
# Mirrors an alerter who writes structured, separator-heavy alerts with
# profit targets and stop clauses.
# ---------------------------------------------------------------------------

ALPHA_CORE: list[Msg] = [
    Msg(_dt(3, 1, 6, 57), ALPHA_AUTHOR, "$SPY 612C 03/01 @0.60",
        "formal grammar, $-prefixed ticker, strike+side glued, @price"),
    Msg(_dt(3, 1, 10, 45), ALPHA_AUTHOR,
        "SPY 〡 614 Call〡 03/01 〡 0.50 Fill  @alert  Only looking for 20-30% on this",
        "separator grammar + profit-target % + trailing commentary; still an entry"),
    Msg(_dt(3, 2, 6, 34), ALPHA_AUTHOR, "QQQ〡 528 Call 〡 03/02 〡 0.85 Fill @alert",
        "separator glued to the ticker"),
    Msg(_dt(3, 2, 7, 57), ALPHA_AUTHOR, "QQQ 526C 03/02 .78 FILL 0.6 STOP @alert",
        "leading-dot price kept; the 0.6 STOP clause truncated off the spine"),
    Msg(_dt(3, 3, 6, 37), ALPHA_AUTHOR,
        "HIGH RISKY - SPY 〡 617 Call 〡 03/03 〡 .69  Fill @alert  "
        "This has run up abit already sorry",
        "tag written ALL-CAPS; leading-dot price"),
    Msg(_dt(3, 3, 8, 12), ALPHA_AUTHOR, "NOW 109C 03/05 @1.10 @alert",
        "uppercase-only ticker: ALL-CAPS NOW is ServiceNow"),
    Msg(_dt(3, 3, 9, 40), ALPHA_AUTHOR, "trimming here, sell now if you want out",
        "lowercase 'now' must never become a ticker", expect="noise"),
    Msg(_dt(3, 4, 7, 5), ALPHA_AUTHOR, "NOW 110C 03/12 @1.35 @alert swing",
        "second NOW strike, dated expiry"),
    Msg(_dt(3, 4, 11, 2), ALPHA_AUTHOR, "SPX 6150C 03/04 1.00 - 1.10 FILL @alert lotto",
        "fill range collapses to the lower bound"),
    Msg(_dt(3, 5, 6, 40), ALPHA_AUTHOR, "SPX 6075C 03/05 4.00-4.60 FILL @alert",
        "fill range, no spaces around the dash"),
    Msg(_dt(3, 5, 7, 30), ALPHA_AUTHOR,
        "NVDA 0.80 FILL 03/05 142C "
        "(typing this it just went to 0.96 wait for a pullback)",
        "the parenthetical aside's number must not become a second price"),
    Msg(_dt(3, 8, 6, 45), ALPHA_AUTHOR, "AAPL 03/08 248 Put 0.55 fill @alert small size",
        "word order: ticker, date, strike, side, price"),
    Msg(_dt(3, 8, 7, 15), ALPHA_AUTHOR, "put 03/08 TSLA 412.5 @0.90 @alert risky",
        "word order: side first, half-dollar strike"),
    Msg(_dt(3, 9, 6, 50), ALPHA_AUTHOR, "IWM 231C 0dte @0.44 @alert scalp",
        "0dte keyword instead of a date"),
    Msg(_dt(3, 9, 12, 10), ALPHA_AUTHOR, "META 1dte 705 call 1.20 fill @alert",
        "1dte keyword"),
    Msg(_dt(3, 10, 6, 41), ALPHA_AUTHOR,
        "AVGO 03/10 326C @1.05 @alert 8/10 risk on this one",
        "'8/10 risk' is a scale, not a date — risk-scale neighbor guard"),
    Msg(_dt(3, 10, 9, 20), ALPHA_AUTHOR, "SPY 03/10 620C -> 0.95 up 60%",
        "arrow status update", expect="noise"),
    Msg(_dt(3, 11, 6, 38), ALPHA_AUTHOR, "GOOGL 03/11 176C @0.72 @alert",
        "plain dated call"),
    Msg(_dt(3, 11, 7, 44), ALPHA_AUTHOR, "GOOGL 03/11 176C @0.72 @alert re entry",
        "same contract, same day -> flagged repeat, never traded", expect="repeat"),
    Msg(_dt(3, 11, 10, 15), ALPHA_AUTHOR, "GOOGL 176 call hit 85% so far",
        "'hit' status word; also the claim credited to the 176C", expect="noise"),
    Msg(_dt(3, 12, 6, 55), ALPHA_AUTHOR, "STO SPY 03/12 618P @1.20",
        "sell-to-open is not a follow-able entry", expect="noise"),
    Msg(_dt(3, 12, 7, 40), ALPHA_AUTHOR, "recap of the week: 9 greens 2 reds",
        "commentary phrase", expect="noise"),
    Msg(_dt(3, 15, 6, 48), ALPHA_AUTHOR, "looking at SPY 622C 03/15 around 0.70",
        "'looking at' is a watchlist note, not a fill", expect="noise"),
    Msg(_dt(3, 15, 7, 25), ALPHA_AUTHOR, "SPY 03/15 622C or 624C @0.70 @alert",
        "two strikes -> fail closed", expect="ambiguous"),
    Msg(_dt(3, 16, 6, 44), ALPHA_AUTHOR, "SPY or QQQ 03/16 620C @0.65 @alert",
        "two tickers -> fail closed", expect="ambiguous"),
    Msg(_dt(3, 16, 7, 50), ALPHA_AUTHOR, "MSFT 03/16 498C @0.88 @alert dca into this",
        "dca tag, still a fresh entry"),
    Msg(_dt(3, 17, 6, 39), ALPHA_AUTHOR, "AMZN 03/17 232P 0.62 fill @alert hedge",
        "hedge tag"),
    Msg(_dt(3, 17, 11, 30), ALPHA_AUTHOR, "AMZN 232 put up 120% now, trim",
        "claim message naming the contract -> claimed_max_pct", expect="noise"),
    Msg(_dt(3, 18, 6, 42), ALPHA_AUTHOR, "held the SPY 624C overnight, still green",
        "'held' status word", expect="noise"),
    Msg(_dt(3, 18, 8, 5), ALPHA_AUTHOR, "up 240% on the day overall, great session",
        "unnamed hype % must not be credited to any play", expect="noise"),
    Msg(_dt(3, 19, 6, 36), ALPHA_ALT_AUTHOR, "SPY 03/19 626C @0.80",
        "lookalike personal account excluded by author_hints", expect="excluded"),
    Msg(_dt(3, 19, 6, 58), OTHER_ADMIN, "reminder: alerts are not financial advice",
        "server admin chatter", expect="excluded"),
    Msg(_dt(3, 19, 7, 10), OTHER_MEMBER, "TSLA 03/19 410C @1.00 anyone in this?",
        "member guessing a play — must never enter the record", expect="excluded"),
    Msg(_dt(3, 19, 7, 35), ALPHA_AUTHOR,
        "TSLA 〡 415 Put 〡 03/19 〡 1.15 Fill @alert very risky",
        "separator grammar, put side, multi-word tag"),
    Msg(_dt(3, 22, 6, 47), ALPHA_AUTHOR, "SCHW 03/26 92C @0.35 @alert earnings play",
        "later-dated expiry"),
    Msg(_dt(3, 22, 9, 15), ALPHA_AUTHOR, "stop is if we break 618.00 on SPY",
        "stop-only message, no entry spine", expect="noise"),
]


# ---------------------------------------------------------------------------
# BRAVO — the loose, lowercase, typo-heavy grammar, delivered as a bracketed
# text export plus a copy-paste sample. Includes the alerter rename.
# ---------------------------------------------------------------------------

BRAVO_CORE: list[Msg] = [
    Msg(_dt(4, 1, 7, 0), BRAVO_OLD_AUTHOR, "@alert spy call 612 0dte risky",
        "pre-rename handle; word order ticker/side/strike"),
    Msg(_dt(4, 1, 9, 30), BRAVO_OLD_AUTHOR, "spy 612 call up 190% tp whenever",
        "claim naming the contract", expect="noise"),
    Msg(_dt(4, 2, 6, 37), BRAVO_OLD_AUTHOR,
        "@alert spy 616 call 0dte risky quick scalp probs",
        "two tags, one fuzzy"),
    Msg(_dt(4, 5, 7, 7), BRAVO_OLD_AUTHOR, "@alert 528 call qqq 0dte risky",
        "word order: strike, side, ticker"),
    Msg(_dt(4, 6, 6, 50), BRAVO_OLD_AUTHOR, "@alert telsa 410 put 0dte gamble",
        "ticker alias typo telsa -> TSLA"),
    Msg(_dt(4, 7, 6, 44), BRAVO_AUTHOR, "@alert aaple 246 call 0dte riksy",
        "alias typo aaple -> AAPL, tag typo riksy caught by the fuzzy pass"),
    Msg(_dt(4, 8, 6, 41), BRAVO_AUTHOR, "@alert nvdia 141 call 0dte go light",
        "alias typo nvdia -> NVDA"),
    Msg(_dt(4, 8, 7, 20), BRAVO_AUTHOR, "@alert nvidia 141 call 0dte",
        "same contract same day -> repeat", expect="repeat"),
    Msg(_dt(4, 9, 6, 36), BRAVO_AUTHOR, "@alert tesla 415 call 0dte very risky",
        "alias tesla -> TSLA"),
    Msg(_dt(4, 9, 11, 45), BRAVO_AUTHOR, "tesla 415 call 126% TP WHENEVER",
        "claim naming the contract", expect="noise"),
    Msg(_dt(4, 12, 6, 48), BRAVO_AUTHOR,
        "@alert googl 176.5 call 0dte go in with profits 0.80",
        "half-dollar strike, multi-word tag, trailing price"),
    Msg(_dt(4, 13, 6, 52), BRAVO_AUTHOR, "@alert qqq 530 call 0dte gamble",
        "plain gamble"),
    Msg(_dt(4, 14, 6, 39), BRAVO_AUTHOR, "@alert tsla 408 put 0dte risky very risky",
        "stacked risk tags"),
    Msg(_dt(4, 15, 6, 46), BRAVO_AUTHOR, "@alert spy 620 put 0dte risky super risky",
        "super risky tier"),
    Msg(_dt(4, 16, 6, 43), BRAVO_AUTHOR, "@alert iwm 232 call 0dte not gonna count",
        "not-counting tag still trades (trade_all_tags)"),
    Msg(_dt(4, 19, 6, 40), BRAVO_AUTHOR, "@alert msft 496 call 0dte lotto 0.45",
        "lotto with price"),
    Msg(_dt(4, 20, 6, 55), BRAVO_AUTHOR, "@alert amzn 230 call 0dte last play",
        "last play tag"),
    Msg(_dt(4, 21, 6, 38), BRAVO_AUTHOR, "@alert avgo 324 call 0dte fomo",
        "fomo tag"),
    Msg(_dt(4, 22, 6, 51), BRAVO_AUTHOR, "@alert meta 700 put 0dte scalp",
        "scalp tag"),
    Msg(_dt(4, 23, 6, 42), BRAVO_AUTHOR, "@alert SCHW 92 call 4/30 0.32",
        "explicit date instead of 0dte"),
    Msg(_dt(4, 26, 6, 49), BRAVO_AUTHOR,
        "@alert spy 618 call 0dte or maybe the 620, deciding",
        "two strikes -> fail closed", expect="ambiguous"),
    Msg(_dt(4, 27, 6, 44), BRAVO_AUTHOR,
        "cons close to breakeven, if you want out now you can",
        "pure management chatter, no spine", expect="noise"),
    Msg(_dt(4, 27, 7, 30), BRAVO_AUTHOR, "@alert 25% bang, taking some off",
        "bare % status, no spine", expect="noise"),
    Msg(_dt(4, 28, 6, 47), BRAVO_AUTHOR, "@alert spy 622 call 0dte 0.55 level 9/10 risk",
        "'level 9/10 risk' is a scale, not a date"),
    Msg(_dt(4, 29, 6, 53), BRAVO_OLD_AUTHOR, "@alert iwm 234 call 0dte",
        "old handle again late in the history — same alerter, still counted"),
    Msg(_dt(4, 30, 7, 5), OTHER_MEMBER, "@alert spy 624 call 0dte lets go",
        "member echo, excluded by author_hints", expect="excluded"),
    Msg(_dt(4, 30, 7, 25), OTHER_ADMIN, "welcome to the channel, read the pinned rules",
        "admin chatter", expect="excluded"),
]


# ---------------------------------------------------------------------------
# Bulk padding — routine alerts in rotating formats, deterministic.
# ---------------------------------------------------------------------------

_BULK_TICKERS = (
    "SPY", "QQQ", "IWM", "TSLA", "AAPL", "NVDA", "MSFT", "AMZN", "META", "GOOGL",
)
_BULK_BASE = {
    "SPY": 612.0, "QQQ": 528.0, "IWM": 231.0, "TSLA": 410.0, "AAPL": 246.0,
    "NVDA": 141.0, "MSFT": 496.0, "AMZN": 230.0, "META": 700.0, "GOOGL": 176.0,
}
_ALPHA_TAGS = ("", " risky", " lotto", " swing", " small size", " scalp", " high risk")
_BRAVO_TAGS = ("", " risky", " gamble", " scalp", " go light", " very risky", " lotto")

# Weekday-only slots, so the synthetic history looks like a real channel.
_ALPHA_DAYS = (
    (3, 23), (3, 24), (3, 25), (3, 26), (3, 29), (3, 30), (3, 31),
    (4, 1), (4, 2), (4, 5), (4, 6), (4, 7), (4, 8), (4, 9),
    (4, 12), (4, 13), (4, 14), (4, 15), (4, 16),
)
_BRAVO_DAYS = (
    (5, 3), (5, 4), (5, 5), (5, 6), (5, 7),
    (5, 10), (5, 11), (5, 12), (5, 13), (5, 14),
    (5, 17), (5, 18), (5, 19), (5, 20), (5, 21),
)


def _bulk_alpha() -> list[Msg]:
    """Two alerts per day in the alpha (formal, separator/strike-glued) style."""
    out: list[Msg] = []
    for i, (mo, day) in enumerate(_ALPHA_DAYS):
        for slot in range(2):
            n = i * 2 + slot
            tkr = _BULK_TICKERS[n % len(_BULK_TICKERS)]
            strike = _BULK_BASE[tkr] + 2.0 * ((n % 5) - 2)
            side = "Call" if n % 3 else "Put"
            price = 0.40 + 0.07 * (n % 9)
            tag = _ALPHA_TAGS[n % len(_ALPHA_TAGS)]
            hour, minute = (6, 45) if slot == 0 else (10, 20)
            strike_txt = f"{strike:g}"
            if n % 2:
                text = (f"{tkr} 〡 {strike_txt} {side} 〡 {mo:02d}/{day:02d} 〡 "
                        f"{price:.2f} Fill @alert{tag}")
            else:
                text = (f"${tkr} {strike_txt}{side[0]} {mo:02d}/{day:02d} "
                        f"@{price:.2f}{tag}")
            out.append(Msg(_dt(mo, day, hour, minute), ALPHA_AUTHOR, text, "bulk"))
    return out


def _bulk_bravo() -> list[Msg]:
    """Two alerts per day in the bravo (loose, lowercase, 0dte) style."""
    out: list[Msg] = []
    for i, (mo, day) in enumerate(_BRAVO_DAYS):
        for slot in range(2):
            n = i * 2 + slot
            tkr = _BULK_TICKERS[(n + 3) % len(_BULK_TICKERS)]
            strike = _BULK_BASE[tkr] + 2.0 * ((n % 7) - 3)
            side = "call" if n % 4 else "put"
            tag = _BRAVO_TAGS[n % len(_BRAVO_TAGS)]
            hour, minute = (6, 50) if slot == 0 else (11, 5)
            strike_txt = f"{strike:g}"
            if n % 3 == 0:
                text = f"@alert {tkr.lower()} {strike_txt} {side} 0dte{tag}"
            elif n % 3 == 1:
                text = f"@alert {strike_txt} {side} {tkr.lower()} 0dte{tag}"
            else:
                price = 0.35 + 0.06 * (n % 8)
                text = f"@alert {tkr.lower()} {strike_txt} {side} 0dte{tag} {price:.2f}"
            out.append(Msg(_dt(mo, day, hour, minute), BRAVO_AUTHOR, text, "bulk"))
    return out


def alpha_messages() -> list[Msg]:
    return sorted([*ALPHA_CORE, *_bulk_alpha()], key=lambda m: m.ts)


def bravo_messages() -> list[Msg]:
    return sorted([*BRAVO_CORE, *_bulk_bravo()], key=lambda m: m.ts)


# The copy-paste sample: one session, deliberately in the shape Discord's
# clipboard produces (APP headers, a time-only header, a pre-header tail).
BRAVO_PASTED_DAY = datetime(2027, 5, 24)
BRAVO_PASTED: list[Msg] = [
    Msg(_dt(5, 24, 6, 35), BRAVO_AUTHOR,
        "@alert\ngoogl 176.5 call 0dte go in with profits 0.80",
        "pre-header tail: inherits the first author"),
    Msg(_dt(5, 24, 7, 10), BRAVO_AUTHOR, "@alert qqq 530 call 0dte gamble",
        "APP-style header"),
    Msg(_dt(5, 24, 7, 48), BRAVO_AUTHOR, "@alert tsla 408 put 0dte risky very risky",
        "author-dash-date header"),
    Msg(_dt(5, 24, 8, 21), BRAVO_AUTHOR, "@alert tsla 406 put 0dte risky",
        "time-only header; the 140% claim below credits this play"),
    Msg(_dt(5, 24, 8, 35), BRAVO_AUTHOR, "100% TP WHENEVER\n126%\ntsla 406 put 140%",
        "claim lines; only the contract-naming one counts", expect="noise"),
    Msg(_dt(5, 24, 9, 2), BRAVO_AUTHOR, "@alert spy 624 put 0dte risky super risky",
        "stacked tags"),
    Msg(_dt(5, 24, 9, 40), BRAVO_AUTHOR, "@alert tsla 418 call 0dte riksy 0.69",
        "tag typo + price"),
    Msg(_dt(5, 24, 10, 15), BRAVO_AUTHOR,
        "@alert nvda 142.5 call 0dte not gonna count risky",
        "two tags"),
    Msg(_dt(5, 24, 11, 30), BRAVO_AUTHOR, "@alert SCHW 92 call 5/28 0.32",
        "formal-grammar footer, explicit date"),
]
