"""
sessions.py — Materialise visitor sessions from raw events.
Sessions are rebuilt on each ingest so metrics/funnel endpoints stay fast.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from database import get_conn

logger = logging.getLogger("store_intelligence.sessions")

BILLING_CONVERSION_WINDOW_MINUTES = 5


def rebuild_sessions_for_store(store_id: str):
    """
    Rebuild visitor_sessions table for a given store from raw events.
    A session = all events for one visitor_id (entry → exit).
    Re-entries create separate sessions.
    """
    with get_conn() as conn:
        # Get all events for the store, ordered by visitor + time
        rows = conn.execute("""
            SELECT visitor_id, event_type, timestamp, zone_id, dwell_ms, is_staff
            FROM events
            WHERE store_id = ? AND is_staff = 0
            ORDER BY visitor_id, timestamp
        """, (store_id,)).fetchall()

        # Group events by visitor
        visitor_events: dict[str, list] = {}
        for row in rows:
            vid = row["visitor_id"]
            if vid not in visitor_events:
                visitor_events[vid] = []
            visitor_events[vid].append(dict(row))

        # Get POS transactions for this store
        pos_rows = conn.execute("""
            SELECT timestamp, basket_value FROM pos_transactions
            WHERE store_id = ?
            ORDER BY timestamp
        """, (store_id,)).fetchall()
        pos_times = [row["timestamp"] for row in pos_rows]

        # Delete existing sessions for store
        conn.execute("DELETE FROM visitor_sessions WHERE store_id = ?", (store_id,))

        sessions_built = 0
        for visitor_id, events in visitor_events.items():
            # Split into individual sessions at REENTRY events
            session_groups = _split_sessions(events)

            for session_events in session_groups:
                if not session_events:
                    continue

                session_id  = f"{visitor_id}_{sessions_built}"
                entry_ts    = None
                exit_ts     = None
                zones       = set()
                total_dwell = 0
                reached_billing = False
                abandoned   = False
                is_reentry  = False

                for ev in session_events:
                    etype = ev["event_type"]
                    ts    = ev["timestamp"]

                    if etype == "ENTRY":
                        entry_ts = ts
                    elif etype == "EXIT":
                        exit_ts = ts
                    elif etype == "REENTRY":
                        is_reentry = True
                        entry_ts   = ts
                    elif etype in ("ZONE_ENTER", "ZONE_DWELL") and ev.get("zone_id"):
                        zones.add(ev["zone_id"])
                    elif etype == "ZONE_EXIT":
                        total_dwell += ev.get("dwell_ms", 0)
                    elif etype == "BILLING_QUEUE_JOIN":
                        reached_billing = True
                    elif etype == "BILLING_QUEUE_ABANDON":
                        abandoned = True

                # POS correlation: was visitor at billing in 5 min before a transaction?
                converted = _check_conversion(entry_ts, exit_ts, pos_times)
                date_str  = (entry_ts or "")[:10]

                conn.execute("""
                    INSERT OR REPLACE INTO visitor_sessions
                        (session_id, store_id, visitor_id, entry_ts, exit_ts,
                         converted, total_dwell_ms, zones_visited,
                         reached_billing, abandoned_queue, is_reentry, date)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    session_id, store_id, visitor_id,
                    entry_ts, exit_ts,
                    int(converted),
                    total_dwell,
                    json.dumps(list(zones)),
                    int(reached_billing),
                    int(abandoned),
                    int(is_reentry),
                    date_str,
                ))
                sessions_built += 1

    logger.info(f'"Rebuilt {sessions_built} sessions for {store_id}"')


def _split_sessions(events: list[dict]) -> list[list[dict]]:
    """Split a visitor's event stream into sessions, breaking at REENTRY."""
    sessions = []
    current  = []
    for ev in events:
        if ev["event_type"] == "REENTRY" and current:
            sessions.append(current)
            current = [ev]
        else:
            current.append(ev)
    if current:
        sessions.append(current)
    return sessions


def _check_conversion(entry_ts: str, exit_ts: str, pos_times: list[str]) -> bool:
    """
    A visitor counts as converted if a POS transaction occurred within
    BILLING_CONVERSION_WINDOW_MINUTES after they were in the billing zone.
    Simplified: any POS transaction between entry and exit+5min.
    """
    if not entry_ts:
        return False
    try:
        t_entry = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
        t_exit  = (
            datetime.fromisoformat(exit_ts.replace("Z", "+00:00"))
            if exit_ts else t_entry + timedelta(hours=1)
        )
        t_window_end = t_exit + timedelta(minutes=BILLING_CONVERSION_WINDOW_MINUTES)
        for pos_ts in pos_times:
            t_pos = datetime.fromisoformat(pos_ts.replace("Z", "+00:00"))
            if t_entry <= t_pos <= t_window_end:
                return True
    except Exception:
        pass
    return False
