"""Audit DB: every event lands in SQLite with alerter_id and UTC created_at."""

from audit.db import AuditDB


def test_roundtrip(tmp_path):
    with AuditDB(tmp_path / "bot.db") as db:
        rm = db.record_raw_message("bravo", "f.txt:1", "Bravo Alerts#1561",
                                   "2026-06-03 06:40:00", "@alert iwm 289 put 0dte RISKY")
        alert = db.record_alert("bravo", rm, "IWM", 289.0, "PUT", "DTE0",
                                None, "risky", "@alert iwm 289 put 0dte RISKY")
        db.record_decision("bravo", "skip", "phase1-replay", alert_id=alert)

        tickers = db.conn.execute("SELECT ticker, side FROM alerts").fetchall()
        assert tickers == [("IWM", "PUT")]
        d = db.conn.execute("SELECT action, reason, alerter_id FROM decisions").fetchone()
        assert d == ("skip", "phase1-replay", "bravo")
        created = db.conn.execute("SELECT created_at FROM alerts").fetchone()[0]
        assert "+00:00" in created  # UTC


def test_raw_message_dedupe_by_source_id(tmp_path):
    with AuditDB(tmp_path / "bot.db") as db:
        a = db.record_raw_message("bravo", "n:1", "x", "", "hello")
        b = db.record_raw_message("bravo", "n:1", "x", "", "hello")
        assert a == b
        n = db.conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0]
        assert n == 1
