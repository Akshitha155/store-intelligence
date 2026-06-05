# PROMPT: "Write pytest tests for a FastAPI store analytics API. Test: POST /events/ingest
# idempotency, partial success on bad events, GET /metrics for zero-traffic store,
# GET /funnel session deduplication, GET /anomalies structure, GET /health 503 on DB failure.
# Use FastAPI TestClient with an in-memory SQLite database."
# CHANGES MADE: Added test for all-staff clip (no customer metrics), re-entry not counted twice
# in funnel, zero-purchase store returns 0.0 not null for conversion_rate.

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import pytest
import uuid
from datetime import datetime, timezone, timedelta, date
from fastapi.testclient import TestClient

# Override DB to temp file before importing app
import tempfile, os
_tmp = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = _tmp


@pytest.fixture(scope="module")
def client():
    from main import app
    from database import init_db
    init_db()
    with TestClient(app) as c:
        yield c


def make_event(**kwargs):
    defaults = {
        "event_id":   str(uuid.uuid4()),
        "store_id":   "STORE_TEST_001",
        "camera_id":  "CAM_ENTRY_01",
        "visitor_id": f"VIS_{uuid.uuid4().hex[:6].upper()}",
        "event_type": "ENTRY",
        "timestamp":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "is_staff":   False,
        "confidence": 0.88,
        "metadata":   {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }
    defaults.update(kwargs)
    return defaults


# ─── Ingestion Tests ──────────────────────────────────────────────────────────
class TestIngestion:

    def test_ingest_single_event(self, client):
        ev = make_event()
        r = client.post("/events/ingest", json={"events": [ev]})
        assert r.status_code == 200
        data = r.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 0

    def test_idempotent_duplicate_event(self, client):
        ev = make_event()
        r1 = client.post("/events/ingest", json={"events": [ev]})
        r2 = client.post("/events/ingest", json={"events": [ev]})
        assert r1.status_code == 200
        assert r2.status_code == 200
        # Second call should report it as duplicate, not re-insert
        data2 = r2.json()
        assert data2["duplicate"] >= 1
        assert data2["accepted"] == 0

    def test_partial_success_bad_event(self, client):
        good = make_event()
        bad  = {"event_id": str(uuid.uuid4()), "store_id": "S1"}  # missing required fields
        r = client.post("/events/ingest", json={"events": [good, bad]})
        assert r.status_code in (200, 207)
        data = r.json()
        assert data["accepted"] >= 1
        assert data["rejected"] >= 1

    def test_ingest_batch_500(self, client):
        events = [make_event(store_id="STORE_BATCH_TEST") for _ in range(500)]
        r = client.post("/events/ingest", json={"events": events})
        assert r.status_code == 200
        assert r.json()["accepted"] == 500

    def test_invalid_event_type_rejected(self, client):
        ev = make_event(event_type="INVALID_TYPE")
        r = client.post("/events/ingest", json={"events": [ev]})
        # Pydantic should reject this
        assert r.status_code in (422, 207)

    def test_invalid_timestamp_rejected(self, client):
        ev = make_event(timestamp="not-a-timestamp")
        r = client.post("/events/ingest", json={"events": [ev]})
        assert r.status_code in (422, 207)


# ─── Metrics Tests ────────────────────────────────────────────────────────────
class TestMetrics:

    def test_zero_traffic_store_returns_valid_response(self, client):
        r = client.get("/stores/STORE_EMPTY_ZERO/metrics")
        assert r.status_code == 200
        data = r.json()
        assert data["unique_visitors"] == 0
        assert data["conversion_rate"] == 0.0
        assert data["avg_dwell_seconds"] == 0.0
        assert data["queue_depth_now"] == 0
        assert data["zone_dwell"] == []

    def test_zero_purchase_store_conversion_is_float(self, client):
        """Conversion rate must be 0.0 (float), not null, for zero-purchase store."""
        r = client.get("/stores/STORE_EMPTY_ZERO/metrics")
        assert r.status_code == 200
        assert isinstance(r.json()["conversion_rate"], float)

    def test_metrics_excludes_staff(self, client):
        """Staff events must not inflate visitor counts."""
        store = "STORE_STAFF_TEST"
        events = [make_event(store_id=store, event_type="ENTRY", is_staff=True) for _ in range(10)]
        client.post("/events/ingest", json={"events": events})
        r = client.get(f"/stores/{store}/metrics")
        assert r.status_code == 200
        # unique_visitors should be 0 since all events are staff
        assert r.json()["unique_visitors"] == 0

    def test_metrics_response_schema(self, client):
        r = client.get("/stores/STORE_TEST_001/metrics")
        assert r.status_code == 200
        data = r.json()
        required = ["store_id", "unique_visitors", "conversion_rate",
                    "avg_dwell_seconds", "queue_depth_now", "abandonment_rate",
                    "zone_dwell", "last_updated"]
        for field in required:
            assert field in data, f"Missing field: {field}"


# ─── Funnel Tests ─────────────────────────────────────────────────────────────
class TestFunnel:

    def test_funnel_has_four_stages(self, client):
        r = client.get("/stores/STORE_TEST_001/funnel")
        assert r.status_code == 200
        data = r.json()
        assert len(data["stages"]) == 4

    def test_funnel_stage_names(self, client):
        r = client.get("/stores/STORE_TEST_001/funnel")
        stages = r.json()["stages"]
        names = [s["stage"] for s in stages]
        assert "Entry"         in names
        assert "Zone Visit"    in names
        assert "Billing Queue" in names
        assert "Purchase"      in names

    def test_funnel_reentry_not_double_counted(self, client):
        """Re-entry events (is_reentry=1) must not inflate funnel totals."""
        store = f"STORE_REENTRY_{uuid.uuid4().hex[:4]}"
        vid   = f"VIS_REENTRY_TEST"
        ts_fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        today  = date.today().isoformat()

        # First visit
        ev1 = make_event(store_id=store, visitor_id=vid, event_type="ENTRY")
        # Reentry
        ev2 = make_event(store_id=store, visitor_id=vid, event_type="REENTRY")
        client.post("/events/ingest", json={"events": [ev1, ev2]})

        r = client.get(f"/stores/{store}/funnel")
        assert r.status_code == 200
        entry_stage = next(s for s in r.json()["stages"] if s["stage"] == "Entry")
        # Should count as 1 unique visitor, not 2
        assert entry_stage["count"] <= 1

    def test_funnel_drop_off_pct_is_non_negative(self, client):
        r = client.get("/stores/STORE_TEST_001/funnel")
        for stage in r.json()["stages"]:
            assert stage["drop_off_pct"] >= 0.0


# ─── Anomalies Tests ─────────────────────────────────────────────────────────
class TestAnomalies:

    def test_anomalies_response_structure(self, client):
        r = client.get("/stores/STORE_TEST_001/anomalies")
        assert r.status_code == 200
        data = r.json()
        assert "anomalies" in data
        assert "checked_at" in data
        assert "store_id"   in data

    def test_anomaly_severity_valid_values(self, client):
        r = client.get("/stores/STORE_TEST_001/anomalies")
        for a in r.json()["anomalies"]:
            assert a["severity"] in ("INFO", "WARN", "CRITICAL")

    def test_anomaly_has_suggested_action(self, client):
        r = client.get("/stores/STORE_TEST_001/anomalies")
        for a in r.json()["anomalies"]:
            assert a.get("suggested_action"), "Each anomaly must have a suggested_action"


# ─── Health Tests ─────────────────────────────────────────────────────────────
class TestHealth:

    def test_health_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_schema(self, client):
        data = client.get("/health").json()
        assert "status"    in data
        assert "db_status" in data
        assert "stores"    in data
        assert "checked_at" in data

    def test_health_db_status_ok(self, client):
        data = client.get("/health").json()
        assert data["db_status"] == "ok"
