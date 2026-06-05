# DESIGN.md — Purplle Store Intelligence

## Architecture Overview

The system is a three-stage pipeline: **raw video → structured events → analytics API**.

```
CCTV Clips
  │
  ▼
┌─────────────────────────────────────────┐
│  Detection Layer  (pipeline/)           │
│  YOLOv8n → ByteTrack-style IoU tracker  │
│  Entry direction logic (Y-threshold)    │
│  Zone polygon mapping (ZoneMapper)      │
│  Re-ID: trajectory + exit memory        │
│  Staff: aspect ratio + speed heuristic  │
│  Output: events_output.jsonl            │
└────────────────┬────────────────────────┘
                 │ POST /events/ingest
                 ▼
┌─────────────────────────────────────────┐
│  Intelligence API  (app/)               │
│  FastAPI + SQLite (WAL mode)            │
│  Idempotent ingest (dedup by event_id)  │
│  Session materialisation on ingest      │
│  /metrics  /funnel  /heatmap            │
│  /anomalies  /health                    │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│  Live Dashboard  (dashboard/)           │
│  Pure HTML/JS, polls API every 5s       │
│  KPIs, funnel bars, zone heatmap,       │
│  anomaly list, activity feed            │
└─────────────────────────────────────────┘
```

### Detection Layer

**Frame processing:**  Every 3rd frame is processed (configurable via `PROCESS_EVERY_N`). This
gives ~10fps effective rate on 30fps footage — sufficient for entry counting while reducing GPU
load ~67%.

**Person detection:** YOLOv8n (nano) for CPU-friendly default; swap to `yolov8s.pt` for better
accuracy on crowded frames. Class 0 (person) only, confidence ≥ 0.35.

**Tracking:** Lightweight IoU + centroid distance matching. Each frame, every existing track gets
matched to the nearest detection using `0.5 × IoU + 0.5 × dist_score`. Unmatched tracks age out
after `MAX_DISAPPEARED` frames. This avoids requiring ByteTrack's Kalman filter dependency while
maintaining robust association for retail video (people move predictably in stores).

**Entry/exit direction:** Entry camera frames are split by a Y-threshold (top 35% = outside,
bottom 65% = inside). A track emits `ENTRY` when its trajectory crosses the threshold downward,
`EXIT` when crossing upward. This is robust to people pausing at the threshold.

**Re-entry detection:** When a track exits, its `visitor_id` + exit Y position is stored in a
rolling 5-minute window. New detections near the entry zone are checked against this list; if a
spatial match is found, the original `visitor_id` is re-used and a `REENTRY` event is emitted.

**Staff classification:** Heuristic combining bounding box aspect ratio (tall/narrow = uniform
+ apron) and average movement speed (staff move purposefully at high speed). Not perfect, but
catches the majority of staff without requiring uniform training data.

**Zone mapping:** `ZoneMapper` parses zone polygons from `store_layout.json` and uses a
ray-casting algorithm to map each person's centroid to a zone. Zones are defined in pixel
coordinates for each camera. The synthetic layout is generated from the Store 1 floor plan image.

### Intelligence API

**Storage:** SQLite with WAL journal mode. Chosen for simplicity and zero-ops deployment.
At 40 stores × ~500 events/day each, SQLite handles this comfortably. Switching to PostgreSQL
requires only changing the `get_conn()` function in `database.py`.

**Session materialisation:** On each ingest, `rebuild_sessions_for_store()` re-derives
`visitor_sessions` rows from raw events. This makes read endpoints fast (single table scan)
at the cost of write overhead — acceptable since ingest is batched, not row-by-row.

**Conversion rate:** POS transactions are correlated by time window: a visitor counts as
converted if any POS transaction occurred between their `entry_ts` and `exit_ts + 5 minutes`.
No customer ID required.

**Anomaly detection:** Four anomaly types — `BILLING_QUEUE_SPIKE`, `CONVERSION_DROP`,
`DEAD_ZONE`, `STALE_FEED`. Thresholds are constants in `anomalies.py`. The `CONVERSION_DROP`
anomaly compares today's rate against 7-day rolling baseline from `daily_baselines` table.

### Dashboard

Single-page HTML with no build step. Polls all API endpoints every 5 seconds. The live
activity feed synthesises events from metric changes (visitor count increase = new ENTRY event).

---

## AI-Assisted Decisions

### 1. Zone polygon vs bounding-box approach for zone mapping

I initially considered using simple bounding rectangles for zones (top-left corner + width/height)
from the floor plan image. When I described the problem to an LLM, it pointed out that retail
zones rarely align to grid rectangles — gondolas, curved counters, and diagonal shelves all
create non-rectangular zones. It suggested using polygon-based zones with ray-casting instead.

**I agreed and implemented it.** The `point_in_polygon()` function uses the ray-casting algorithm
which handles arbitrary polygon shapes. The floor plan polygons were estimated from the Store 1
layout image provided.

### 2. Session materialisation strategy

The LLM suggested two approaches: (a) compute sessions on-the-fly in each read endpoint using
window functions, or (b) materialise sessions to a separate table on write. It recommended (a)
for simplicity. After thinking about it, I went with (b) — materialise on ingest.

**I disagreed with the LLM's recommendation.** For a read-heavy analytics API where the same
session data powers 3+ endpoints (metrics, funnel, heatmap), materialisation makes read endpoints
significantly faster and simpler to implement. The ingest path can afford some extra latency
since it's called in batches, not per-event.

### 3. Staff exclusion without training data

The LLM suggested training a separate classifier on uniform images for staff detection.
I pointed out we have no labelled staff training data for these videos.

**I chose a heuristic approach** (aspect ratio + speed) as a pragmatic substitute.
This is documented in CHOICES.md. The LLM agreed this was the right call for a challenge context,
but flagged it as a production risk that would need proper training data in a real deployment.
