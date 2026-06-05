"""
metrics.py — GET /stores/{store_id}/metrics
"""

import logging
from datetime import datetime, timezone, date
from fastapi import APIRouter, HTTPException, Query
from models import MetricsResponse, ZoneDwellMetric
from database import get_conn

logger = logging.getLogger("store_intelligence.metrics")
router = APIRouter(tags=["Analytics"])


@router.get("/stores/{store_id}/metrics", response_model=MetricsResponse)
def get_metrics(
    store_id: str,
    date_str: str = Query(default=None, alias="date", description="YYYY-MM-DD (default: today)"),
):
    """
    Real-time store metrics for today (or a given date).
    Excludes staff events. Handles zero-traffic stores gracefully.
    """
    target_date = date_str or date.today().isoformat()

    with get_conn() as conn:
        # ── Check store exists ──────────────────────────────────────
        store_check = conn.execute(
            "SELECT COUNT(*) as cnt FROM events WHERE store_id = ?", (store_id,)
        ).fetchone()
        if store_check["cnt"] == 0:
            # Return valid zero-traffic response
            return MetricsResponse(
                store_id=store_id,
                date=target_date,
                unique_visitors=0,
                total_entries=0,
                conversion_rate=0.0,
                avg_dwell_seconds=0.0,
                queue_depth_now=0,
                abandonment_rate=0.0,
                zone_dwell=[],
                data_window=target_date,
                last_updated=datetime.now(timezone.utc).isoformat(),
            )

        # ── Unique visitors (non-staff, no reentry double-count) ────
        uv_row = conn.execute("""
            SELECT COUNT(DISTINCT visitor_id) as cnt
            FROM visitor_sessions
            WHERE store_id = ? AND date = ? AND is_reentry = 0
        """, (store_id, target_date)).fetchone()
        unique_visitors = uv_row["cnt"] if uv_row else 0

        # ── Total entries ───────────────────────────────────────────
        entries_row = conn.execute("""
            SELECT COUNT(*) as cnt FROM events
            WHERE store_id = ? AND event_type = 'ENTRY' AND is_staff = 0
              AND substr(timestamp, 1, 10) = ?
        """, (store_id, target_date)).fetchone()
        total_entries = entries_row["cnt"] if entries_row else 0

        # ── Conversion rate ─────────────────────────────────────────
        conv_row = conn.execute("""
            SELECT
                COUNT(*) as total_sessions,
                SUM(converted) as converted_sessions
            FROM visitor_sessions
            WHERE store_id = ? AND date = ? AND is_reentry = 0
        """, (store_id, target_date)).fetchone()

        total_sessions     = conv_row["total_sessions"] or 0
        converted_sessions = conv_row["converted_sessions"] or 0
        conversion_rate    = (converted_sessions / total_sessions) if total_sessions > 0 else 0.0

        # ── Average dwell (seconds) ─────────────────────────────────
        dwell_row = conn.execute("""
            SELECT AVG(total_dwell_ms) as avg_ms
            FROM visitor_sessions
            WHERE store_id = ? AND date = ? AND is_reentry = 0
              AND total_dwell_ms > 0
        """, (store_id, target_date)).fetchone()
        avg_dwell_s = round((dwell_row["avg_ms"] or 0) / 1000, 1)

        # ── Queue depth (current, from most recent billing events) ──
        queue_row = conn.execute("""
            SELECT queue_depth FROM events
            WHERE store_id = ? AND event_type = 'BILLING_QUEUE_JOIN'
              AND queue_depth IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT 1
        """, (store_id,)).fetchone()
        queue_depth_now = queue_row["queue_depth"] if queue_row else 0

        # ── Abandonment rate ─────────────────────────────────────────
        abandon_row = conn.execute("""
            SELECT
                COUNT(*) as total_billing,
                SUM(abandoned_queue) as abandoned
            FROM visitor_sessions
            WHERE store_id = ? AND date = ? AND reached_billing = 1
        """, (store_id, target_date)).fetchone()
        total_billing = abandon_row["total_billing"] or 0
        total_abandoned = abandon_row["abandoned"] or 0
        abandonment_rate = (total_abandoned / total_billing) if total_billing > 0 else 0.0

        # ── Zone dwell breakdown ────────────────────────────────────
        zone_rows = conn.execute("""
            SELECT
                zone_id,
                COUNT(DISTINCT visitor_id)  as visit_count,
                AVG(dwell_ms)               as avg_dwell_ms,
                SUM(dwell_ms)               as total_dwell_ms
            FROM events
            WHERE store_id = ? AND is_staff = 0
              AND event_type IN ('ZONE_EXIT', 'ZONE_DWELL')
              AND zone_id IS NOT NULL
              AND substr(timestamp, 1, 10) = ?
            GROUP BY zone_id
            ORDER BY visit_count DESC
        """, (store_id, target_date)).fetchall()

        zone_dwell = [
            ZoneDwellMetric(
                zone_id=r["zone_id"],
                zone_name=r["zone_id"].split("_Z_")[-1].replace("_", " ").title()
                          if "_Z_" in r["zone_id"] else r["zone_id"],
                visit_count=r["visit_count"],
                avg_dwell_seconds=round((r["avg_dwell_ms"] or 0) / 1000, 1),
                total_dwell_seconds=round((r["total_dwell_ms"] or 0) / 1000, 1),
            )
            for r in zone_rows
        ]

    return MetricsResponse(
        store_id=store_id,
        date=target_date,
        unique_visitors=unique_visitors,
        total_entries=total_entries,
        conversion_rate=round(conversion_rate, 4),
        avg_dwell_seconds=avg_dwell_s,
        queue_depth_now=queue_depth_now,
        abandonment_rate=round(abandonment_rate, 4),
        zone_dwell=zone_dwell,
        data_window=target_date,
        last_updated=datetime.now(timezone.utc).isoformat(),
    )
