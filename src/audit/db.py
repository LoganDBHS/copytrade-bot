"""Audit-first SQLite store. Every raw message, parse, decision, quote,
order, and fill is written as it happens. All tables carry alerter_id and
created_at (UTC ISO-8601)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY,
    alerter_id TEXT NOT NULL,
    source_id TEXT NOT NULL UNIQUE,
    author TEXT,
    ts_local TEXT,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    alerter_id TEXT NOT NULL,
    raw_message_id INTEGER REFERENCES raw_messages(id),
    ticker TEXT NOT NULL,
    strike REAL NOT NULL,
    side TEXT NOT NULL,
    expiry TEXT NOT NULL DEFAULT '',
    alert_price REAL,
    tags TEXT NOT NULL DEFAULT '',
    raw TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT '',
    alerter_id TEXT NOT NULL,
    alert_id INTEGER REFERENCES alerts(id),
    raw_message_id INTEGER REFERENCES raw_messages(id),
    action TEXT NOT NULL,          -- enter | skip | ambiguous | noise | repeat
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY,
    alerter_id TEXT NOT NULL,
    contract TEXT NOT NULL,
    bid REAL, ask REAL, mid REAL,
    quote_ts TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT '',
    alerter_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    contract TEXT NOT NULL,
    side TEXT NOT NULL,            -- BTO | STC
    qty INTEGER NOT NULL,
    limit_price REAL NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY,
    alerter_id TEXT NOT NULL,
    order_id INTEGER REFERENCES orders(id),
    qty INTEGER NOT NULL,
    price REAL NOT NULL,
    fill_ts TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT '',
    alerter_id TEXT NOT NULL,
    contract TEXT NOT NULL,
    state TEXT NOT NULL,           -- OPEN | TRIM | RUNNER | TP_ONLY | CLOSED
    qty INTEGER NOT NULL,
    avg_price REAL,
    stop_mid REAL,
    peak_mid REAL,
    disaster_order_id TEXT,
    opened_at TEXT,
    closed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerter_follow (
    alerter_id TEXT PRIMARY KEY,
    followed INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_summary (
    id INTEGER PRIMARY KEY,
    alerter_id TEXT NOT NULL,
    day TEXT NOT NULL,
    entries INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AuditDB:
    def __init__(self, path: str | Path = "data/bot.db", mode: str = "paper") -> None:
        # mode stamps decisions/orders/positions so paper and live results
        # never blend in reports. Defaults to paper — matches invariant 1.
        self.mode = mode
        p = Path(path)
        if p.parent and str(p.parent) not in (".", ""):
            p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(p)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        # migrate dbs created before position persistence existed
        for ddl in ("ALTER TABLE positions ADD COLUMN peak_mid REAL",
                    "ALTER TABLE positions ADD COLUMN disaster_order_id TEXT",
                    "ALTER TABLE decisions ADD COLUMN mode TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE orders ADD COLUMN mode TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE positions ADD COLUMN mode TEXT NOT NULL DEFAULT ''"):
            try:
                self.conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already there
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> AuditDB:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def record_raw_message(
        self, alerter_id: str, source_id: str, author: str, ts_local: str, text: str
    ) -> int:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO raw_messages"
            " (alerter_id, source_id, author, ts_local, text, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (alerter_id, source_id, author, ts_local, text, _now()),
        )
        self.conn.commit()
        if cur.lastrowid and cur.rowcount:
            return cur.lastrowid
        row = self.conn.execute(
            "SELECT id FROM raw_messages WHERE source_id=?", (source_id,)
        ).fetchone()
        return row[0]

    def record_alert(
        self,
        alerter_id: str,
        raw_message_id: int | None,
        ticker: str,
        strike: float,
        side: str,
        expiry: str,
        alert_price: float | None,
        tags: str,
        raw: str,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO alerts (alerter_id, raw_message_id, ticker, strike, side,"
            " expiry, alert_price, tags, raw, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (alerter_id, raw_message_id, ticker, strike, side, expiry,
             alert_price, tags, raw, _now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def record_position_open(
        self,
        alerter_id: str,
        contract_key: str,
        state: str,
        qty: int,
        avg_price: float,
        stop_mid: float,
        peak_mid: float,
        opened_at: str,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO positions (mode, alerter_id, contract, state, qty, avg_price,"
            " stop_mid, peak_mid, opened_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.mode, alerter_id, contract_key, state, qty, avg_price, stop_mid,
             peak_mid, opened_at, _now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_position(
        self,
        position_id: int,
        state: str,
        qty: int,
        stop_mid: float,
        peak_mid: float,
        disaster_order_id: str | None,
    ) -> None:
        closed_at = _now() if state == "CLOSED" else None
        self.conn.execute(
            "UPDATE positions SET state=?, qty=?, stop_mid=?, peak_mid=?,"
            " disaster_order_id=?, closed_at=COALESCE(closed_at, ?) WHERE id=?",
            (state, qty, stop_mid, peak_mid, disaster_order_id, closed_at, position_id),
        )
        self.conn.commit()

    def open_positions(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT id, alerter_id, contract, state, qty, avg_price, stop_mid,"
            " peak_mid, disaster_order_id, opened_at FROM positions"
            " WHERE state != 'CLOSED' ORDER BY id"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def follow_states(self) -> dict[str, bool]:
        """Persisted follow/unfollow choices — survive restarts and the
        Keeper task's auto-revive, so an unfollow can't silently undo."""
        rows = self.conn.execute(
            "SELECT alerter_id, followed FROM alerter_follow"
        ).fetchall()
        return {alerter_id: bool(f) for alerter_id, f in rows}

    def set_followed(self, alerter_id: str, followed: bool) -> None:
        self.conn.execute(
            "INSERT INTO alerter_follow (alerter_id, followed, updated_at)"
            " VALUES (?,?,?) ON CONFLICT(alerter_id) DO UPDATE SET"
            " followed=excluded.followed, updated_at=excluded.updated_at",
            (alerter_id, int(followed), _now()),
        )
        self.conn.commit()

    def record_decision(
        self,
        alerter_id: str,
        action: str,
        reason: str = "",
        alert_id: int | None = None,
        raw_message_id: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO decisions (mode, alerter_id, alert_id, raw_message_id, action,"
            " reason, created_at) VALUES (?,?,?,?,?,?,?)",
            (self.mode, alerter_id, alert_id, raw_message_id, action, reason, _now()),
        )
        self.conn.commit()
        return cur.lastrowid
