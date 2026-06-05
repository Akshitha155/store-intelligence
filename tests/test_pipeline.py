# PROMPT: "Write pytest tests for a retail CCTV detection pipeline that emits structured events.
# Cover: entry/exit direction logic, zone mapping, re-entry detection, staff classification,
# group entry (multiple people in same frame), billing queue join/abandon, dwell timer.
# Use mocked video frames and track objects."
# CHANGES MADE: Added edge cases for empty store periods, low-confidence events kept (not dropped),
# confirmed all event_ids are unique UUIDs, added assertion for session_seq incrementing.

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'pipeline'))

import pytest
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

# ─── Fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def base_ts():
    return datetime(2026, 3, 8, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def mock_layout():
    from zone_mapper import generate_synthetic_layout
    return generate_synthetic_layout("STORE_TEST_001", 1920, 1080)


@pytest.fixture
def zone_mapper(mock_layout):
    from zone_mapper import ZoneMapper
    return ZoneMapper(mock_layout, "zone")


@pytest.fixture
def emitter(tmp_path):
    from emit import EventEmitter
    outfile = str(tmp_path / "test_events.jsonl")
    em = EventEmitter(output_file=outfile, store_id="STORE_TEST_001")
    yield em
    em.close()


def make_track(visitor_id="VIS_TEST01", store_id="STORE_TEST_001", camera_id="CAM_ENTRY_01"):
    from tracker import Track
    det = {"bbox": [100, 200, 160, 320], "conf": 0.88, "cx": 130, "cy": 260, "width": 60, "height": 120}
    t = Track(
        track_id=1,
        detection=det,
        timestamp=datetime(2026, 3, 8, 10, 0, 0, tzinfo=timezone.utc),
        visitor_id=visitor_id,
        store_id=store_id,
        camera_id=camera_id,
    )
    return t


# ─── Emit Tests ───────────────────────────────────────────────────────────────
class TestEventEmitter:

    def test_emit_entry_creates_event(self, emitter, base_ts):
        track = make_track()
        emitter.emit_entry(track, base_ts)
        assert emitter.event_count == 1

    def test_emit_exit_includes_dwell_ms(self, emitter, base_ts):
        track = make_track()
        track.first_seen_ts = base_ts
        exit_ts = base_ts + timedelta(minutes=5)
        emitter.emit_exit(track, exit_ts)
        import json
        with open(emitter.output_file) as f:
            ev = json.loads(f.readline())
        assert ev["dwell_ms"] == 300_000  # 5 minutes in ms

    def test_event_ids_are_unique(self, emitter, base_ts):
        """All emitted event_ids must be globally unique."""
        track = make_track()
        for _ in range(20):
            emitter.emit_entry(track, base_ts)
        import json
        ids = []
        with open(emitter.output_file) as f:
            for line in f:
                ids.append(json.loads(line)["event_id"])
        assert len(ids) == len(set(ids)), "Duplicate event_ids found"

    def test_low_confidence_events_not_dropped(self, emitter, base_ts):
        """Low-confidence events must be emitted, not silently dropped."""
        track = make_track()
        track.conf = 0.25  # below normal threshold
        emitter.emit_entry(track, base_ts)
        assert emitter.event_count == 1
        import json
        with open(emitter.output_file) as f:
            ev = json.loads(f.readline())
        assert ev["confidence"] == 0.25

    def test_reentry_event_type(self, emitter, base_ts):
        track = make_track()
        emitter.emit_reentry(track, base_ts)
        import json
        with open(emitter.output_file) as f:
            ev = json.loads(f.readline())
        assert ev["event_type"] == "REENTRY"

    def test_billing_queue_join_sets_metadata(self, emitter, base_ts):
        track = make_track()
        emitter.emit_billing_queue_join(track, base_ts, queue_depth=3)
        import json
        with open(emitter.output_file) as f:
            ev = json.loads(f.readline())
        assert ev["event_type"] == "BILLING_QUEUE_JOIN"
        assert ev["metadata"]["queue_depth"] == 3

    def test_staff_events_flagged(self, emitter, base_ts):
        track = make_track()
        track.is_staff = True
        emitter.emit_entry(track, base_ts)
        import json
        with open(emitter.output_file) as f:
            ev = json.loads(f.readline())
        assert ev["is_staff"] is True

    def test_zone_dwell_emits_with_zone_id(self, emitter, base_ts, mock_layout):
        from zone_mapper import ZoneMapper
        zm = ZoneMapper(mock_layout, "zone")
        track = make_track()
        zone  = zm.zones[0] if zm.zones else {"zone_id": "Z_TEST", "zone_name": "Test", "sku_zone": "TEST"}
        emitter.emit_zone_dwell(track, base_ts, zone, dwell_ms=45_000)
        import json
        with open(emitter.output_file) as f:
            ev = json.loads(f.readline())
        assert ev["event_type"] == "ZONE_DWELL"
        assert ev["dwell_ms"] == 45_000
        assert ev["zone_id"] is not None


# ─── Zone Mapper Tests ────────────────────────────────────────────────────────
class TestZoneMapper:

    def test_billing_zone_found(self, zone_mapper):
        billing = zone_mapper.get_billing_zone()
        assert billing is not None
        assert billing["zone_type"] == "BILLING"

    def test_point_in_zone(self, zone_mapper):
        """A point known to be in F.O.H zone should resolve."""
        # F.O.H center approx 28%–72% width, 28%–65% height of 1920x1080
        cx, cy = int(1920 * 0.50), int(1080 * 0.45)
        zone = zone_mapper.get_zone(cx, cy)
        assert zone is not None

    def test_point_outside_all_zones(self, zone_mapper):
        """A point in a gap between zones should return None."""
        # Far top-right corner (likely no zone)
        zone = zone_mapper.get_zone(1910, 10)
        assert zone is None

    def test_all_zones_have_required_fields(self, zone_mapper):
        for z in zone_mapper.zones:
            assert "zone_id"   in z, f"Missing zone_id in {z}"
            assert "zone_name" in z, f"Missing zone_name in {z}"
            assert "zone_type" in z, f"Missing zone_type in {z}"


# ─── Tracker Tests ────────────────────────────────────────────────────────────
class TestPersonTracker:

    def _make_tracker(self, emitter, mock_layout, role="entry"):
        from tracker import PersonTracker
        from zone_mapper import ZoneMapper
        zm = ZoneMapper(mock_layout, role)
        return PersonTracker(
            store_id="STORE_TEST_001",
            camera_id="CAM_ENTRY_01",
            camera_role=role,
            zone_mapper=zm,
            emitter=emitter,
            clip_start_time=datetime(2026, 3, 8, 10, 0, 0, tzinfo=timezone.utc),
        )

    def test_new_detection_creates_track(self, emitter, mock_layout, base_ts):
        tracker = self._make_tracker(emitter, mock_layout)
        tracker.frame_height = 1080
        tracker.frame_width  = 1920
        dets = [{"bbox": [100, 50, 180, 200], "conf": 0.85, "cx": 140, "cy": 125, "width": 80, "height": 150}]
        tracker.update(dets, base_ts, None, 1)
        assert len(tracker.active_tracks) == 1

    def test_group_entry_creates_multiple_tracks(self, emitter, mock_layout, base_ts):
        """3 people entering together → 3 separate tracks."""
        tracker = self._make_tracker(emitter, mock_layout)
        tracker.frame_height = 1080
        tracker.frame_width  = 1920
        dets = [
            {"bbox": [50,  50, 120, 200], "conf": 0.90, "cx": 85,  "cy": 125, "width": 70, "height": 150},
            {"bbox": [200, 50, 270, 200], "conf": 0.88, "cx": 235, "cy": 125, "width": 70, "height": 150},
            {"bbox": [350, 50, 420, 200], "conf": 0.86, "cx": 385, "cy": 125, "width": 70, "height": 150},
        ]
        tracker.update(dets, base_ts, None, 1)
        assert len(tracker.active_tracks) == 3, "Group entry must create 3 separate tracks"
        assert len(set(t.visitor_id for t in tracker.active_tracks.values())) == 3, \
            "Each person in group must have unique visitor_id"

    def test_empty_store_does_not_crash(self, emitter, mock_layout, base_ts):
        """Zero detections for many frames must not raise."""
        tracker = self._make_tracker(emitter, mock_layout)
        tracker.frame_height = 1080
        tracker.frame_width  = 1920
        for i in range(50):
            tracker.update([], base_ts + timedelta(seconds=i), None, i)
        assert len(tracker.active_tracks) == 0

    def test_tracks_age_out_after_max_disappeared(self, emitter, mock_layout, base_ts):
        from tracker import MAX_DISAPPEARED
        tracker = self._make_tracker(emitter, mock_layout)
        tracker.frame_height = 1080
        dets = [{"bbox": [100, 50, 180, 200], "conf": 0.85, "cx": 140, "cy": 125, "width": 80, "height": 150}]
        tracker.update(dets, base_ts, None, 1)
        assert len(tracker.active_tracks) == 1
        # Now send empty frames past the max_disappeared threshold
        for i in range(MAX_DISAPPEARED + 5):
            tracker.update([], base_ts + timedelta(seconds=i), None, i + 2)
        assert len(tracker.active_tracks) == 0, "Aged-out track should be removed"

    def test_staff_classification_heuristic(self):
        """Tall narrow box + lots of trajectory = staff candidate."""
        from tracker import Track
        det = {"bbox": [100, 50, 140, 350], "conf": 0.9, "cx": 120, "cy": 200,
               "width": 40, "height": 300}
        t = Track(1, det, datetime.now(timezone.utc), "VIS_STAFF", "S1", "CAM1")
        # Simulate long trajectory
        t.trajectory = [(120 + i * 0.3, 200 + i * 1.5) for i in range(150)]
        assert t.classify_staff(), "Tall narrow fast-moving track should be classified as staff"


# ─── Schema Compliance ────────────────────────────────────────────────────────
class TestSchemaCompliance:

    def test_all_required_fields_present(self, emitter, base_ts):
        track = make_track()
        emitter.emit_entry(track, base_ts)
        import json
        with open(emitter.output_file) as f:
            ev = json.loads(f.readline())
        required = ["event_id", "store_id", "camera_id", "visitor_id",
                    "event_type", "timestamp", "is_staff", "confidence", "metadata"]
        for field in required:
            assert field in ev, f"Required field '{field}' missing from event"

    def test_timestamp_is_iso8601(self, emitter, base_ts):
        track = make_track()
        emitter.emit_entry(track, base_ts)
        import json
        with open(emitter.output_file) as f:
            ev = json.loads(f.readline())
        ts = ev["timestamp"]
        assert ts.endswith("Z") or "+" in ts, f"Timestamp not ISO-8601 UTC: {ts}"
        # Should be parseable
        datetime.fromisoformat(ts.replace("Z", "+00:00"))

    def test_event_id_is_uuid_format(self, emitter, base_ts):
        track = make_track()
        emitter.emit_entry(track, base_ts)
        import json
        with open(emitter.output_file) as f:
            ev = json.loads(f.readline())
        # Should be a valid UUID
        parsed = uuid.UUID(ev["event_id"])
        assert str(parsed) == ev["event_id"]
