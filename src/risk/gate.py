"""Risk gate: gate(alert, ...) -> Decision, checks in fixed order, fail closed.

Any missing/stale quote, chain miss, or failed check = NO order, with a
reason that the control channel posts verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from execution.broker import BrokerAdapter, BrokerError, OptionContract, Quote
from parsing.models import ParsedAlert

DAILY_EXPIRY_TICKERS = {"SPY", "IWM", "QQQ"}  # everything else expires Fridays


@dataclass
class DayState:
    """Per-trading-day state the gate consults; the app owns and resets it."""

    day: date
    entries_today: int = 0
    played_keys: set[str] = field(default_factory=set)  # contract keys traded today
    halted: bool = False


@dataclass
class Decision:
    action: str  # "enter" | "skip"
    reason: str = ""
    contract: OptionContract | None = None
    qty: int = 0
    limit_price: float = 0.0
    tp_only: bool = False
    quote: Quote | None = None


def _next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def resolve_expiry(alert: ParsedAlert, today: date, max_dte: int,
                   assume_today: bool) -> date | None:
    """None means unresolvable (fail closed upstream)."""
    e = alert.expiry
    if e == "" and not assume_today:
        return None
    if e in ("", "DTE0"):
        return today
    if e == "DTE1":
        return _next_weekday(today)
    if e.startswith("DATE"):
        m, d = e[4:].split("/")
        expiry = date(today.year, int(m), int(d))
        if expiry < today:  # a dated alert from the past can only mean next year
            expiry = date(today.year + 1, int(m), int(d))
        return expiry
    return None


async def gate(
    alert: ParsedAlert,
    broker: BrokerAdapter,
    cfg: dict,
    state: DayState,
    now: datetime,
) -> Decision:
    entry = cfg["entry"]
    exits = cfg["exits"]

    if state.halted:
        return Decision("skip", "halted (kill switch) — no new entries until restart")

    # 1. Alert staleness.
    age = (now - alert.ts).total_seconds()
    if age > entry["staleness_seconds"]:
        return Decision("skip", f"stale alert: {age:.0f}s old > {entry['staleness_seconds']}s")

    # 2. Expiry resolution, then the contract MUST exist in the chain.
    expiry = resolve_expiry(
        alert, now.date(), entry["max_dte"], entry["assume_today_if_no_expiry"]
    )
    if expiry is None:
        return Decision("skip", f"cannot resolve expiry {alert.expiry!r}")
    dte = (expiry - now.date()).days
    if dte > entry["max_dte"]:
        return Decision("skip", f"expiry {expiry} is {dte} DTE > max_dte {entry['max_dte']}")
    contract = OptionContract(alert.ticker, alert.strike, alert.side, expiry)
    try:
        chain = await broker.get_chain(alert.ticker, expiry)
    except BrokerError:
        # No chain endpoint at this broker (Webull OpenAPI today): the quote
        # fetch below is the existence check — no quote, no order. Same
        # fail-closed guarantee by other means.
        chain = None
    if chain is not None and (alert.strike, alert.side) not in chain:
        hint = "" if alert.ticker in DAILY_EXPIRY_TICKERS else " (single names only expire Fridays)"
        return Decision(
            "skip", f"contract not in {alert.ticker} chain for {expiry}{hint}", contract
        )

    # 3. Quote: present and fresh.
    try:
        quote = await broker.get_option_quote(contract)
    except BrokerError as e:
        return Decision("skip", f"no quote: {e}", contract)
    quote_age = (now - quote.ts).total_seconds()
    if quote_age > entry["staleness_seconds"]:
        return Decision(
            "skip",
            f"stale quote: {quote_age:.0f}s old > {entry['staleness_seconds']}s",
            contract,
        )
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return Decision("skip", f"bad quote bid={quote.bid} ask={quote.ask}", contract)

    # 4. Spread sanity.
    if quote.spread_pct_of_mid > entry["max_spread_pct_of_mid"]:
        return Decision(
            "skip",
            f"spread {quote.spread_pct_of_mid:.1%} of mid > "
            f"{entry['max_spread_pct_of_mid']:.0%} "
            f"(bid {quote.bid:.2f} / ask {quote.ask:.2f})",
            contract,
        )

    # 5. Price band / priceless policy. Below the alert price is always fine.
    if alert.alert_price is not None:
        band = alert.alert_price * entry["price_band_max"]
        if quote.ask > band:
            return Decision(
                "skip",
                f"ask {quote.ask:.2f} > {band:.2f} "
                f"(alert {alert.alert_price:.2f} × band {entry['price_band_max']})",
                contract,
            )
        limit_price = min(quote.ask, band)
    else:
        if entry["priceless_alerts"] != "enter_at_ask":
            return Decision("skip", "priceless alert and policy is skip", contract)
        limit_price = quote.ask
    limit_price = round(limit_price, 2)

    # 6. Buying power → qty (single-contract fallback becomes a tp_only play).
    qty = entry["contracts_per_play"]
    tp_only = False
    bp = await broker.buying_power()
    cost = limit_price * 100 * qty
    if bp < cost:
        if entry["single_contract_fallback"] and bp >= limit_price * 100:
            qty, tp_only = 1, True
        else:
            return Decision(
                "skip",
                f"buying power {bp:.2f} < cost {cost:.2f} "
                f"({qty} contracts × {limit_price:.2f} × 100; "
                f"1 contract needs {limit_price * 100:.2f})",
                contract,
            )

    # 7. Daily entry count.
    if state.entries_today >= entry["max_entries_per_day"]:
        return Decision(
            "skip", f"max_entries_per_day {entry['max_entries_per_day']} reached", contract
        )

    # 8. Duplicate / same-day re-alert of the same contract: log, never trade.
    if entry["skip_reentries"] and contract.key in state.played_keys:
        return Decision(
            "skip",
            f"same-day re-alert of {contract} — the alerter's re-entries are on followers",
            contract,
        )

    # 9. Enough runway before the hard flatten.
    hh, mm = map(int, str(exits["flatten_time_local"]).split(":"))
    flatten_at = datetime.combine(now.date(), time(hh, mm), tzinfo=now.tzinfo)
    minutes_left = (flatten_at - now).total_seconds() / 60
    if minutes_left < entry["min_minutes_before_flatten"]:
        return Decision(
            "skip",
            f"{minutes_left:.0f} min before flatten < {entry['min_minutes_before_flatten']}",
            contract,
        )

    return Decision("enter", "", contract, qty, limit_price, tp_only, quote)
