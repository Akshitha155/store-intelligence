"""
health.py — GET /health
Service status, last event per store, STALE_FEED warning.
"""

from datetime import datetime, timezone
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from models import HealthResponse, StoreHealthStatus
from database import get_conn, DB_PATH
import os

router = APIRouter(tags=["Health"])
STALE_THRESHOLD_MINUTES = 10


@router.get("/health", response_model=HealthResponse)
def health_check():
    now = datetime.now(timezone.utc)

    # Test DB connection
    db_status = "ok"
    stores_status: list[StoreHealthStatus] = []

    try:
        with get_conn() as conn:
            store_rows = conn.execute("""
                SELECT store_id, MAX(timestamp) as last_ts
                FROM events
                GROUP BY store_id
            """).fetchall()

            for row in store_rows:
                sid    = row["store_id"]
                last_ts = row["last_ts"]
                lag    = None
                status = "NO_DATA"

                if last_ts:
                    try:
                        t = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                        lag = round((now - t).total_seconds() / 60, 1)
                        status = "STALE_FEED" if lag >= STALE_THRESHOLD_MINUTES else "OK"
                    except Exception:
                        status = "NO_DATA"

                stores_status.append(StoreHealthStatus(
                    store_id=sid,
                    last_event_ts=last_ts,
                    lag_minutes=lag,
                    status=status,
                ))

    except Exception as ex:
        db_status = "unavailable"
        return JSONResponse(
            status_code=503,
            content={
                "status":    "degraded",
                "db_status": "unavailable",
                "error":     str(ex),
                "checked_at": now.isoformat(),
            },
        )

    overall = "healthy" if all(s.status == "OK" for s in stores_status) else "degraded"
    if not stores_status:
        overall = "healthy"  # No stores yet = fresh deployment

    return HealthResponse(
        status=overall,
        version="1.0.0",
        db_status=db_status,
        stores=stores_status,
        checked_at=now.isoformat(),
    )
