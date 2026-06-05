"""
generate_layout.py — Generates store_layout.json from the Store 1 floor plan.
Run this if you don't have a layout JSON from the challenge dataset.
"""
import json
import argparse
from zone_mapper import generate_synthetic_layout

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store_id", default="STORE_BLR_001")
    parser.add_argument("--output",   default="store_layout.json")
    parser.add_argument("--width",    type=int, default=1920)
    parser.add_argument("--height",   type=int, default=1080)
    args = parser.parse_args()

    layout = generate_synthetic_layout(args.store_id, args.width, args.height)
    with open(args.output, "w") as f:
        json.dump(layout, f, indent=2)
    print(f"[✓] Layout saved to {args.output} ({len(layout['zones'])} zones)")

if __name__ == "__main__":
    main()
