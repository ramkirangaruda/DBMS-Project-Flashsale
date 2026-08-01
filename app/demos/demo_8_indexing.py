"""
Section 6 -- Indexing.

Proves the two Section 6 indexes actually earn their keep by running
EXPLAIN ANALYZE with each index present, then again immediately after
DROP INDEX, and comparing scan type + execution time:

  1. Composite B+ tree on Order(sale_id, created_at) -- "all orders for
     this sale in the last N seconds".
  2. Hash index on User(device_fingerprint) -- "how many accounts share
     this device fingerprint" (exact-match only, no range queries needed).

Each index is dropped and recreated in place so the demo is repeatable.
Finishes with the Section 6 case study: a 3-way join (orders x order_items
x flagged_orders) computing units sold in the first 60s of a sale broken
down by bot-flag status, confirming the planner pushes the sale_id +
time-window selection down onto the orders scan before the joins run --
the selection-before-join heuristic from the design doc.

Requires the large synthetic dataset from scripts/seed_large.py -- run
that first (seeds ~200k orders, takes a minute or two):

    venv/bin/python -m scripts.seed_large
    venv/bin/python -m app.demos.demo_8_indexing
"""
import sys
import os
import re
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from app.database import SessionLocal

try:
    from scripts.seed_ids_large import (
        PRIMARY_SALE_ID, PRIMARY_SALE_START, HOT_DEVICE_FINGERPRINT,
        HOT_DEVICE_FINGERPRINT_COUNT, TOTAL_ORDERS, TOTAL_USERS,
    )
except ImportError:
    raise SystemExit(
        "scripts/seed_ids_large.py not found -- run `python -m scripts.seed_large` first "
        "to seed the ~200k-row dataset this demo needs."
    )

COMPOSITE_INDEX = "ix_orders_sale_id_created_at"
COMPOSITE_INDEX_DDL = "CREATE INDEX ix_orders_sale_id_created_at ON orders (sale_id, created_at)"
HASH_INDEX = "ix_users_device_fingerprint_hash"
HASH_INDEX_DDL = "CREATE INDEX ix_users_device_fingerprint_hash ON users USING hash (device_fingerprint)"

WINDOW_SECONDS = 60


def explain(db, sql, params=None):
    rows = db.execute(text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql}"), params or {}).fetchall()
    return "\n".join(r[0] for r in rows)


def parse_plan(plan_text):
    time_match = re.search(r"Execution Time:\s*([\d.]+)\s*ms", plan_text)
    exec_ms = float(time_match.group(1)) if time_match else None

    scan_type = None
    for pattern in ("Index Only Scan", "Bitmap Index Scan", "Bitmap Heap Scan", "Index Scan", "Seq Scan"):
        if pattern in plan_text:
            scan_type = pattern
            break
    return scan_type, exec_ms


def confirm_pushdown(plan_text):
    """Heuristic check: does the sale_id/created_at filter show up directly
    on the orders scan node, rather than as a post-join filter?"""
    lines = plan_text.splitlines()
    for i, line in enumerate(lines):
        if "Scan" in line and "orders" in line and "order_items" not in line:
            window = "\n".join(lines[i:i + 3])
            if "sale_id" in window and "created_at" in window:
                return True, line.strip()
    return False, None


def run_with_and_without(db, label, table, sql, params, index_name, index_ddl):
    print("=" * 78)
    print(label)
    print("=" * 78)

    print("\n-- WITH index --")
    plan_with = explain(db, sql, params)
    print(plan_with)
    scan_with, time_with = parse_plan(plan_with)

    print(f"\nDropping {index_name} to show the baseline plan...")
    db.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
    db.execute(text(f"ANALYZE {table}"))
    db.commit()

    print("\n-- WITHOUT index --")
    plan_without = explain(db, sql, params)
    print(plan_without)
    scan_without, time_without = parse_plan(plan_without)

    print(f"\nRecreating {index_name} so the demo is repeatable...")
    db.execute(text(index_ddl))
    db.execute(text(f"ANALYZE {table}"))
    db.commit()

    return {
        "query": label,
        "with_scan": scan_with, "with_ms": time_with,
        "without_scan": scan_without, "without_ms": time_without,
    }


def print_markdown_table(results):
    print("\n" + "=" * 78)
    print("SECTION 6 SUMMARY -- paste directly into your report")
    print("=" * 78 + "\n")
    print("| Query | Index present (scan, time) | Index dropped (scan, time) | Speedup |")
    print("|---|---|---|---|")
    for r in results:
        speedup = (r["without_ms"] / r["with_ms"]) if r["with_ms"] else None
        speedup_s = f"{speedup:.1f}x" if speedup else "n/a"
        with_s = f"{r['with_scan']}, {r['with_ms']:.2f} ms" if r["with_ms"] is not None else r["with_scan"]
        without_s = f"{r['without_scan']}, {r['without_ms']:.2f} ms" if r["without_ms"] is not None else r["without_scan"]
        print(f"| {r['query']} | {with_s} | {without_s} | {speedup_s} |")


def run():
    db = SessionLocal()

    print(f"Dataset: {TOTAL_ORDERS:,} orders, {TOTAL_USERS:,} users "
          f"(hottest device_fingerprint shared by {HOT_DEVICE_FINGERPRINT_COUNT} accounts)")
    print(f"Primary sale for these demos: {PRIMARY_SALE_ID}\n")

    anchor = db.execute(
        text("SELECT MAX(created_at) FROM orders WHERE sale_id = :sid"), {"sid": PRIMARY_SALE_ID}
    ).scalar()
    if anchor is None:
        raise SystemExit("No orders found for the primary sale -- run `python -m scripts.seed_large` first.")
    cutoff = anchor - timedelta(seconds=WINDOW_SECONDS)

    results = []

    results.append(run_with_and_without(
        db,
        f"Query 1 -- all orders for sale {PRIMARY_SALE_ID[:8]}... in the last {WINDOW_SECONDS}s (composite B+ tree)",
        "orders",
        "SELECT id, user_id, status, created_at FROM orders "
        "WHERE sale_id = :sid AND created_at >= :cutoff ORDER BY created_at DESC",
        {"sid": PRIMARY_SALE_ID, "cutoff": cutoff},
        COMPOSITE_INDEX,
        COMPOSITE_INDEX_DDL,
    ))

    results.append(run_with_and_without(
        db,
        f"Query 2 -- accounts sharing device_fingerprint {HOT_DEVICE_FINGERPRINT[:8]}... (hash index)",
        "users",
        "SELECT count(*) FROM users WHERE device_fingerprint = :fp",
        {"fp": HOT_DEVICE_FINGERPRINT},
        HASH_INDEX,
        HASH_INDEX_DDL,
    ))

    print("=" * 78)
    print("Case study -- units sold in the first 60s of a sale, by bot-flag status")
    print("(3-way join: orders x order_items x flagged_orders)")
    print("=" * 78)

    sale_start = datetime.fromisoformat(PRIMARY_SALE_START)
    window_end = sale_start + timedelta(seconds=60)
    case_sql = """
        SELECT
            CASE WHEN fo.id IS NULL THEN 'clean' ELSE fo.review_status END AS bot_flag_status,
            SUM(oi.quantity) AS units_sold
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        LEFT JOIN flagged_orders fo ON fo.order_id = o.id
        WHERE o.sale_id = :sid
          AND o.created_at >= :start
          AND o.created_at < :end
        GROUP BY 1
        ORDER BY units_sold DESC
    """
    case_params = {"sid": PRIMARY_SALE_ID, "start": sale_start, "end": window_end}

    plan_case = explain(db, case_sql, case_params)
    print(plan_case)

    pushed, scan_line = confirm_pushdown(plan_case)
    if pushed:
        print("\nConfirmed: the sale_id + created_at filter is applied directly on the")
        print(f"  orders scan node ({scan_line}) -- pushed down BEFORE the joins to")
        print("  order_items/flagged_orders, matching the design doc's selection-before-join heuristic.")
    else:
        print("\nCould not auto-confirm pushdown from the plan text -- inspect the EXPLAIN output above.")

    print("\nQuery result:")
    case_rows = db.execute(text(case_sql), case_params).fetchall()
    for row in case_rows:
        print(f"  {row[0]:>15}: {row[1]} units")

    db.close()

    print_markdown_table(results)


if __name__ == "__main__":
    run()
