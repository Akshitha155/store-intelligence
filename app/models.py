"""
models.py — Pydantic event schema + response models.
"""

from __future__ import annotations
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Literal
from datetime import datetime
import uuid


EVENT_TYPES = Literal[
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
    "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY"
]

SEVERITY = Literal["INFO", "WARN", "CRITICAL"]


# ─────────────────────────────────────────────
# INBOUND EVENT
# ─────────────────────────────────────────────
class EventMetadata(BaseModel):
    queue_depth:  Optional[int]   = None
    sku_zone:     Optional[str]   = None
    session_seq:  Optional[int]   = None


class StoreEvent(BaseModel):
    event_id:    str  = Field(default_factory=lambda: str(uuid.uuid4()))
    store_id:    str
    camera_id:   str
    visitor_id:  str
    event_type:  EVENT_TYPES
    timestamp:   str
    zone_id:     Optional[str]  = None
    dwell_ms:    int            = 0
    is_staff:    bool           = False
    confidence:  float          = Field(ge=0.0, le=1.0, default=0.5)
    metadata:    EventMetadata  = Field(default_factory=EventMetadata)

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v):
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"Invalid ISO-8601 timestamp: {v}")
        return v

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, v):
        if not v or len(v) < 4:
            raise ValueError("event_id must be non-empty")
        return v


class IngestRequest(BaseModel):
    events: List[StoreEvent] = Field(..., max_length=500)


class IngestResponse(BaseModel):
    accepted:  int
    rejected:  int
    duplicate: int
    errors:    List[dict] = []


# ─────────────────────────────────────────────
# METRICS RESPONSE
# ─────────────────────────────────────────────
class ZoneDwellMetric(BaseModel):
    zone_id:          str
    zone_name:        str
    visit_count:      int
    avg_dwell_seconds: float
    total_dwell_seconds: float


class MetricsResponse(BaseModel):
    store_id:           str
    date:               str
    unique_visitors:    int
    total_entries:      int
    conversion_rate:    float   # 0.0 – 1.0
    avg_dwell_seconds:  float
    queue_depth_now:    int
    abandonment_rate:   float
    zone_dwell:         List[ZoneDwellMetric] = []
    data_window:        str     # e.g. "today" or "2026-03-08"
    last_updated:       str


# ─────────────────────────────────────────────
# FUNNEL RESPONSE
# ─────────────────────────────────────────────
class FunnelStage(BaseModel):
    stage:        str
    count:        int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    date:     str
    stages:   List[FunnelStage]
    note:     Optional[str] = None


# ─────────────────────────────────────────────
# HEATMAP RESPONSE
# ─────────────────────────────────────────────
class HeatmapZone(BaseModel):
    zone_id:            str
    zone_name:          str
    visit_count:        int
    avg_dwell_seconds:  float
    heat_score:         float   # 0–100 normalised
    data_confidence:    bool    # False if < 20 sessions


class HeatmapResponse(BaseModel):
    store_id: str
    date:     str
    zones:    List[HeatmapZone]


# ─────────────────────────────────────────────
# ANOMALY RESPONSE
# ─────────────────────────────────────────────
class Anomaly(BaseModel):
    anomaly_id:       str
    anomaly_type:     str
    severity:         SEVERITY
    description:      str
    suggested_action: str
    detected_at:      str
    zone_id:          Optional[str] = None
    value:            Optional[float] = None
    threshold:        Optional[float] = None


class AnomaliesResponse(BaseModel):
    store_id:  str
    anomalies: List[Anomaly]
    checked_at: str


# ─────────────────────────────────────────────
# HEALTH RESPONSE
# ─────────────────────────────────────────────
class StoreHealthStatus(BaseModel):
    store_id:          str
    last_event_ts:     Optional[str]
    lag_minutes:       Optional[float]
    status:            Literal["OK", "STALE_FEED", "NO_DATA"]


class HealthResponse(BaseModel):
    status:        Literal["healthy", "degraded"]
    version:       str
    db_status:     Literal["ok", "unavailable"]
    stores:        List[StoreHealthStatus]
    checked_at:    str
