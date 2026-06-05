"""
anomalies.py — GET /stores/{store_id}/anomalies
Detects: queue spikes, conversion drops, dead zones, stale feeds.
"""

import uuid
import logging
from datetime import datetime, timezone, timedelta, date
from fastapi import APIRouter, Query
from models import AnomaliesResponse, Anomaly
from database import get_conn

logger = logging.getLogger("store_intelligence.anomalies")
router = APIRouter(tags=["Analytics"])

# Thresholds
QUEUE_SPIKE_THRESHOLD        = 5    # queue depth > 5 = WARN; > 8 = CRITICAL
CONVERSION_DROP_PCT          = 0.30 # 30% drop vs 7-day avg = WARN
DEAD_ZONE_MINUTES            = 30   # no visits in 30 min = INFO
STALE_FEED_MINUTES           = 10


@router.get("/stores/{store_id}/anomalies", response_model=AnomaliesResponse)
def get_anomalies(
    store_id: str,
    date_str: str = Query(default=None, alias="date"),
):
    now         = datetime.now(timezone.utc)
    target_date = date_str or date.today().isoformat()
    anomalies: list[Anomaly] = []

    with get_conn() as conn:

        # ── 1. QUEUE SPIKE ──────────────────────────────────────────
        queue_row = conn.execute("""
            SELECT queue_depth, timestamp FROM events
            WHERE store_id = ? AND event_type = 'BILLING_QUEUE_JOIN'
              AND queue_depth IS NOT NULL
            ORDER BY timestamp DESC LIMIT 1
        """, (store_id,)).fetchone()

        if queue_row and queue_row["queue_depth"] is not None:
            qdepth = queue_row["queue_depth"]
            if qdepth >= QUEUE_SPIKE_THRESHOLD:
                severity = "CRITICAL" if qdepth >= 8 else "WARN"
                anomalies.append(Anomaly(
                    anomaly_id=str(uuid.uuid4())[:8],
                    anomaly_type="BILLING_QUEUE_SPIKE",
                    severity=severity,
                    description=f"Billing queue depth is {qdepth} (threshold: {QUEUE_SPIKE_THRESHOLD})",
                    suggested_action="Open additional billing counter or redirect customers.",
                    detected_at=now.isoformat(),
                    zone_id=f"{store_id}_Z_BILLING",
                    value=float(qdepth),
                    threshold=float(QUEUE_SPIKE_THRESHOLD),
                ))

        # ── 2. CONVERSION DROP vs 7-day average ────────────────────
        today_conv = conn.execute("""
            SELECT AVG(CAST(converted AS FLOAT)) as rate
            FROM visitor_sessions
            WHERE store_id = ? AND date = ?
        """, (store_id, target_date)).fetchone()

        baseline_conv = conn.execute("""
            SELECT AVG(conversion_rate) as rate
            FROM daily_baselines
            WHERE store_id = ?
              AND date BETWEEN date(?, '-7 days') AND date(?, '-1 day')
        """, (store_id, target_date, target_date)).fetchone()

        today_rate    = today_conv["rate"]    if today_conv    else None
        baseline_rate = baseline_conv["rate"] if baseline_conv else None

        if today_rate is not None and baseline_rate is not None and baseline_rate > 0:
            drop = (baseline_rate - today_rate) / baseline_rate
            if drop >= CONVERSION_DROP_PCT:
                severity = "CRITICAL" if drop >= 0.5 else "WARN"
                anomalies.append(Anomaly(
                    anomaly_id=str(uuid.uuid4())[:8],
                    anomaly_type="CONVERSION_DROP",
                    severity=severity,
                    description=(
                        f"Conversion rate {today_rate:.1%} is {drop:.0%} below "
                        f"7-day average of {baseline_rate:.1%}"
                    ),
                    suggested_action=(
                        "Check for staff shortages, product availability issues, "
                        "or pricing anomalies."
                    ),
                    detected_at=now.isoformat(),
                    value=round(today_rate, 4),
                    threshold=round(baseline_rate * (1 - CONVERSION_DROP_PCT), 4),
                ))

        # ── 3. DEAD ZONES (no visits in 30 min) ────────────────────
        zone_rows = conn.execute("""
            SELECT zone_id, MAX(timestamp) as last_visit
            FROM events
            WHERE store_id = ? AND is_staff = 0
              AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')
              AND zone_id IS NOT NULL
            GROUP BY zone_id
        """, (store_id,)).fetchall()

        for zrow in zone_rows:
            if not zrow["last_visit"]:
                continue
            try:
                last_visit = datetime.fromisoformat(
                    zrow["last_visit"].replace("Z", "+00:00")
                )
                gap_minutes = (now - last_visit).total_seconds() / 60
                if gap_minutes >= DEAD_ZONE_MINUTES:
                    zone_label = zrow["zone_id"].split("_Z_")[-1].replace("_", " ").title() \
                                 if "_Z_" in zrow["zone_id"] else zrow["zone_id"]
                    anomalies.append(Anomaly(
                        anomaly_id=str(uuid.uuid4())[:8],
                        anomaly_type="DEAD_ZONE",
                        severity="INFO",
                        description=(
                            f"Zone '{zone_label}' has had no visitor activity "
                            f"for {gap_minutes:.0f} minutes."
                        ),
                        suggested_action=(
                            "Check zone display / stock levels. "
                            "Consider staff-driven customer engagement."
                        ),
                        detected_at=now.isoformat(),
                        zone_id=zrow["zone_id"],
                        value=round(gap_minutes, 1),
                        threshold=float(DEAD_ZONE_MINUTES),
                    ))
            except Exception:
                pass

        # ── 4. STALE FEED ───────────────────────────────────────────
        last_event = conn.execute("""
            SELECT MAX(timestamp) as ts FROM events WHERE store_id = ?
        """, (store_id,)).fetchone()

        if last_event and last_event["ts"]:
            try:
                last_ts = datetime.fromisoformat(last_event["ts"].replace("Z", "+00:00"))
                lag_min = (now - last_ts).total_seconds() / 60
                if lag_min >= STALE_FEED_MINUTES:
                    anomalies.append(Anomaly(
                        anomaly_id=str(uuid.uuid4())[:8],
                        anomaly_type="STALE_FEED",
                        severity="WARN",
                        description=f"No events received for {lag_min:.1f} minutes.",
                        suggested_action="Check camera connectivity and detection pipeline health.",
                        detected_at=now.isoformat(),
                        value=round(lag_min, 1),
                        threshold=float(STALE_FEED_MINUTES),
                    ))
            except Exception:
                pass

    return AnomaliesResponse(
        store_id=store_id,
        anomalies=anomalies,
        checked_at=now.isoformat(),
    )
