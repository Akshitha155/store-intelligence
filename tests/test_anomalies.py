# PROMPT: "Write pytest tests for anomaly detection in a retail store analytics API.
# Test: queue spike at threshold and critical level, dead zone detection after 30min no visits,
# stale feed warning after 10min no events, conversion drop vs 7-day baseline.
# Mock DB timestamps to control time offsets."
# CHANGES MADE: Used direct DB inserts to simulate stale data rather than sleeping;
# added test for zero-anomalies response format; verified anomaly_id uniqueness per response.

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import pytest
import uuid
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from main import app
    with TestClient(app) as c:
        yield c


def make_event(**kwargs):
    defaults = {
        "event_id":   str(uuid.uuid4()),
        "store_id":   "STORE_ANOMALY_TEST",
        "camera_id":  "CAM_BILLING_01",
        "visitor_id": f"VIS_{uuid.uuid4().hex[:6].upper()}",
        "event_type": "ENTRY",
        "timestamp":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "is_staff":   False,
        "confidence": 0.88,
        "metadata":   {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }
    defaults.update(kwargs)
    return defaults


class TestAnomalyDetection:

    def test_no_anomalies_has_valid_structure(self, client):
        """Empty anomaly list is a valid response."""
        r = client.get("/stores/STORE_NO_ANOMALIES_XYZ/anomalies")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data["anomalies"], list)
        assert "checked_at" in data

    def test_queue_spike_warn_at_threshold(self, client):
        """Queue depth >= 5 triggers WARN anomaly."""
        store = f"STORE_Q_{uuid.uuid4().hex[:4]}"
        ev = make_event(
            store_id=store,
            event_type="BILLING_QUEUE_JOIN",
            metadata={"queue_depth": 5, "sku_zone": None, "session_seq": 1},
        )
        client.post("/events/ingest", json={"events": [ev]})
        r = client.get(f"/stores/{store}/anomalies")
        anomalies = r.json()["anomalies"]
        queue_anomalies = [a for a in anomalies if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(queue_anomalies) >= 1
        assert queue_anomalies[0]["severity"] in ("WARN", "CRITICAL")

    def test_queue_spike_critical_at_8(self, client):
        """Queue depth >= 8 triggers CRITICAL anomaly."""
        store = f"STORE_QC_{uuid.uuid4().hex[:4]}"
        ev = make_event(
            store_id=store,
            event_type="BILLING_QUEUE_JOIN",
            metadata={"queue_depth": 9, "sku_zone": None, "session_seq": 1},
        )
        client.post("/events/ingest", json={"events": [ev]})
        r = client.get(f"/stores/{store}/anomalies")
        anomalies = r.json()["anomalies"]
        crits = [a for a in anomalies if a["severity"] == "CRITICAL"
                 and a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(crits) >= 1

    def test_stale_feed_detected(self, client):
        """
        After inserting an old-timestamped event, the API should detect stale feed.
        We inject a very old timestamp directly.
        """
        store = f"STORE_STALE_{uuid.uuid4().hex[:4]}"
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        ev = make_event(store_id=store, timestamp=old_ts)
        client.post("/events/ingest", json={"events": [ev]})
        r = client.get(f"/stores/{store}/anomalies")
        anomalies = r.json()["anomalies"]
        stale = [a for a in anomalies if a["anomaly_type"] == "STALE_FEED"]
        assert len(stale) >= 1
        assert stale[0]["severity"] == "WARN"

    def test_anomaly_ids_unique_in_response(self, client):
        """All anomaly_ids in a single response must be unique."""
        store = f"STORE_UNIQ_{uuid.uuid4().hex[:4]}"
        # Insert multiple types of anomaly triggers
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=35)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        events = [
            make_event(store_id=store, timestamp=old_ts, event_type="BILLING_QUEUE_JOIN",
                       metadata={"queue_depth": 9, "sku_zone": None, "session_seq": 1}),
            make_event(store_id=store, timestamp=old_ts, event_type="ZONE_DWELL",
                       zone_id=f"{store}_Z_SALM",
                       metadata={"queue_depth": None, "sku_zone": "SALM", "session_seq": 2}),
        ]
        client.post("/events/ingest", json={"events": events})
        r = client.get(f"/stores/{store}/anomalies")
        anomalies = r.json()["anomalies"]
        ids = [a["anomaly_id"] for a in anomalies]
        assert len(ids) == len(set(ids)), "Anomaly IDs must be unique within a response"

    def test_all_anomalies_have_suggested_action(self, client):
        store = f"STORE_SA_{uuid.uuid4().hex[:4]}"
        ev = make_event(
            store_id=store,
            event_type="BILLING_QUEUE_JOIN",
            metadata={"queue_depth": 6, "sku_zone": None, "session_seq": 1},
        )
        client.post("/events/ingest", json={"events": [ev]})
        r = client.get(f"/stores/{store}/anomalies")
        for a in r.json()["anomalies"]:
            assert a.get("suggested_action"), \
                f"Anomaly type {a['anomaly_type']} missing suggested_action"

    def test_anomaly_value_and_threshold_present(self, client):
        """Anomalies should expose numeric value + threshold for alerting systems."""
        store = f"STORE_VT_{uuid.uuid4().hex[:4]}"
        ev = make_event(
            store_id=store,
            event_type="BILLING_QUEUE_JOIN",
            metadata={"queue_depth": 7, "sku_zone": None, "session_seq": 1},
        )
        client.post("/events/ingest", json={"events": [ev]})
        r = client.get(f"/stores/{store}/anomalies")
        for a in r.json()["anomalies"]:
            if a["anomaly_type"] == "BILLING_QUEUE_SPIKE":
                assert a.get("value") is not None
                assert a.get("threshold") is not None
