"""
heatmap.py — GET /stores/{store_id}/heatmap
Zone visit frequency + avg dwell, normalised 0–100.
"""

from datetime import date
from fastapi import APIRouter, Query
from models import HeatmapResponse, HeatmapZone
from database import get_conn

router = APIRouter(tags=["Analytics"])


@router.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
def get_heatmap(
    store_id: str,
    date_str: str = Query(default=None, alias="date"),
):
    target_date = date_str or date.today().isoformat()

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                zone_id,
                COUNT(DISTINCT visitor_id) AS visit_count,
                AVG(dwell_ms)              AS avg_dwell_ms
            FROM events
            WHERE store_id = ?
              AND is_staff  = 0
              AND zone_id   IS NOT NULL
              AND event_type IN ('ZONE_EXIT', 'ZONE_DWELL')
              AND substr(timestamp, 1, 10) = ?
            GROUP BY zone_id
            ORDER BY visit_count DESC
        """, (store_id, target_date)).fetchall()

        # Count total unique sessions for confidence flag
        session_row = conn.execute("""
            SELECT COUNT(*) as cnt FROM visitor_sessions
            WHERE store_id = ? AND date = ? AND is_reentry = 0
        """, (store_id, target_date)).fetchone()
        total_sessions = session_row["cnt"] if session_row else 0

    if not rows:
        return HeatmapResponse(store_id=store_id, date=target_date, zones=[])

    max_visits = max(r["visit_count"] for r in rows) or 1
    max_dwell  = max(r["avg_dwell_ms"] or 0 for r in rows) or 1

    zones = []
    for r in rows:
        # Composite heat score = 60% visit frequency + 40% dwell time
        visit_norm = (r["visit_count"] / max_visits) * 100
        dwell_norm = ((r["avg_dwell_ms"] or 0) / max_dwell) * 100
        heat_score = round(0.6 * visit_norm + 0.4 * dwell_norm, 1)

        zone_name = r["zone_id"].split("_Z_")[-1].replace("_", " ").title() \
                    if "_Z_" in r["zone_id"] else r["zone_id"]

        zones.append(HeatmapZone(
            zone_id=r["zone_id"],
            zone_name=zone_name,
            visit_count=r["visit_count"],
            avg_dwell_seconds=round((r["avg_dwell_ms"] or 0) / 1000, 1),
            heat_score=heat_score,
            data_confidence=total_sessions >= 20,
        ))

    return HeatmapResponse(store_id=store_id, date=target_date, zones=zones)
