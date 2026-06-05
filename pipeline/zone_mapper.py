"""
zone_mapper.py — Maps pixel (cx, cy) coordinates to zone definitions from store_layout.json.
Supports polygon-based zones (precise) and bounding-rect fallback.
"""

import json
from typing import Optional


def point_in_polygon(px: float, py: float, polygon: list) -> bool:
    """Ray-casting algorithm to test if (px, py) is inside a polygon."""
    n = len(polygon)
    inside = False
    x, y = px, py
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def rect_to_poly(rect: dict) -> list:
    """Convert {x, y, w, h} rect to polygon list."""
    x, y, w, h = rect["x"], rect["y"], rect.get("w", rect.get("width", 0)), rect.get("h", rect.get("height", 0))
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


class ZoneMapper:
    """
    Given a store_layout.json (or our synthetic layout), map pixel coordinates to zones.

    The layout JSON can have zones defined as:
      - polygon: list of [x, y] normalized (0–1) or pixel coordinates
      - rect: {x, y, w, h} in normalized or pixel coordinates
    """

    def __init__(self, layout: dict, camera_role: str):
        self.camera_role = camera_role
        self.zones: list[dict] = []
        self._parse_layout(layout, camera_role)

    def _parse_layout(self, layout: dict, role: str):
        """Parse layout JSON into normalised zone definitions."""
        cameras = layout.get("cameras", {})
        zones_raw = layout.get("zones", [])

        # Filter zones relevant to this camera role
        for z in zones_raw:
            cam_roles = z.get("camera_roles", [role])
            if role not in cam_roles and "all" not in cam_roles:
                continue

            zone_entry = {
                "zone_id":   z.get("zone_id", z.get("id", "UNKNOWN")),
                "zone_name": z.get("zone_name", z.get("name", "Unknown")),
                "zone_type": z.get("zone_type", "SHELF"),
                "is_revenue_zone": z.get("is_revenue_zone", "Yes"),
                "sku_zone":  z.get("sku_zone", z.get("zone_name", "")),
            }

            # Prefer polygon definition
            if "polygon_px" in z:
                zone_entry["poly_px"] = z["polygon_px"]
            elif "polygon_norm" in z:
                # Will be resolved later with frame dimensions
                zone_entry["poly_norm"] = z["polygon_norm"]
                zone_entry["poly_px"]   = z["polygon_norm"]  # placeholder
            elif "rect" in z:
                zone_entry["poly_px"] = rect_to_poly(z["rect"])
            elif "bbox_norm" in z:
                b = z["bbox_norm"]  # [x1, y1, x2, y2] normalised
                zone_entry["bbox_norm"] = b
                zone_entry["poly_px"]   = [
                    [b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]
                ]
            else:
                continue  # skip zones with no geometry

            self.zones.append(zone_entry)

    def get_zone(self, cx: float, cy: float) -> Optional[dict]:
        """Return the first zone that contains point (cx, cy)."""
        for zone in self.zones:
            poly = zone.get("poly_px", [])
            if len(poly) >= 3 and point_in_polygon(cx, cy, poly):
                return zone
        return None

    def get_billing_zone(self) -> Optional[dict]:
        """Return the billing zone definition."""
        for z in self.zones:
            if z.get("zone_type") == "BILLING":
                return z
        return None

    def get_zone_name(self, zone_id: str) -> str:
        for z in self.zones:
            if z["zone_id"] == zone_id:
                return z["zone_name"]
        return zone_id


# ─────────────────────────────────────────────
# SYNTHETIC LAYOUT GENERATOR
# (used when no layout JSON is provided)
# ─────────────────────────────────────────────

def generate_synthetic_layout(store_id: str, frame_width: int = 1920, frame_height: int = 1080) -> dict:
    """
    Generate a synthetic store layout based on the Store 1 floor plan:
    - Salm, TFS, Minimalis, Aqualogi, Foxtal, JC (top shelf row)
    - F.O.H (center floor)
    - Fragrance / Nail / Makeup Unit (middle)
    - Fac, Mars+Nybae, Mens, Lo'real, Beaut (bottom row)
    - Cash Counter / Billing (right side)
    All coordinates are in pixels for a 1920x1080 frame.
    """
    W, H = frame_width, frame_height

    return {
        "store_id": store_id,
        "cameras": {
            "entry":   {"id": "CAM_ENTRY_01",   "role": "entry"},
            "zone":    {"id": "CAM_ZONE_01",     "role": "zone"},
            "billing": {"id": "CAM_BILLING_01",  "role": "billing"},
        },
        "zones": [
            # ── Top shelf row ──────────────────────────────────────
            {
                "zone_id": f"{store_id}_Z_SALM",
                "zone_name": "Salm",
                "zone_type": "SHELF",
                "is_revenue_zone": "Yes",
                "sku_zone": "SALM",
                "camera_roles": ["zone", "all"],
                "polygon_px": [[int(W*0.04), int(H*0.04)], [int(W*0.15), int(H*0.04)],
                               [int(W*0.15), int(H*0.20)], [int(W*0.04), int(H*0.20)]],
            },
            {
                "zone_id": f"{store_id}_Z_TFS",
                "zone_name": "TFS",
                "zone_type": "SHELF",
                "is_revenue_zone": "Yes",
                "sku_zone": "TFS",
                "camera_roles": ["zone", "all"],
                "polygon_px": [[int(W*0.15), int(H*0.04)], [int(W*0.26), int(H*0.04)],
                               [int(W*0.26), int(H*0.20)], [int(W*0.15), int(H*0.20)]],
            },
            {
                "zone_id": f"{store_id}_Z_MINIMALIS",
                "zone_name": "Minimalis",
                "zone_type": "SHELF",
                "is_revenue_zone": "Yes",
                "sku_zone": "MINIMALIS",
                "camera_roles": ["zone", "all"],
                "polygon_px": [[int(W*0.42), int(H*0.04)], [int(W*0.55), int(H*0.04)],
                               [int(W*0.55), int(H*0.20)], [int(W*0.42), int(H*0.20)]],
            },
            {
                "zone_id": f"{store_id}_Z_AQUALOGI",
                "zone_name": "Aqualogi",
                "zone_type": "SHELF",
                "is_revenue_zone": "Yes",
                "sku_zone": "AQUALOGI",
                "camera_roles": ["zone", "all"],
                "polygon_px": [[int(W*0.55), int(H*0.04)], [int(W*0.67), int(H*0.04)],
                               [int(W*0.67), int(H*0.20)], [int(W*0.55), int(H*0.20)]],
            },
            {
                "zone_id": f"{store_id}_Z_FOXTAL",
                "zone_name": "Foxtal",
                "zone_type": "SHELF",
                "is_revenue_zone": "Yes",
                "sku_zone": "FOXTAL",
                "camera_roles": ["zone", "all"],
                "polygon_px": [[int(W*0.67), int(H*0.04)], [int(W*0.78), int(H*0.04)],
                               [int(W*0.78), int(H*0.20)], [int(W*0.67), int(H*0.20)]],
            },
            # ── Center FOH ────────────────────────────────────────
            {
                "zone_id": f"{store_id}_Z_FOH",
                "zone_name": "F.O.H",
                "zone_type": "DISPLAY",
                "is_revenue_zone": "Yes",
                "sku_zone": "FOH",
                "camera_roles": ["zone", "all"],
                "polygon_px": [[int(W*0.28), int(H*0.28)], [int(W*0.72), int(H*0.28)],
                               [int(W*0.72), int(H*0.65)], [int(W*0.28), int(H*0.65)]],
            },
            # ── Fragrance / Nail ─────────────────────────────────
            {
                "zone_id": f"{store_id}_Z_FRAGRANCE",
                "zone_name": "Fragrance",
                "zone_type": "SHELF",
                "is_revenue_zone": "Yes",
                "sku_zone": "FRAGRANCE",
                "camera_roles": ["zone", "all"],
                "polygon_px": [[int(W*0.26), int(H*0.30)], [int(W*0.38), int(H*0.30)],
                               [int(W*0.38), int(H*0.55)], [int(W*0.26), int(H*0.55)]],
            },
            # ── Bottom shelf row ──────────────────────────────────
            {
                "zone_id": f"{store_id}_Z_FACES",
                "zone_name": "Faces",
                "zone_type": "SHELF",
                "is_revenue_zone": "Yes",
                "sku_zone": "FACES",
                "camera_roles": ["zone", "all"],
                "polygon_px": [[int(W*0.13), int(H*0.68)], [int(W*0.30), int(H*0.68)],
                               [int(W*0.30), int(H*0.88)], [int(W*0.13), int(H*0.88)]],
            },
            {
                "zone_id": f"{store_id}_Z_MARS_NYBAE",
                "zone_name": "Mars+Nybae",
                "zone_type": "SHELF",
                "is_revenue_zone": "Yes",
                "sku_zone": "MARS_NYBAE",
                "camera_roles": ["zone", "all"],
                "polygon_px": [[int(W*0.38), int(H*0.68)], [int(W*0.52), int(H*0.68)],
                               [int(W*0.52), int(H*0.88)], [int(W*0.38), int(H*0.88)]],
            },
            {
                "zone_id": f"{store_id}_Z_MENS",
                "zone_name": "Mens",
                "zone_type": "SHELF",
                "is_revenue_zone": "Yes",
                "sku_zone": "MENS",
                "camera_roles": ["zone", "all"],
                "polygon_px": [[int(W*0.52), int(H*0.68)], [int(W*0.62), int(H*0.68)],
                               [int(W*0.62), int(H*0.88)], [int(W*0.52), int(H*0.88)]],
            },
            {
                "zone_id": f"{store_id}_Z_LOREAL",
                "zone_name": "Lo'real",
                "zone_type": "SHELF",
                "is_revenue_zone": "Yes",
                "sku_zone": "LOREAL",
                "camera_roles": ["zone", "all"],
                "polygon_px": [[int(W*0.68), int(H*0.68)], [int(W*0.80), int(H*0.68)],
                               [int(W*0.80), int(H*0.88)], [int(W*0.68), int(H*0.88)]],
            },
            # ── Billing / Cash Counter ────────────────────────────
            {
                "zone_id": f"{store_id}_Z_BILLING",
                "zone_name": "Billing Counter",
                "zone_type": "BILLING",
                "is_revenue_zone": "Yes",
                "sku_zone": "BILLING",
                "camera_roles": ["billing", "zone", "all"],
                "polygon_px": [[int(W*0.82), int(H*0.20)], [int(W*0.98), int(H*0.20)],
                               [int(W*0.98), int(H*0.75)], [int(W*0.82), int(H*0.75)]],
            },
        ],
        "open_hours": {"open": "10:00", "close": "22:00"},
        "store_name": f"Purplle Store {store_id}",
    }
