"""Streamlit dashboard over data/bot.db — READ-ONLY, safe to run while the
bot trades. Launch: tools\\dashboard.bat (opens http://localhost:8501).

Sections: live header, open positions with stops, equity curve, daily P&L,
per-tag and claimed-vs-realized stats, decision log, and the mined Bravo
track record as seed context until live data accumulates.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from execution.broker import OptionContract  # noqa: E402
from parsing.claims import extract_positive_pcts, mentions_contract  # noqa: E402
from settings import load_config  # noqa: E402

DB = Path(os.environ.get("COPYTRADE_DB", ROOT / "data" / "bot.db"))
BLUE = "#4C78A8"  # single hue for magnitude charts
GAIN, LOSS = "#2E7D32", "#C62828"  # polarity pair; tables always show the numbers

st.set_page_config(page_title="CopyTrade Bot", page_icon="🟢", layout="wide")


@st.cache_data(ttl=10)
def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    if not DB.exists():
        return pd.DataFrame()
    with sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True) as conn:
        return pd.read_sql_query(sql, conn, params=params)


@st.cache_data(ttl=60)
def bot_config():
    try:
        return load_config(ROOT / "config.yaml")
    except Exception:
        return None


def parse_key(key: str) -> dict | None:
    try:
        c = OptionContract.from_key(key)
        return {"ticker": c.ticker, "strike": c.strike, "side": c.side,
                "expiry": c.expiry.isoformat(), "label": str(c)}
    except Exception:
        return None


@st.cache_data(ttl=10)
def plays() -> pd.DataFrame:
    """One row per (contract, day): entry/exit averages and realized P&L,
    reconstructed from the orders+fills audit trail."""
    f = q("""SELECT f.qty, f.price, o.side AS order_side, o.contract, o.mode,
                    o.alerter_id, substr(o.created_at, 1, 10) AS day
             FROM fills f JOIN orders o ON f.order_id = o.id""")
    if f.empty:
        return pd.DataFrame()
    f["mode"] = f["mode"].replace("", "unmarked")
    f["cash"] = f.qty * f.price * 100.0 * f.order_side.map({"BTO": -1.0, "STC": 1.0})
    rows = []
    for (contract, day, alerter, mode), g in f.groupby(
            ["contract", "day", "alerter_id", "mode"]):
        bto, stc = g[g.order_side == "BTO"], g[g.order_side == "STC"]
        bought, sold = int(bto.qty.sum()), int(stc.qty.sum())
        meta = parse_key(contract) or {"label": contract, "ticker": "", "strike": 0.0,
                                       "side": "", "expiry": ""}
        entry = float((bto.qty * bto.price).sum() / bought) if bought else 0.0
        exit_ = float((stc.qty * stc.price).sum() / sold) if sold else 0.0
        rows.append({**meta, "contract": contract, "day": day, "alerter_id": alerter,
                     "mode": mode, "qty": bought, "entry": entry, "exit": exit_,
                     "closed": bought > 0 and bought == sold,
                     "realized_$": round(float(g.cash.sum()), 2),
                     "realized_%": round((exit_ / entry - 1) * 100, 1) if entry and sold else None})
    return pd.DataFrame(rows)


cfg = bot_config()
p = plays()
open_pos = q("""SELECT mode, contract, state, qty, avg_price, stop_mid, peak_mid,
                       opened_at FROM positions WHERE state != 'CLOSED' ORDER BY id""")
today = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")

st.title("CopyTrade Bot")
if not DB.exists():
    st.warning("data/bot.db not found — start the bot once to create it.")
    st.stop()

# Paper and live results never blend: everything below obeys this switch.
mode_pick = st.radio("results", ["live", "paper", "all"], horizontal=True,
                     label_visibility="collapsed")
if mode_pick != "all":
    if not p.empty:
        p = p[p["mode"] == mode_pick]
    if not open_pos.empty:
        open_pos = open_pos[open_pos["mode"].replace("", "unmarked") == mode_pick]
closed = p[p.closed] if not p.empty else pd.DataFrame()

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Bot mode", cfg.mode if cfg else "?")
c2.metric("Open positions", len(open_pos))
c3.metric("Entries today", 0 if p.empty else int((p.day == today).sum()))
c4.metric("Realized today ($)",
          f"{0 if closed.empty else closed[closed.day == today]['realized_$'].sum():+,.2f}")
c5.metric("Realized all-time ($)",
          f"{0 if closed.empty else closed['realized_$'].sum():+,.2f}")
st.caption("Data refreshes every ~10 s on interaction; press R to rerun. "
           "Read-only view of data/bot.db.")

# ---------------------------------------------------------------- open plays
st.subheader("Open positions")
if open_pos.empty:
    st.caption("none")
else:
    view = open_pos.copy()
    view["contract"] = view.contract.map(lambda k: (parse_key(k) or {}).get("label", k))
    view["stop distance"] = ((view.stop_mid / view.avg_price - 1) * 100).round(1).astype(str) + "%"
    st.dataframe(view, width="stretch", hide_index=True)
    st.caption("stop_mid is the bot-managed soft stop, updated every poll; "
               "peak_mid drives the runner trail.")

# ------------------------------------------------------------- equity curve
left, right = st.columns(2)
with left:
    st.subheader("Equity curve")
    if closed.empty:
        st.caption("no closed plays yet")
    else:
        daily = closed.groupby("day", as_index=False)["realized_$"].sum()
        daily["cumulative_$"] = daily["realized_$"].cumsum()
        line = (alt.Chart(daily).mark_line(color=BLUE, strokeWidth=2, point=True)
                .encode(x=alt.X("day:T", title=None),
                        y=alt.Y("cumulative_$:Q", title="cumulative realized $"),
                        tooltip=["day:T", alt.Tooltip("cumulative_$:Q", format="+,.2f"),
                                 alt.Tooltip("realized_$:Q", format="+,.2f")]))
        st.altair_chart(line, width="stretch")
with right:
    st.subheader("Daily P&L")
    if closed.empty:
        st.caption("no closed plays yet")
    else:
        daily = closed.groupby("day", as_index=False)["realized_$"].sum()
        bars = (alt.Chart(daily).mark_bar(cornerRadiusEnd=4)
                .encode(x=alt.X("day:T", title=None),
                        y=alt.Y("realized_$:Q", title="realized $/day"),
                        color=alt.condition(alt.datum["realized_$"] >= 0,
                                            alt.value(GAIN), alt.value(LOSS)),
                        tooltip=["day:T", alt.Tooltip("realized_$:Q", format="+,.2f")]))
        st.altair_chart(bars, width="stretch")

# ---------------------------------------------------------------- all plays
st.subheader("Closed plays")
if closed.empty:
    st.caption("none yet")
else:
    st.dataframe(
        closed.sort_values("day", ascending=False)
        [["day", "mode", "label", "alerter_id", "qty", "entry", "exit",
          "realized_$", "realized_%"]],
        width="stretch", hide_index=True)

# ------------------------------------------------- tags & claimed vs realized
alerts = q("""SELECT a.id, a.alerter_id, a.ticker, a.strike, a.side, a.tags, a.raw,
                     a.created_at, substr(a.created_at, 1, 10) AS day
              FROM alerts a JOIN decisions d ON d.alert_id = a.id
              WHERE d.action = 'enter'""")
scored = pd.DataFrame()
if not alerts.empty and not p.empty:
    scored = alerts.merge(p, on=["ticker", "strike", "side", "day", "alerter_id"], how="left")

tag_col, claim_col = st.columns(2)
with tag_col:
    st.subheader("Per-tag results")
    if scored.empty or not (scored.closed == True).any():  # noqa: E712
        st.caption("needs closed plays")
    else:
        t = scored[scored.closed == True].copy()  # noqa: E712
        t["tag"] = t.tags.str.split(";")
        t = t.explode("tag")
        t.loc[t.tag.isin(["", None]), "tag"] = "(untagged)"
        agg = (t.groupby("tag")
               .agg(plays=("id", "count"),
                    wins=("realized_$", lambda s: int((s > 0).sum())),
                    total_realized=("realized_$", "sum"),
                    avg_pct=("realized_%", "mean"))
               .reset_index().sort_values("total_realized", ascending=False))
        st.dataframe(agg.round(1), width="stretch", hide_index=True)

with claim_col:
    st.subheader("Claimed vs realized")
    if scored.empty or not (scored.closed == True).any() or cfg is None:  # noqa: E712
        st.caption("needs closed plays (claims are mined from the alerter's own "
                   "same-day %-messages in the audit log)")
    else:
        raw_msgs = q("SELECT alerter_id, author, text, created_at FROM raw_messages")
        rows = []
        for _, a in scored[scored.closed == True].iterrows():  # noqa: E712
            profile = cfg.alerters.get(a.alerter_id)
            if profile is None:
                continue
            same_day = raw_msgs[(raw_msgs.alerter_id == a.alerter_id)
                                & (raw_msgs.created_at >= a.created_at)
                                & (raw_msgs.created_at.str[:10] == a.day)]
            claimed = [pct for _, m in same_day.iterrows()
                       if mentions_contract(m.text, a.ticker, a.strike, profile)
                       for pct in extract_positive_pcts(m.text)]
            rows.append({"day": a.day, "play": a.label,
                         "claimed_max_%": max(claimed) if claimed else None,
                         "realized_%": a["realized_%"], "realized_$": a["realized_$"]})
        cvr = pd.DataFrame(rows)
        st.dataframe(cvr, width="stretch", hide_index=True)
        both = (cvr.dropna(subset=["claimed_max_%", "realized_%"])
                if not cvr.empty else cvr)
        if len(both) >= 3:
            sc = (alt.Chart(both).mark_point(size=90, filled=True, color=BLUE)
                  .encode(x=alt.X("claimed_max_%:Q", title="alerter claimed max %"),
                          y=alt.Y("realized_%:Q", title="bot realized %"),
                          tooltip=["day", "play", "claimed_max_%", "realized_%"]))
            rule = (alt.Chart(pd.DataFrame({"v": [0]})).mark_rule(color="#9E9E9E")
                    .encode(y="v:Q"))
            st.altair_chart(sc + rule, width="stretch")

# ------------------------------------------------------------- decision log
st.subheader("Decision log")
dec = q("""SELECT d.created_at, d.mode, d.alerter_id, d.action, d.reason,
                  COALESCE(a.raw, r.text, '') AS message
           FROM decisions d
           LEFT JOIN alerts a ON a.id = d.alert_id
           LEFT JOIN raw_messages r ON r.id = d.raw_message_id
           ORDER BY d.id DESC LIMIT 500""")
if not dec.empty:
    dec["mode"] = dec["mode"].replace("", "unmarked")
    if mode_pick != "all":
        dec = dec[dec["mode"] == mode_pick]
if dec.empty:
    st.caption(f"no {mode_pick} decisions recorded yet")
else:
    actions = st.multiselect("actions", sorted(dec.action.unique()),
                             default=[x for x in sorted(dec.action.unique()) if x != "noise"])
    view = dec[dec.action.isin(actions)].copy()
    view["message"] = view.message.str.slice(0, 160)
    st.dataframe(view, width="stretch", hide_index=True)
    st.caption("latest 500 decisions; noise (recaps, %-updates, chatter) hidden by default")

# ------------------------------------------------- historical seed context
with st.expander("Bravo mined track record (history fixtures — pre-live context)"):
    csv = ROOT / "tests" / "fixtures" / "bravo" / "entries.csv"
    if not csv.exists():
        st.caption("fixtures not found")
    else:
        hist = pd.read_csv(csv)
        h1, h2, h3 = st.columns(3)
        h1.metric("Mined entries", len(hist))
        h2.metric("With claimed %", int(hist.claimed_max_pct.notna().sum()))
        h3.metric("Median claimed max %", f"{hist.claimed_max_pct.median():.0f}%")
        histo = (alt.Chart(hist.dropna(subset=["claimed_max_pct"]))
                 .mark_bar(color=BLUE, cornerRadiusEnd=4)
                 .encode(x=alt.X("claimed_max_pct:Q", bin=alt.Bin(step=25),
                                 title="claimed max % per play"),
                         y=alt.Y("count()", title="plays"),
                         tooltip=["count()"]))
        st.altair_chart(histo, width="stretch")
        st.caption("Claims are the alerter's own peak-marked numbers mined from "
                   "chat history — context, not performance. The live "
                   "claimed-vs-realized table above is the honest comparison.")
