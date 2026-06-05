"""
database.py — SQLite database setup and connection management.
"""

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/store_intelligence.db")


def init_db():
    """Create all tables if they don't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
        -- Raw events table
        CREATE TABLE IF NOT EXISTS events (
            event_id        TEXT PRIMARY KEY,
            store_id        TEXT NOT NULL,
            camera_id       TEXT NOT NULL,
            visitor_id      TEXT NOT NULL,
            event_type      TEXT NOT NULL,
            timestamp       TEXT NOT NULL,
            zone_id         TEXT,
            dwell_ms        INTEGER DEFAULT 0,
            is_staff        INTEGER DEFAULT 0,
            confidence      REAL,
            queue_depth     INTEGER,
            sku_zone        TEXT,
            session_seq     INTEGER,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_events_store     ON events(store_id);
        CREATE INDEX IF NOT EXISTS idx_events_visitor   ON events(visitor_id);
        CREATE INDEX IF NOT EXISTS idx_events_type      ON events(event_type);
        CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_events_zone      ON events(zone_id);

        -- Visitor sessions (materialised per visitor per day)
        CREATE TABLE IF NOT EXISTS visitor_sessions (
            session_id      TEXT PRIMARY KEY,
            store_id        TEXT NOT NULL,
            visitor_id      TEXT NOT NULL,
            entry_ts        TEXT,
            exit_ts         TEXT,
            converted       INTEGER DEFAULT 0,
            total_dwell_ms  INTEGER DEFAULT 0,
            zones_visited   TEXT,   -- JSON array
            reached_billing INTEGER DEFAULT 0,
            abandoned_queue INTEGER DEFAULT 0,
            is_reentry      INTEGER DEFAULT 0,
            date            TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_store   ON visitor_sessions(store_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_date    ON visitor_sessions(date);
        CREATE INDEX IF NOT EXISTS idx_sessions_visitor ON visitor_sessions(visitor_id);

        -- POS transactions
        CREATE TABLE IF NOT EXISTS pos_transactions (
            transaction_id  TEXT PRIMARY KEY,
            store_id        TEXT NOT NULL,
            timestamp       TEXT NOT NULL,
            basket_value    REAL,
            matched_visitor TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_pos_store ON pos_transactions(store_id);
        CREATE INDEX IF NOT EXISTS idx_pos_ts    ON pos_transactions(timestamp);

        -- 7-day rolling baselines for anomaly detection
        CREATE TABLE IF NOT EXISTS daily_baselines (
            store_id            TEXT NOT NULL,
            date                TEXT NOT NULL,
            unique_visitors     INTEGER,
            conversion_rate     REAL,
            avg_dwell_seconds   REAL,
            PRIMARY KEY (store_id, date)
        );
        """)
    print(f"[DB] Initialised at {DB_PATH}")


@contextmanager
def get_conn():
    """Context manager for SQLite connections."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
