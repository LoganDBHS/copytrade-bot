"""Mine the alerter's own claimed-percentage messages from an exported history.

A claim only counts for a play when the message names that play's ticker (any
alias/typo form from the profile) or its strike. Unnamed hype numbers are
ignored — that is what keeps his 424% messages from being scored against a
play he only ever credited with 162%.
"""

from __future__ import annotations

import re

from settings import AlerterProfile

_PCT_RE = re.compile(
    r"([+\-−]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[+\-−]?\d+(?:\.\d+)?)\s*%"
)


def extract_positive_pcts(text: str) -> list[float]:
    out: list[float] = []
    for m in _PCT_RE.finditer(text):
        val = float(m.group(1).replace(",", "").replace("−", "-"))
        if val > 0:
            out.append(val)
    return out


def _strike_pattern(strike: float) -> re.Pattern:
    s = str(int(strike)) if strike == int(strike) else str(strike)
    # A trailing decimal still counts as naming the strike: he writes underlying
    # levels like "wall at 290.57" while the play is the 290 put.
    return re.compile(r"(?<![\d.])" + re.escape(s) + r"(?!\d)")


def mentions_contract(
    text: str, ticker: str, strike: float, profile: AlerterProfile
) -> bool:
    names = [ticker] + [k for k, v in profile.ticker_aliases.items() if v == ticker]
    for name in names:
        if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
            return True
    return bool(_strike_pattern(strike).search(text))
