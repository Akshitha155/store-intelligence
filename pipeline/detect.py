"""
detect.py — Main detection + tracking script for Purplle Store Intelligence
Processes CCTV clips using YOLOv8 + ByteTrack, emits structured events.

Usage:
    python pipeline/detect.py --store_id STORE_BLR_001 --layout store_layout.json
"""

import cv2
import json
import argparse
import os
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

from tracker import PersonTracker
from emit import EventEmitter
from zone_mapper import ZoneMapper

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
YOLO_MODEL      = "yolov8n.pt"   # swap to yolov8s.pt / yolov8m.pt for better accuracy
CONF_THRESHOLD  = 0.35
IOU_THRESHOLD   = 0.45
PERSON_CLASS_ID = 0              # COCO class 0 = person
PROCESS_EVERY_N = 3              # process every Nth frame (speed vs accuracy tradeoff)

CAMERA_ROLES = {
    "entry":   ["entry", "cam3", "cam_3", "entry_1", "entry_2"],
    "zone":    ["zone",  "cam1", "cam2", "cam_1", "cam_2", "floor"],
    "billing": ["billing", "cam5", "cam_5", "billing_area"],
}


def get_camera_role(filename: str) -> str:
    fname = Path(filename).stem.lower()
    for role, keywords in CAMERA_ROLES.items():
        if any(k in fname for k in keywords):
            return role
    return "zone"


def load_layout(layout_path: str) -> dict:
    with open(layout_path) as f:
        return json.load(f)


def process_video(
    video_path: str,
    store_id: str,
    camera_id: str,
    camera_role: str,
    layout: dict,
    emitter: EventEmitter,
    clip_start_time: datetime,
    output_path: str,
    visualize: bool = False,
):
    """
    Process a single video clip. Detects people, tracks them,
    maps to zones, and emits structured events.
    """
    try:
        from ultralytics import YOLO
    except ImportError:
        raise ImportError("Run: pip install ultralytics")

    model = YOLO(YOLO_MODEL)
    zone_mapper = ZoneMapper(layout, camera_role)
    tracker = PersonTracker(
        store_id=store_id,
        camera_id=camera_id,
        camera_role=camera_role,
        zone_mapper=zone_mapper,
        emitter=emitter,
        clip_start_time=clip_start_time,
    )

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"[ERROR] Cannot open video: {video_path}")
        return

    fps         = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width        = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height       = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    print(f"\n[INFO] Processing: {video_path}")
    print(f"       Role={camera_role}, FPS={fps:.1f}, Frames={total_frames}, {width}x{height}")

    # Optional: video writer for annotated output
    writer = None
    if visualize and output_path:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(output_path, fourcc, fps / PROCESS_EVERY_N, (width, height))

    frame_idx   = 0
    processed   = 0
    start_time  = time.time()

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_idx += 1
        if frame_idx % PROCESS_EVERY_N != 0:
            continue

        processed += 1
        frame_ts = clip_start_time + timedelta(seconds=frame_idx / fps)

        # ── YOLO detection ──────────────────────────────────────────
        results = model(
            frame,
            conf=CONF_THRESHOLD,
            iou=IOU_THRESHOLD,
            classes=[PERSON_CLASS_ID],
            verbose=False,
        )[0]

        detections = []
        if results.boxes is not None:
            for box in results.boxes:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                conf            = float(box.conf[0])
                detections.append({
                    "bbox":  [x1, y1, x2, y2],
                    "conf":  conf,
                    "class": int(box.cls[0]),
                    "cx":    (x1 + x2) / 2,
                    "cy":    (y1 + y2) / 2,
                    "width": x2 - x1,
                    "height": y2 - y1,
                })

        # ── Tracker update ──────────────────────────────────────────
        tracker.update(detections, frame_ts, frame, frame_idx)

        # ── Annotated output ────────────────────────────────────────
        if visualize and writer:
            annotated = _draw_frame(frame, detections, tracker, zone_mapper, camera_role)
            writer.write(annotated)

        if processed % 50 == 0:
            elapsed = time.time() - start_time
            pct     = frame_idx / total_frames * 100
            print(f"   [{pct:5.1f}%] Frame {frame_idx}/{total_frames}  "
                  f"({processed} processed, {elapsed:.1f}s elapsed)")

    # ── Flush any open sessions ──────────────────────────────────────
    tracker.flush(clip_start_time + timedelta(seconds=total_frames / fps))

    cap.release()
    if writer:
        writer.release()

    print(f"[DONE] {video_path} — {processed} frames processed")
    print(f"       Tracks seen: {tracker.total_tracks_seen}  |  Events emitted: {emitter.event_count}")


def _draw_frame(frame, detections, tracker, zone_mapper, camera_role):
    """Draw bounding boxes + track IDs + zone overlays on a frame."""
    import numpy as np
    vis = frame.copy()

    # Draw zone polygons
    for zone in zone_mapper.zones:
        pts = np.array(zone.get("poly_px", []), dtype=np.int32)
        if len(pts) >= 3:
            cv2.polylines(vis, [pts], True, (0, 200, 0), 1)
            cx = int(pts[:, 0].mean())
            cy = int(pts[:, 1].mean())
            cv2.putText(vis, zone["zone_name"][:10], (cx - 30, cy),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)

    # Draw detections
    for d in detections:
        x1, y1, x2, y2 = [int(v) for v in d["bbox"]]
        color = (0, 255, 0)
        cv2.rectangle(vis, (x1, y1), (x2, y2), color, 2)
        cv2.putText(vis, f"{d['conf']:.2f}", (x1, y1 - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)

    # Draw track IDs
    for tid, track in tracker.active_tracks.items():
        lx, ly = track.get("last_pos", (0, 0))
        cv2.putText(vis, f"ID:{tid}", (int(lx), int(ly) - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 100, 0), 2)

    return vis


# ─────────────────────────────────────────────
# CLI ENTRYPOINT
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Purplle Store Intelligence — Detection Pipeline")
    parser.add_argument("--store_id",    required=True,  help="Store ID e.g. STORE_BLR_001")
    parser.add_argument("--layout",      required=True,  help="Path to store_layout.json")
    parser.add_argument("--videos_dir",  default=".",    help="Directory containing video clips")
    parser.add_argument("--output",      default="events_output.jsonl", help="Output JSONL file")
    parser.add_argument("--clip_start",  default=None,   help="ISO datetime for clip start (default: now)")
    parser.add_argument("--visualize",   action="store_true", help="Write annotated video output")
    parser.add_argument("--model",       default=YOLO_MODEL, help="YOLO model weights file")
    args = parser.parse_args()

    global YOLO_MODEL
    YOLO_MODEL = args.model

    # Parse clip start time
    if args.clip_start:
        clip_start = datetime.fromisoformat(args.clip_start).replace(tzinfo=timezone.utc)
    else:
        clip_start = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)

    layout = load_layout(args.layout)
    emitter = EventEmitter(output_file=args.output, store_id=args.store_id)

    # Find all video files
    video_extensions = [".mp4", ".avi", ".mov", ".mkv"]
    video_files = []
    videos_dir  = Path(args.videos_dir)
    for ext in video_extensions:
        video_files.extend(sorted(videos_dir.glob(f"*{ext}")))
        video_files.extend(sorted(videos_dir.glob(f"*{ext.upper()}")))
    video_files = list(dict.fromkeys(video_files))  # deduplicate

    if not video_files:
        print(f"[ERROR] No video files found in {args.videos_dir}")
        return

    print(f"\n{'='*60}")
    print(f"  Purplle Store Intelligence — Detection Pipeline")
    print(f"  Store: {args.store_id}  |  Videos: {len(video_files)}")
    print(f"  Output: {args.output}")
    print(f"{'='*60}\n")

    # Assign camera IDs and process each video
    camera_offset = timedelta(0)
    for idx, video_path in enumerate(video_files):
        fname      = video_path.stem.lower()
        role       = get_camera_role(str(video_path))
        camera_id  = f"CAM_{role.upper()}_{idx+1:02d}"
        start_time = clip_start + camera_offset

        out_video = None
        if args.visualize:
            out_video = str(Path(args.output).parent / f"annotated_{video_path.stem}.mp4")

        process_video(
            video_path   = str(video_path),
            store_id     = args.store_id,
            camera_id    = camera_id,
            camera_role  = role,
            layout       = layout,
            emitter      = emitter,
            clip_start_time = start_time,
            output_path  = out_video,
            visualize    = args.visualize,
        )

    emitter.close()
    print(f"\n[✓] Pipeline complete. {emitter.event_count} events written to {args.output}")


if __name__ == "__main__":
    main()
