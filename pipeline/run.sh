#!/usr/bin/env bash
# pipeline/run.sh — Process all CCTV clips for a store and feed events into the API
# Usage: bash pipeline/run.sh [STORE_ID] [VIDEOS_DIR] [API_URL]

set -e

STORE_ID=${1:-"STORE_BLR_001"}
VIDEOS_DIR=${2:-"./videos"}
API_URL=${3:-"http://localhost:8000"}
OUTPUT_FILE="events_output.jsonl"
LAYOUT_FILE="store_layout.json"

echo "============================================"
echo "  Purplle Store Intelligence — Run Pipeline"
echo "  Store:     $STORE_ID"
echo "  Videos:    $VIDEOS_DIR"
echo "  API:       $API_URL"
echo "  Output:    $OUTPUT_FILE"
echo "============================================"

# Generate layout if it doesn't exist
if [ ! -f "$LAYOUT_FILE" ]; then
  echo "[INFO] store_layout.json not found — generating synthetic layout..."
  python3 pipeline/generate_layout.py --store_id "$STORE_ID" --output "$LAYOUT_FILE"
fi

# Run detection pipeline
echo ""
echo "[STEP 1] Running detection pipeline..."
python3 pipeline/detect.py \
  --store_id   "$STORE_ID" \
  --layout     "$LAYOUT_FILE" \
  --videos_dir "$VIDEOS_DIR" \
  --output     "$OUTPUT_FILE"

echo ""
echo "[STEP 2] Ingesting events into API..."
# Post all events in batches of 500 to the API
python3 - <<EOF
import json, requests, sys

BATCH_SIZE = 500
events = []
with open("$OUTPUT_FILE") as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except:
                pass

total = len(events)
print(f"  Loaded {total} events from $OUTPUT_FILE")

if total == 0:
    print("  No events to ingest.")
    sys.exit(0)

ingested = 0
for i in range(0, total, BATCH_SIZE):
    batch = events[i:i+BATCH_SIZE]
    try:
        r = requests.post(
            "$API_URL/events/ingest",
            json={"events": batch},
            timeout=30
        )
        if r.status_code in (200, 207):
            ingested += len(batch)
            print(f"  Batch {i//BATCH_SIZE + 1}: {len(batch)} events → HTTP {r.status_code}")
        else:
            print(f"  Batch {i//BATCH_SIZE + 1}: FAILED → HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"  Batch {i//BATCH_SIZE + 1}: ERROR → {e}")

print(f"\n  Done. {ingested}/{total} events ingested.")
EOF

echo ""
echo "[STEP 3] Checking metrics..."
python3 - <<EOF
import requests
try:
    r = requests.get("$API_URL/stores/$STORE_ID/metrics", timeout=10)
    print(f"  GET /stores/$STORE_ID/metrics → HTTP {r.status_code}")
    if r.ok:
        import json
        data = r.json()
        print(f"  Visitors:        {data.get('unique_visitors', 'N/A')}")
        print(f"  Conversion rate: {data.get('conversion_rate', 'N/A')}")
        print(f"  Avg dwell:       {data.get('avg_dwell_seconds', 'N/A')}s")
except Exception as e:
    print(f"  Could not reach API: {e}")
EOF

echo ""
echo "============================================"
echo "  Pipeline complete!"
echo "  Events file: $OUTPUT_FILE"
echo "  Dashboard:   $API_URL/dashboard"
echo "============================================"
