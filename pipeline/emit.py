"""
emit.py — Event schema definition + emission to JSONL output file.
All events conform to the Purplle Store Intelligence schema from sample_events.jsonl.
"""

import uuid
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class EventEmitter:
    """
    Emits structured store events to a JSONL file and optionally
    posts them to the FastAPI ingest endpoint.
    """

    def __init__(self, output_file: str, store_id: str, api_url: Optional[str] = None):
        self.output_file = output_file
        self.store_id    = store_id
        self.api_url     = api_url
        self.event_count = 0
        self._batch: list[dict] = []
        self._fh = open(output_file, "w", encoding="utf-8")

    # ─────────────────────────────────────────
    # CORE EMIT
    # ─────────────────────────────────────────
    def _emit(self, event: dict):
        self._fh.write(json.dumps(event) + "\n")
        self._fh.flush()
        self.event_count += 1
        self._batch.append(event)
        # Auto-flush batch to API every 50 events
        if self.api_url and len(self._batch) >= 50:
            self._post_batch()

    def _ts(self, dt: datetime) -> str:
        """Format datetime to ISO-8601 UTC string."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _base_event(self, track, timestamp: datetime, event_type: str) -> dict:
        return {
            "event_id":      str(uuid.uuid4()),
            "store_id":      track.store_id,
            "camera_id":     track.camera_id,
            "visitor_id":    track.visitor_id,
            "event_type":    event_type,
            "timestamp":     self._ts(timestamp),
            "zone_id":       None,
            "dwell_ms":      0,
            "is_staff":      track.is_staff,
            "confidence":    round(track.conf, 4),
            "metadata": {
                "queue_depth":  None,
                "sku_zone":     None,
                "session_seq":  track.session_seq,
            }
        }

    # ─────────────────────────────────────────
    # EVENT TYPES
    # ─────────────────────────────────────────
    def emit_entry(self, track, timestamp: datetime):
        e = self._base_event(track, timestamp, "ENTRY")
        self._emit(e)

    def emit_exit(self, track, timestamp: datetime):
        e = self._base_event(track, timestamp, "EXIT")
        duration_ms = int(
            (timestamp - track.first_seen_ts).total_seconds() * 1000
        )
        e["dwell_ms"] = duration_ms
        self._emit(e)

    def emit_reentry(self, track, timestamp: datetime):
        e = self._base_event(track, timestamp, "REENTRY")
        self._emit(e)

    def emit_zone_enter(self, track, timestamp: datetime, zone: dict):
        e = self._base_event(track, timestamp, "ZONE_ENTER")
        e["zone_id"] = zone["zone_id"]
        e["metadata"]["sku_zone"] = zone.get("sku_zone", zone.get("zone_name"))
        self._emit(e)

    def emit_zone_exit(self, track, timestamp: datetime, zone_id: str):
        e = self._base_event(track, timestamp, "ZONE_EXIT")
        e["zone_id"] = zone_id
        # Compute dwell since zone entry
        if track.zone_enter_ts:
            dwell = int((timestamp - track.zone_enter_ts).total_seconds() * 1000)
            e["dwell_ms"] = max(0, dwell)
        self._emit(e)

    def emit_zone_dwell(self, track, timestamp: datetime, zone: dict, dwell_ms: int):
        e = self._base_event(track, timestamp, "ZONE_DWELL")
        e["zone_id"]  = zone["zone_id"]
        e["dwell_ms"] = dwell_ms
        e["metadata"]["sku_zone"] = zone.get("sku_zone", zone.get("zone_name"))
        self._emit(e)

    def emit_billing_queue_join(self, track, timestamp: datetime, queue_depth: int):
        e = self._base_event(track, timestamp, "BILLING_QUEUE_JOIN")
        e["zone_id"] = f"{track.store_id}_Z_BILLING"
        e["metadata"]["queue_depth"] = queue_depth
        self._emit(e)

    def emit_billing_queue_abandon(self, track, timestamp: datetime, queue_depth: int):
        e = self._base_event(track, timestamp, "BILLING_QUEUE_ABANDON")
        e["zone_id"] = f"{track.store_id}_Z_BILLING"
        e["metadata"]["queue_depth"] = queue_depth
        if track.billing_join_ts:
            e["dwell_ms"] = int((timestamp - track.billing_join_ts).total_seconds() * 1000)
        self._emit(e)

    # ─────────────────────────────────────────
    # API POST
    # ─────────────────────────────────────────
    def _post_batch(self):
        if not self.api_url or not self._batch:
            return
        try:
            import requests
            resp = requests.post(
                f"{self.api_url}/events/ingest",
                json={"events": self._batch},
                timeout=5,
            )
            if resp.status_code not in (200, 207):
                print(f"[WARN] API ingest returned {resp.status_code}")
        except Exception as ex:
            print(f"[WARN] Could not post to API: {ex}")
        self._batch.clear()

    def close(self):
        if self._batch and self.api_url:
            self._post_batch()
        self._fh.close()
