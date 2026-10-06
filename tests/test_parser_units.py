"""Unit coverage for the token-role parser's documented quirks."""

import pytest

from parsing.models import ParseKind
from parsing.parser import damerau_levenshtein, extract_tags, first_block, parse_message


def parse(text, bravo, cfg):
    return parse_message(text, bravo, cfg.parser)


class TestWordOrders:
    """The same spine in all six observed word orders."""

    @pytest.mark.parametrize(
        "text,ticker,strike,side",
        [
            ("@alert iwm 289 put 0dte RISKY", "IWM", 289.0, "PUT"),
            ("@alert iwm put 296 0dte risky", "IWM", 296.0, "PUT"),
            ("@alert 280 appl call 0dte RISKY", "AAPL", 280.0, "CALL"),
            ("@alert GAMBLE RISKY 245 call amazon 7/1", "AMZN", 245.0, "CALL"),
            ("@alert 387.5 call 0dte msft", "MSFT", 387.5, "CALL"),
            ("@alert qqq 720 0dte call GAMBLE", "QQQ", 720.0, "CALL"),
        ],
    )
    def test_any_order(self, text, ticker, strike, side, bravo, cfg):
        r = parse(text, bravo, cfg)
        assert r.kind is ParseKind.ENTRY
        assert (r.alert.ticker, r.alert.strike, r.alert.side) == (ticker, strike, side)


class TestTickers:
    @pytest.mark.parametrize(
        "word,ticker",
        [("tesla", "TSLA"), ("telsa", "TSLA"), ("aaple", "AAPL"), ("appl", "AAPL"),
         ("nvdia", "NVDA"), ("amazon", "AMZN"), ("Tesla", "TSLA")],
    )
    def test_aliases_and_typos(self, word, ticker, bravo, cfg):
        r = parse(f"@alert {word} 300 call 0dte", bravo, cfg)
        assert r.kind is ParseKind.ENTRY
        assert r.alert.ticker == ticker

    def test_two_tickers_fail_closed(self, bravo, cfg):
        r = parse("@alert spy iwm 300 call 0dte", bravo, cfg)
        assert r.kind is ParseKind.AMBIGUOUS

    def test_two_strikes_fail_closed(self, bravo, cfg):
        r = parse("@alert spy 300 310 call 0dte", bravo, cfg)
        assert r.kind is ParseKind.AMBIGUOUS

    def test_filler_never_fuzzy_matches(self, bravo, cfg):
        # "yall" is edit distance 2 from AAPL — must not become a ticker.
        r = parse("@alert yall ready ill be trading iwm today", bravo, cfg)
        assert r.kind is ParseKind.NOISE


class TestExpiry:
    def test_odte_typo(self, bravo, cfg):
        r = parse("@alert tesla 392.5 odte put RISKY", bravo, cfg)
        assert r.alert.expiry == "DTE0"

    def test_1dte(self, bravo, cfg):
        r = parse("@alert AMZN 265 CALL 1DTE GAMBLE 1.00", bravo, cfg)
        assert r.alert.expiry == "DTE1"
        assert r.alert.alert_price == 1.0

    def test_dated(self, bravo, cfg):
        r = parse("@alert GAMBLE RISKY 245 call amazon 7/1", bravo, cfg)
        assert r.alert.expiry == "DATE7/1"

    def test_no_expiry_stays_empty(self, bravo, cfg):
        r = parse("@alert iwm 287 call risky", bravo, cfg)
        assert r.alert.expiry == ""

    @pytest.mark.parametrize(
        "text",
        [
            "@alert spy $750 call 0dte 8/10 risk 0.70 fill",
            "@alert amzn 260 call RISKY level 9/10",
            "@alert apple 290 call 7/1 RISKY 0.72",  # date adjacent to "RISKY"
        ],
    )
    def test_risk_scales_are_not_dates(self, text, bravo, cfg):
        r = parse(text, bravo, cfg)
        assert r.kind is ParseKind.ENTRY
        assert not r.alert.expiry.startswith("DATE")


class TestPrices:
    def test_plain_decimal(self, bravo, cfg):
        r = parse("@alert spy 750 call 0dte risky 1.06", bravo, cfg)
        assert r.alert.alert_price == 1.06

    def test_at_price_counts_outside_formal_grammar_too(self, bravo, cfg):
        r = parse("@alert iwm 302 call 0dte risky @0.68", bravo, cfg)
        assert r.alert.alert_price == 0.68

    def test_dollar_strike(self, bravo, cfg):
        r = parse("@alert iwm $300 call 0dte risky", bravo, cfg)
        assert r.alert.strike == 300.0


class TestFormalGrammar:
    def test_bto_line(self, bravo, cfg):
        r = parse("BTO $SCHW 114c 08/21 @0.32", bravo, cfg)
        assert r.kind is ParseKind.ENTRY
        a = r.alert
        assert (a.ticker, a.strike, a.side, a.expiry, a.alert_price) == (
            "SCHW", 114.0, "CALL", "DATE8/21", 0.32
        )


class TestGuards:
    @pytest.mark.parametrize(
        "text",
        [
            "@alert 100% TP WHENEVER",
            "@alert spy 726 put hit 20%",
            "@alert iwm is back up for those that held",
            "@alert MSFT 385 CALL HIT 114%",
            "0.32 -> 0.64 easy double",
            "looking at tesla calls not in anything yet",
            "im gonna call it a day",
            "ill call out re entry if needed",
            "liking apple calls",
            "spy call back at breakeven",
        ],
    )
    def test_never_an_entry(self, text, bravo, cfg):
        assert parse(text, bravo, cfg).kind is ParseKind.NOISE

    def test_shit_does_not_trip_hit_guard(self, bravo, cfg):
        r = parse(
            "@alert aapl 325 call 0dte GAMBLE (win or lose not counting this shit)",
            bravo, cfg,
        )
        assert r.kind is ParseKind.ENTRY


class TestTags:
    def test_overlapping_vocab(self, bravo, cfg):
        r = parse("@alert spy 746 call 0dte very risky", bravo, cfg)
        assert r.alert.tags == ["risky", "very risky"]

    def test_mixed_case_phrase_is_not_a_tag(self, bravo, cfg):
        # "VERY risky" mid-sentence: only the lowercase "risky" counts.
        r = parse("@alert tesla 395 call 0dte VERY risky 0.89", bravo, cfg)
        assert r.alert.tags == ["risky"]

    def test_typo_tags_recorded_raw(self, bravo, cfg):
        assert parse("@alert tsla 347.5 call 0dte riksy 0.69", bravo, cfg).alert.tags == ["riksy"]
        assert parse("@alert iwm 296 call 0dte riskyy fuck it", bravo, cfg).alert.tags == [
            "risky", "riskyy"
        ]

    def test_reentry_typo_family(self, bravo, cfg):
        assert parse(
            "@alert msft 477.5 put 0dte (reentry please mange by yourself)", bravo, cfg
        ).alert.tags == ["reentry"]
        assert parse(
            "@alert 387.5 call 0dte msft (re ntry ENTER IF NEEDED HANDLE RISK YOURSELF)",
            bravo, cfg,
        ).alert.tags == ["re ntry"]

    def test_plural_is_not_a_typo_form(self, bravo, cfg):
        r = parse("@alert nvda 195 call 0dte (quick scalps) GAMBLE", bravo, cfg)
        assert r.alert.tags == ["gamble", "scalp"]

    def test_bare_risk_is_not_risky(self, bravo, cfg):
        r = parse("@alert amzn 230 call 0dte RISKY (take at your own risk)(", bravo, cfg)
        assert r.alert.tags == ["risky"]


class TestInvisibleChars:
    """Windows toast text wraps mentions in bidi isolates (U+2068/U+2069).

    Un-stripped, the isolate detaches into its own token, the leading-prefix
    strip misses, and the mid-message @alert truncation eats the whole
    message. Missed a live TSLA entry on 2026-08-20.
    """

    def test_leading_isolated_mention_is_still_an_entry(self, bravo, cfg):
        # Verbatim from raw_messages id 36 (the missed alert).
        r = parse("⁨@alert⁩\ntsla 330 put 0dte high risk 0.52 fill", bravo, cfg)
        assert r.kind is ParseKind.ENTRY
        a = r.alert
        assert (a.ticker, a.strike, a.side, a.expiry, a.alert_price) == (
            "TSLA", 330.0, "PUT", "DTE0", 0.52
        )

    def test_mid_message_isolated_mention_still_truncates(self, bravo, cfg):
        r = parse(
            "tsla 330 put 0dte 0.52 fill ⁨@alert⁩ end target 277",
            bravo, cfg,
        )
        assert r.kind is ParseKind.ENTRY
        assert r.alert.strike == 330.0

    def test_other_invisibles_stripped(self, bravo, cfg):
        r = parse("@alert ‎tsla‏ 330﻿ put⁠ 0dte", bravo, cfg)
        assert r.kind is ParseKind.ENTRY
        assert (r.alert.ticker, r.alert.strike) == ("TSLA", 330.0)


class TestHelpers:
    def test_first_block_joins_lines_until_blank(self):
        assert first_block("@alert\nMSFT 385 CALL 0DTE\nVERY RISKY") == (
            "@alert MSFT 385 CALL 0DTE VERY RISKY"
        )
        assert first_block("first para line\n\nsecond para") == "first para line"

    def test_damerau_transposition_counts_once(self):
        assert damerau_levenshtein("riksy", "risky") == 1
        assert damerau_levenshtein("renetry", "reentry") == 1

    def test_extract_tags_case_rule(self, bravo):
        assert extract_tags("VERY RISKY", bravo.known_tags) == ["risky", "very risky"]
        assert extract_tags("VERY risky", bravo.known_tags) == ["risky"]
