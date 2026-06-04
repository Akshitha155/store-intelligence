# Purplle Store Intelligence API

End-to-end store analytics: CCTV clips → structured events → real-time REST API → live dashboard.

---

## Quick Start (5 commands)

```bash
# 1. Clone and enter the project
git clone <your-repo-url> store-intelligence && cd store-intelligence

# 2. Start the API (Docker)
docker compose up --build -d

# 3. Install pipeline dependencies (Python 3.10+)
pip install -r pipeline/requirements.txt

# 4. Run detection pipeline on your video clips
bash pipeline/run.sh STORE_BLR_001 ./videos http://localhost:8000

# 5. Open the dashboard
open http://localhost:8000/dashboard
```

**API docs:** http://localhost:8000/docs  
**Health check:** http://localhost:8000/health

---

## Project Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py           # Main detection + tracking script
│   ├── tracker.py          # IoU + centroid tracker with Re-ID
│   ├── emit.py             # Event schema + JSONL emission
│   ├── zone_mapper.py      # Polygon zone mapping from layout JSON
│   ├── generate_layout.py  # Generates store_layout.json from floor plan
│   ├── run.sh              # One-command pipeline runner
│   └── requirements.txt    # Pipeline Python deps
├── app/
│   ├── main.py             # FastAPI entrypoint
│   ├── database.py         # SQLite + init
│   ├── models.py           # Pydantic schemas
│   ├── ingestion.py        # POST /events/ingest
│   ├── metrics.py          # GET /stores/{id}/metrics
│   ├── funnel.py           # GET /stores/{id}/funnel
│   ├── heatmap.py          # GET /stores/{id}/heatmap
│   ├── anomalies.py        # GET /stores/{id}/anomalies
│   ├── health.py           # GET /health
│   ├── sessions.py         # Session materialisation
│   └── requirements.txt    # API Python deps
├── tests/
│   ├── test_pipeline.py    # Detection pipeline unit tests
│   ├── test_metrics.py     # API endpoint tests
│   └── test_anomalies.py   # Anomaly detection tests
├── docs/
│   ├── DESIGN.md           # Architecture + AI-assisted decisions
│   └── CHOICES.md          # 3 key decisions with full reasoning
├── dashboard/
│   └── index.html          # Live web dashboard (auto-refresh)
├── docker-compose.yml
├── Dockerfile
└── README.md
```

---

## Running the Detection Pipeline

### Prerequisites

```bash
pip install -r pipeline/requirements.txt
```

YOLOv8 weights are downloaded automatically on first run (~6MB for nano model).

### Prepare your videos

Place your CCTV clips in a folder. File naming drives camera role detection:
- `*entry*` or `*cam3*` → entry/exit camera
- `*billing*` or `*cam5*` → billing camera  
- `*zone*` or `*cam1*`, `*cam2*` → floor zone cameras

```
videos/
├── CAM_3_-_entry.mp4
├── CAM_1_-_zone.mp4
├── CAM_2_-_zone.mp4
└── CAM_5_-_billing.mp4
```

### Generate layout (if you don't have store_layout.json)

```bash
python pipeline/generate_layout.py --store_id STORE_BLR_001 --output store_layout.json
```

### Run pipeline

```bash
# Basic usage
python pipeline/detect.py \
  --store_id STORE_BLR_001 \
  --layout store_layout.json \
  --videos_dir ./videos \
  --output events_output.jsonl

# With better model (higher accuracy)
python pipeline/detect.py --model yolov8s.pt ...

# With annotated video output (draws bounding boxes)
python pipeline/detect.py --visualize ...
```

Output: `events_output.jsonl` — one event per line.

### Ingest events into API

```bash
# After docker compose up
bash pipeline/run.sh STORE_BLR_001 ./videos http://localhost:8000
```

This runs detection AND ingests all events into the API in one step.

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/events/ingest` | Ingest batch of events (≤500). Idempotent by event_id. |
| `GET`  | `/stores/{id}/metrics` | Unique visitors, conversion rate, dwell, queue depth |
| `GET`  | `/stores/{id}/funnel` | Entry → Zone → Billing → Purchase with drop-off % |
| `GET`  | `/stores/{id}/heatmap` | Zone heat scores 0–100 |
| `GET`  | `/stores/{id}/anomalies` | Active anomalies with severity + suggested action |
| `GET`  | `/health` | Service status, per-store feed lag, STALE_FEED warning |
| `GET`  | `/dashboard` | Live web dashboard |
| `GET`  | `/docs` | Swagger UI |

### Quick test

```bash
# Ingest a sample event
curl -X POST http://localhost:8000/events/ingest \
  -H "Content-Type: application/json" \
  -d '{"events":[{"event_id":"test-001","store_id":"STORE_BLR_001","camera_id":"CAM_ENTRY_01","visitor_id":"VIS_ABC123","event_type":"ENTRY","timestamp":"2026-03-08T10:00:00.000Z","is_staff":false,"confidence":0.91,"metadata":{"queue_depth":null,"sku_zone":null,"session_seq":1}}]}'

# Get metrics
curl http://localhost:8000/stores/STORE_BLR_001/metrics | python3 -m json.tool

# Get health
curl http://localhost:8000/health
```

---

## Running Tests

```bash
cd store-intelligence

# Install test dependencies
pip install -r app/requirements.txt

# Run all tests with coverage
pytest tests/ -v --cov=app --cov-report=term-missing

# Run specific test file
pytest tests/test_metrics.py -v
```

Expected coverage: >70%.

---

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `DB_PATH` | `/data/store_intelligence.db` | SQLite database location |

---

## Loading POS Transactions

```bash
python3 - <<EOF
import csv, json, requests, uuid
from datetime import datetime

with open('POS_-_sample_transactions.csv') as f:
    reader = csv.DictReader(f)
    for row in reader:
        # Load via direct DB insert or extend ingestion.py to accept POS events
        pass
EOF
```

A simpler approach: place `pos_transactions.csv` in the data folder. The API reads it on startup
if `app/load_pos.py` is run manually:

```bash
docker exec store_intelligence_api python load_pos.py /data/pos_transactions.csv
```

---

## Live Dashboard (Part E)

The dashboard at `http://localhost:8000/dashboard` polls all API endpoints every 5 seconds
and displays:
- 4 live KPI cards (visitors, conversion, queue depth, avg dwell)
- Conversion funnel with animated bars
- Zone heatmap (colour-coded by visit frequency + dwell)
- Active anomalies feed
- Live event activity feed

To run the pipeline in real-time and watch metrics update live:

```bash
# Terminal 1: API running
docker compose up

# Terminal 2: Run pipeline (events stream to API in batches of 50)
python pipeline/detect.py --store_id STORE_BLR_001 --layout store_layout.json \
  --videos_dir ./videos --output /dev/null
# Note: set api_url in emit.py to stream live, or use run.sh
```

---

## Architecture Notes

- **No manual steps** beyond `git clone` + `docker compose up` for the API.
- **SQLite WAL mode** — zero-ops, sufficient for challenge scale.
- **Idempotent ingest** — safe to re-run pipeline; duplicates are counted but not re-inserted.
- **Graceful degradation** — all endpoints return valid empty responses for zero-traffic stores.
- **See DESIGN.md** for full architecture + AI-assisted decision log.
- **See CHOICES.md** for model selection, schema design, storage engine decisions.

