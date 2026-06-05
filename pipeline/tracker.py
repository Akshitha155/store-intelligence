"""
tracker.py — Multi-person tracker with Re-ID, direction detection, zone tracking.
Uses IoU + centroid distance for lightweight tracking (no deep Re-ID dependency).
"""

import uuid
import math
import numpy as np
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Optional


# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
MAX_DISAPPEARED     = 30   # frames before track is considered lost
MAX_DIST_PIXELS     = 150  # max centroid distance to associate detection to track
IOU_THRESHOLD       = 0.30 # min IoU for association
DWELL_EMIT_INTERVAL = 30.0 # emit ZONE_DWELL every 30 seconds
REENTRY_WINDOW_SEC  = 300  # 5 minutes — same visitor re-entering within this window = REENTRY
ENTRY_ZONE_Y_FRAC   = 0.35 # top 35% of entry frame = "outside" region
STAFF_ASPECT_RATIO  = 1.8  # tall thin boxes more likely staff (uniform + apron)
STAFF_CONF_BOOST    = 0.15 # confidence penalty for staff classification


def iou(boxA, boxB):
    """Compute Intersection-over-Union of two [x1,y1,x2,y2] boxes."""
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])
    inter = max(0, xB - xA) * max(0, yB - yA)
    if inter == 0:
        return 0.0
    areaA = (boxA[2] - boxA[0]) * (boxA[3] - boxA[1])
    areaB = (boxB[2] - boxB[0]) * (boxB[3] - boxB[1])
    return inter / float(areaA + areaB - inter)


def centroid_dist(c1, c2):
    return math.sqrt((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2)


class Track:
    """Represents a single tracked person across frames."""

    def __init__(self, track_id: int, detection: dict, timestamp: datetime,
                 visitor_id: str, store_id: str, camera_id: str):
        self.track_id       = track_id
        self.visitor_id     = visitor_id
        self.store_id       = store_id
        self.camera_id      = camera_id

        self.bbox           = detection["bbox"]
        self.cx             = detection["cx"]
        self.cy             = detection["cy"]
        self.conf           = detection["conf"]
        self.disappeared    = 0

        self.first_seen_ts  = timestamp
        self.last_seen_ts   = timestamp
        self.first_cy       = detection["cy"]     # for entry/exit direction
        self.last_cy        = detection["cy"]
        self.trajectory     = [(detection["cx"], detection["cy"])]

        self.current_zone   = None
        self.zone_enter_ts  = None
        self.last_dwell_ts  = None
        self.is_staff       = False
        self.session_seq    = 0                   # ordinal position of events in session
        self.has_entered    = False               # fired ENTRY event
        self.has_exited     = False               # fired EXIT event
        self.entered_billing = False
        self.billing_join_ts = None
        self.queue_depth_at_join = 0

    def update(self, detection: dict, timestamp: datetime):
        self.bbox        = detection["bbox"]
        self.cx          = detection["cx"]
        self.cy          = detection["cy"]
        self.conf        = max(self.conf, detection["conf"])  # track best confidence seen
        self.last_seen_ts = timestamp
        self.last_cy     = detection["cy"]
        self.disappeared = 0
        self.trajectory.append((detection["cx"], detection["cy"]))
        if len(self.trajectory) > 60:
            self.trajectory.pop(0)

    def aspect_ratio(self) -> float:
        x1, y1, x2, y2 = self.bbox
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        return h / w

    def classify_staff(self) -> bool:
        """
        Heuristic staff classification:
        - Very tall/thin bounding box (uniform/apron makes people look narrow)
        - Consistent movement across multiple zones (staff patrol)
        - Rarely stops in product zones
        """
        ar = self.aspect_ratio()
        long_trajectory = len(self.trajectory) > 100
        fast_mover = False
        if len(self.trajectory) > 10:
            dists = [
                centroid_dist(self.trajectory[i], self.trajectory[i - 1])
                for i in range(1, min(10, len(self.trajectory)))
            ]
            avg_speed = sum(dists) / len(dists)
            fast_mover = avg_speed > 18  # pixels/frame — staff move purposefully
        return ar > STAFF_ASPECT_RATIO and (long_trajectory or fast_mover)


class PersonTracker:
    """
    Manages all active tracks for one camera.
    Handles:
    - Detection ↔ track assignment (IoU + centroid)
    - Entry / exit direction detection
    - Zone transitions
    - DWELL timing
    - Re-entry detection
    - Staff classification
    - Queue depth (billing camera)
    """

    def __init__(self, store_id, camera_id, camera_role, zone_mapper, emitter, clip_start_time):
        self.store_id       = store_id
        self.camera_id      = camera_id
        self.camera_role    = camera_role
        self.zone_mapper    = zone_mapper
        self.emitter        = emitter
        self.clip_start_time = clip_start_time

        self.active_tracks: dict[int, Track] = {}
        self.next_track_id  = 1
        self.total_tracks_seen = 0

        # For Re-ID: remember visitor_ids that have exited recently
        # key = approx_exit_cy_bucket, value = list of (visitor_id, exit_ts)
        self.recently_exited: list[dict] = []

        # Frame-level state
        self.frame_height   = None
        self.frame_width    = None
        self.current_queue_depth = 0

    # ─────────────────────────────────────────
    # MAIN UPDATE (called per frame)
    # ─────────────────────────────────────────
    def update(self, detections: list[dict], timestamp: datetime,
               frame: np.ndarray, frame_idx: int):

        if frame is not None:
            self.frame_height, self.frame_width = frame.shape[:2]

        if not detections:
            self._age_tracks(timestamp)
            return

        # ── Match detections → existing tracks ──────────────────────
        unmatched_dets   = list(range(len(detections)))
        matched_track_ids = set()

        for track_id, track in list(self.active_tracks.items()):
            best_score  = -1
            best_det_idx = None

            for di in unmatched_dets:
                d     = detections[di]
                score = 0.0
                # IoU component
                iou_v = iou(track.bbox, d["bbox"])
                # Distance component (normalised)
                dist  = centroid_dist((track.cx, track.cy), (d["cx"], d["cy"]))
                dist_score = max(0.0, 1.0 - dist / MAX_DIST_PIXELS)

                score = 0.5 * iou_v + 0.5 * dist_score
                if score > best_score and (iou_v > IOU_THRESHOLD or dist < MAX_DIST_PIXELS):
                    best_score   = score
                    best_det_idx = di

            if best_det_idx is not None:
                self.active_tracks[track_id].update(detections[best_det_idx], timestamp)
                matched_track_ids.add(track_id)
                unmatched_dets.remove(best_det_idx)

        # ── Age unmatched tracks ─────────────────────────────────────
        self._age_tracks(timestamp, skip_ids=matched_track_ids)

        # ── Create new tracks for unmatched detections ───────────────
        for di in unmatched_dets:
            d = detections[di]
            vid = self._find_reentry_visitor(d, timestamp)
            is_reentry = vid is not None
            if vid is None:
                vid = f"VIS_{uuid.uuid4().hex[:6].upper()}"

            t = Track(
                track_id=self.next_track_id,
                detection=d,
                timestamp=timestamp,
                visitor_id=vid,
                store_id=self.store_id,
                camera_id=self.camera_id,
            )
            t.is_staff  = t.classify_staff()
            t.has_entered = is_reentry  # skip second ENTRY if reentry

            self.active_tracks[self.next_track_id] = t
            self.next_track_id += 1
            self.total_tracks_seen += 1

            if is_reentry:
                self.emitter.emit_reentry(t, timestamp)
            elif self.camera_role == "entry":
                pass  # wait to confirm entry direction

        # ── Per-role logic ──────────────────────────────────────────
        if self.camera_role == "entry":
            self._process_entry_camera(timestamp)
        elif self.camera_role in ("zone", "floor"):
            self._process_zone_camera(timestamp)
        elif self.camera_role == "billing":
            self._process_billing_camera(timestamp)

    # ─────────────────────────────────────────
    # ENTRY CAMERA LOGIC
    # ─────────────────────────────────────────
    def _process_entry_camera(self, timestamp: datetime):
        """
        Detect entry vs exit by tracking Y movement across a threshold line.
        Top of frame = outside, bottom = inside store.
        Entry:  first_cy in top region → last_cy moves into bottom region
        Exit:   first_cy in bottom region → last_cy moves into top region
        """
        if not self.frame_height:
            return

        threshold_y = self.frame_height * ENTRY_ZONE_Y_FRAC

        for tid, track in list(self.active_tracks.items()):
            if track.is_staff:
                continue

            traj = track.trajectory
            if len(traj) < 5:
                continue

            start_y = traj[0][1]
            curr_y  = traj[-1][1]
            dy      = curr_y - start_y

            # Entering store: moving from top → bottom
            if not track.has_entered and start_y < threshold_y and curr_y > threshold_y and dy > 20:
                track.has_entered = True
                track.session_seq += 1
                self.emitter.emit_entry(track, timestamp)

            # Exiting store: moving from bottom → top
            elif track.has_entered and not track.has_exited and \
                    start_y > threshold_y and curr_y < threshold_y and dy < -20:
                track.has_exited = True
                track.session_seq += 1
                self.emitter.emit_exit(track, timestamp)
                # Remember this visitor for re-entry detection
                self.recently_exited.append({
                    "visitor_id": track.visitor_id,
                    "exit_ts":    timestamp,
                    "exit_cy":    curr_y,
                })

    # ─────────────────────────────────────────
    # ZONE CAMERA LOGIC
    # ─────────────────────────────────────────
    def _process_zone_camera(self, timestamp: datetime):
        """Map each tracked person's centroid to a zone polygon."""
        for tid, track in list(self.active_tracks.items()):
            if track.is_staff:
                continue

            zone = self.zone_mapper.get_zone(track.cx, track.cy)
            zone_id = zone["zone_id"] if zone else None

            if zone_id != track.current_zone:
                # Zone exit
                if track.current_zone is not None:
                    track.session_seq += 1
                    self.emitter.emit_zone_exit(track, timestamp, track.current_zone)
                # Zone enter
                if zone_id is not None:
                    track.current_zone = zone_id
                    track.zone_enter_ts = timestamp
                    track.last_dwell_ts = timestamp
                    track.session_seq  += 1
                    self.emitter.emit_zone_enter(track, timestamp, zone)
                else:
                    track.current_zone  = None
                    track.zone_enter_ts = None

            # DWELL: emit every 30s if still in same zone
            elif zone_id is not None and track.zone_enter_ts:
                elapsed = (timestamp - track.zone_enter_ts).total_seconds()
                since_last = (
                    (timestamp - track.last_dwell_ts).total_seconds()
                    if track.last_dwell_ts else elapsed
                )
                if elapsed >= DWELL_EMIT_INTERVAL and since_last >= DWELL_EMIT_INTERVAL:
                    track.last_dwell_ts = timestamp
                    track.session_seq  += 1
                    dwell_ms = int(elapsed * 1000)
                    self.emitter.emit_zone_dwell(track, timestamp, zone, dwell_ms)

    # ─────────────────────────────────────────
    # BILLING CAMERA LOGIC
    # ─────────────────────────────────────────
    def _process_billing_camera(self, timestamp: datetime):
        """
        Track queue depth and emit BILLING_QUEUE_JOIN / ABANDON events.
        Queue depth = number of people in the billing zone right now.
        """
        billing_zone = self.zone_mapper.get_billing_zone()
        if not billing_zone:
            return

        people_in_billing = []
        for tid, track in self.active_tracks.items():
            if track.is_staff:
                continue
            z = self.zone_mapper.get_zone(track.cx, track.cy)
            if z and z.get("zone_type") == "BILLING":
                people_in_billing.append(tid)

        queue_depth = max(0, len(people_in_billing) - 1)  # subtract the one being served
        self.current_queue_depth = queue_depth

        for tid in people_in_billing:
            track = self.active_tracks[tid]
            if not track.entered_billing:
                track.entered_billing        = True
                track.billing_join_ts        = timestamp
                track.queue_depth_at_join    = queue_depth
                track.session_seq           += 1
                if queue_depth > 0:
                    self.emitter.emit_billing_queue_join(track, timestamp, queue_depth)

    # ─────────────────────────────────────────
    # TRACK AGING
    # ─────────────────────────────────────────
    def _age_tracks(self, timestamp: datetime, skip_ids: set = None):
        skip_ids = skip_ids or set()
        to_delete = []

        for tid, track in self.active_tracks.items():
            if tid in skip_ids:
                continue
            track.disappeared += 1
            if track.disappeared > MAX_DISAPPEARED:
                # Track is lost → emit exit if in store
                if track.has_entered and not track.has_exited and self.camera_role == "entry":
                    track.has_exited = True
                    track.session_seq += 1
                    self.emitter.emit_exit(track, timestamp)
                    self.recently_exited.append({
                        "visitor_id": track.visitor_id,
                        "exit_ts":    timestamp,
                        "exit_cy":    track.last_cy,
                    })
                # Billing abandon check
                if track.entered_billing and self.camera_role == "billing":
                    self.emitter.emit_billing_queue_abandon(
                        track, timestamp, track.queue_depth_at_join
                    )
                to_delete.append(tid)

        for tid in to_delete:
            del self.active_tracks[tid]

    # ─────────────────────────────────────────
    # RE-ENTRY DETECTION
    # ─────────────────────────────────────────
    def _find_reentry_visitor(self, detection: dict, timestamp: datetime) -> Optional[str]:
        """
        If a new detection appears near the entry zone shortly after a visitor exited,
        treat as re-entry and return the original visitor_id.
        """
        cutoff = timestamp - timedelta(seconds=REENTRY_WINDOW_SEC)
        # Clean old entries
        self.recently_exited = [
            r for r in self.recently_exited if r["exit_ts"] > cutoff
        ]
        if not self.recently_exited:
            return None

        # Spatial match: similar Y position (within 50px of entry zone)
        if self.frame_height:
            thresh = self.frame_height * ENTRY_ZONE_Y_FRAC
            if detection["cy"] > thresh * 1.5:
                return None  # too far inside store to be re-entry

        # Pick the most recent exit with similar horizontal position
        for rec in reversed(self.recently_exited):
            if abs(detection["cy"] - rec.get("exit_cy", 9999)) < 80:
                self.recently_exited.remove(rec)
                return rec["visitor_id"]
        return None

    # ─────────────────────────────────────────
    # FLUSH (end of clip)
    # ─────────────────────────────────────────
    def flush(self, end_timestamp: datetime):
        """Close all remaining open tracks at end of clip."""
        for tid, track in list(self.active_tracks.items()):
            if track.current_zone and self.camera_role in ("zone", "billing"):
                elapsed = (end_timestamp - (track.zone_enter_ts or end_timestamp)).total_seconds()
                self.emitter.emit_zone_exit(track, end_timestamp, track.current_zone)

            if track.has_entered and not track.has_exited and self.camera_role == "entry":
                track.has_exited = True
                self.emitter.emit_exit(track, end_timestamp)

            if track.entered_billing and not track.has_exited and self.camera_role == "billing":
                self.emitter.emit_billing_queue_abandon(
                    track, end_timestamp, track.queue_depth_at_join
                )

        self.active_tracks.clear()
