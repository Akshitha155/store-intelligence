"""
funnel.py — GET /stores/{store_id}/funnel
Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase
Session-level, no double-counting of re-entries.
"""

from datetime import date
from fastapi import APIRouter, Query
from models import FunnelResponse, FunnelStage
from database import get_conn

router = APIRouter(tags=["Analytics"])


@router.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
def get_funnel(
    store_id: str,
    date_str: str = Query(default=None, alias="date"),
):
    target_date = date_str or date.today().isoformat()

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                COUNT(*)                as total_sessions,
                SUM(CASE WHEN zones_visited != '[]' AND zones_visited != '' THEN 1 ELSE 0 END)
                                        as visited_zone,
                SUM(reached_billing)    as reached_billing,
                SUM(converted)          as purchased
            FROM visitor_sessions
            WHERE store_id = ? AND date = ? AND is_reentry = 0
        """, (store_id, target_date)).fetchone()

    total      = rows["total_sessions"]     or 0
    zone_visit = rows["visited_zone"]       or 0
    billing    = rows["reached_billing"]    or 0
    purchased  = rows["purchased"]          or 0

    def drop_pct(current: int, prior: int) -> float:
        if prior == 0:
            return 0.0
        return round((1 - current / prior) * 100, 1)

    stages = [
        FunnelStage(stage="Entry",         count=total,      drop_off_pct=0.0),
        FunnelStage(stage="Zone Visit",    count=zone_visit, drop_off_pct=drop_pct(zone_visit, total)),
        FunnelStage(stage="Billing Queue", count=billing,    drop_off_pct=drop_pct(billing, zone_visit)),
        FunnelStage(stage="Purchase",      count=purchased,  drop_off_pct=drop_pct(purchased, billing)),
    ]

    note = None
    if total == 0:
        note = "No visitor sessions recorded for this date."
    elif total < 20:
        note = f"Low session count ({total}). Funnel accuracy may be limited."

    return FunnelResponse(store_id=store_id, date=target_date, stages=stages, note=note)
