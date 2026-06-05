"""
load_pos.py — Load POS transactions CSV into the database.
Usage: python load_pos.py <path_to_csv>
"""
import sys
import csv
import os
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from database import get_conn, init_db


def load_pos(csv_path: str):
    init_db()
    loaded = 0
    skipped = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        with get_conn() as conn:
            for row in reader:
                try:
                    # Support both CSV formats
                    store_id = row.get("store_id", row.get("store_code", "UNKNOWN"))
                    txn_id   = row.get("transaction_id", row.get("order_id", str(uuid.uuid4())))
                    amount   = float(row.get("basket_value_inr",
                                    row.get("total_amount", 0)) or 0)

                    # Build timestamp
                    if "timestamp" in row:
                        ts = row["timestamp"]
                    elif "order_date" in row and "order_time" in row:
                        ts_raw = f"{row['order_date']} {row['order_time']}"
                        try:
                            dt = datetime.strptime(ts_raw, "%d-%m-%Y %H:%M:%S")
                        except ValueError:
                            dt = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
                        ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                    else:
                        continue

                    conn.execute("""
                        INSERT OR IGNORE INTO pos_transactions
                            (transaction_id, store_id, timestamp, basket_value)
                        VALUES (?, ?, ?, ?)
                    """, (str(txn_id), store_id, ts, amount))
                    loaded += 1

                except Exception as ex:
                    skipped += 1
                    print(f"[SKIP] Row {loaded+skipped}: {ex}")

    print(f"[✓] Loaded {loaded} POS transactions. Skipped: {skipped}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python load_pos.py <path_to_csv>")
        sys.exit(1)
    load_pos(sys.argv[1])
