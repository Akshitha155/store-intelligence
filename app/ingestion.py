"""
ingestion.py — POST /events/ingest endpoint.
Idempotent by event_id. Supports batches up to 500. Partial success.
"""

import json
import logging
from datetime import datetime, timezone
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from models   import IngestRequest, IngestResponse, StoreEvent
from database import get_conn
from sessions import rebuild_sessions_for_store

logger = logging.getLogger("store_intelligence.ingest")
router = APIRouter(tags=["Ingestion"])


@router.post("/events/ingest", response_model=IngestResponse)
def ingest_events(payload: IngestRequest):
    """
    Ingest a batch of store events (up to 500).
    - Idempotent: duplicate event_ids are silently skipped.
    - Partial success: malformed events are reported in 'errors', valid ones proceed.
    - Returns HTTP 207 if any events were rejected.
    """
    accepted  = 0
    rejected  = 0
    duplicate = 0
    errors    = []

    stores_updated: set[str] = set()

    with get_conn() as conn:
        for idx, event in enumerate(payload.events):
            try:
                # Check duplicate
                existing = conn.execute(
                    "SELECT 1 FROM events WHERE event_id = ?", (event.event_id,)
                ).fetchone()
                if existing:
                    duplicate += 1
                    continue

                conn.execute("""
                    INSERT INTO events
                        (event_id, store_id, camera_id, visitor_id, event_type,
                         timestamp, zone_id, dwell_ms, is_staff, confidence,
                         queue_depth, sku_zone, session_seq)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    event.event_id,
                    event.store_id,
                    event.camera_id,
                    event.visitor_id,
                    event.event_type,
                    event.timestamp,
                    event.zone_id,
                    event.dwell_ms,
                    int(event.is_staff),
                    event.confidence,
                    event.metadata.queue_depth,
                    event.metadata.sku_zone,
                    event.metadata.session_seq,
                ))
                accepted += 1
                stores_updated.add(event.store_id)

            except Exception as ex:
                rejected += 1
                errors.append({
                    "index":    idx,
                    "event_id": getattr(event, "event_id", "unknown"),
                    "error":    str(ex),
                })
                logger.warning(f'"Rejected event idx={idx}: {ex}"')

    # Rebuild session summaries for affected stores
    for store_id in stores_updated:
        try:
            rebuild_sessions_for_store(store_id)
        except Exception as ex:
            logger.error(f'"Session rebuild failed for {store_id}: {ex}"')

    logger.info(
        f'{{"action":"ingest","accepted":{accepted},"rejected":{rejected},'
        f'"duplicate":{duplicate},"stores":{list(stores_updated)}}}'
    )

    response = IngestResponse(
        accepted=accepted,
        rejected=rejected,
        duplicate=duplicate,
        errors=errors,
    )

    status_code = 207 if rejected > 0 else 200
    return JSONResponse(content=response.model_dump(), status_code=status_code)
