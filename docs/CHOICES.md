# CHOICES.md — Key Architectural Decisions

## Decision 1: Detection Model — YOLOv8n

### Options considered
- **YOLOv8n (nano)** — fastest inference, ~3ms/frame on GPU, ~30ms on CPU. Pre-trained on COCO
  which includes the `person` class with strong accuracy.
- **YOLOv8s (small)** — 2× model size, meaningfully better on partially occluded people.
- **RT-DETR** — transformer-based, better at crowded scenes but requires GPU and has higher
  latency (~15ms on GPU).
- **MediaPipe PersonDetection** — extremely fast on CPU but lower accuracy on small/distant people.

### What AI suggested
When I described the retail CCTV scenario (1080p, 15–30fps, partial occlusion, groups entering
together), an LLM suggested starting with YOLOv8s rather than nano, specifically because of the
partial occlusion edge case in the billing area. It noted that nano models lose significant accuracy
on partially obscured bounding boxes.

### What I chose and why
I chose **YOLOv8n as the default** but made the model path configurable via `--model` flag.
My reasoning:

1. **CPU compatibility** — The challenge says video must be processed on the candidate's machine.
   Not everyone has a GPU. YOLOv8n runs at ~5–8fps on a modern CPU, which is adequate since
   we only need to count entries/exits (not track 100 people in real-time).

2. **Swap path exists** — Running `--model yolov8s.pt` is a one-flag change. I documented this
   in the README.

3. **Frame sampling** — I process every 3rd frame (`PROCESS_EVERY_N = 3`), which means even
   a slow CPU keeps up with 30fps footage without dropping detections at the entry threshold.

If I were deploying to production with Jetson Orin hardware, I would use YOLOv8m with TensorRT
export for 30fps real-time inference.

### VLM usage
I evaluated using a VLM (GPT-4V style) for zone classification — prompting it to describe which
zone a person centroid falls in from a frame thumbnail. The prompt I tested was:
> "Here is a frame from a retail store. The person's centroid is at pixel (x, y) in a 1920×1080
> frame. Based on the store layout provided, which zone is this person in?"

**It did not work well for real-time use** — VLM latency (~1–2s/call) makes per-frame zone
classification impractical. I used it only for one-time zone coordinate estimation from the
layout image, which worked well.

---

## Decision 2: Event Schema Design

### Options considered
**Option A: Flat schema** — every event has all possible fields, NULLs where not applicable.
```json
{"event_type": "ZONE_DWELL", "zone_id": "...", "queue_depth": null, ...}
```

**Option B: Typed events** — separate schemas per event type, discriminated union.
```json
{"event_type": "ZONE_DWELL", "zone_payload": {"zone_id": "...", "dwell_ms": 8400}}
```

**Option C: Base + metadata dict** — shared base fields + open `metadata` dict for type-specific
fields (what the sample_events.jsonl shows Purplle expects).

### What AI suggested
The LLM suggested Option B (typed schemas) as the most type-safe approach, saying it would
prevent querying `queue_depth` on a `ZONE_DWELL` event.

### What I chose and why
I chose **Option C — base + metadata dict** because:

1. The `sample_events.jsonl` file in the challenge dataset clearly shows Purplle's own events use
   a flat/metadata-dict approach. Matching that schema reduces integration friction.

2. The API's SQL queries filter by `event_type` before accessing type-specific fields, so the
   type-safety benefit of Option B doesn't apply in practice.

3. Pydantic validation on `metadata` fields still catches type errors at ingest time.

The `metadata` dict contains `queue_depth`, `sku_zone`, and `session_seq`. Only events where
these are meaningful populate them; others send `null`.

---

## Decision 3: Storage Engine — SQLite with WAL

### Options considered
- **SQLite (WAL mode)** — zero-ops, embedded, single file, perfect for single-node deployment.
- **PostgreSQL** — production-grade, required for multi-node horizontal scale.
- **TimescaleDB** — PostgreSQL extension optimised for time-series; better for high-volume event
  streams.
- **DuckDB** — columnar, excellent for analytical queries (aggregations over large event tables).

### What AI suggested
The LLM recommended PostgreSQL as the "production-aware" choice, noting the challenge's
emphasis on production readiness. It flagged SQLite's write concurrency limitations
(one writer at a time) as a risk.

### What I chose and why
I chose **SQLite with WAL mode** for the following reasons:

1. **Challenge scope** — The challenge says "SQLite is fine" in the FAQ. 5 stores × ~500
   events/day = ~2,500 rows/day. SQLite handles millions of rows comfortably.

2. **WAL mode** removes the read-write lock contention issue. Readers don't block writers.
   With 2 uvicorn workers and batch ingest, this is sufficient.

3. **Zero-ops for reviewers** — `docker compose up` just works. No PostgreSQL init scripts,
   no password configuration, no volume mount complexity.

4. **Portability** — The database file path is configurable via `DB_PATH` env var. Swapping
   to PostgreSQL requires only replacing `database.py`'s `get_conn()` function with
   `psycopg2` or `asyncpg`. I documented this migration path in DESIGN.md.

**Where I agree with the AI's concern:** At 40 live stores with real-time event streaming
(~50 events/second sustained), SQLite's single-writer model would become a bottleneck. At that
scale, TimescaleDB or a dedicated event store (Kafka + PostgreSQL) would be the right choice.
