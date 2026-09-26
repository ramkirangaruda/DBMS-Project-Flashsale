"""
Bulk-seeds a large synthetic dataset (~200k Order + ~400k OrderItem rows)
for the Section 6 indexing demo (app/demos/demo_8_indexing.py):

- Several flash sale events with orders front-loaded near the start of
  each sale (an exponential decay curve), so "orders in the last N
  seconds" / "orders in the first 60s" queries are actually meaningful.
- ~50,000 users, most with a unique device_fingerprint but a handful of
  "hot" fingerprints shared by 150-500 accounts each -- the bot-farm
  signal the hash index exists to look up quickly.
- A ~4% slice of orders marked status='flagged' with a matching
  FlaggedOrder row, for the Section 6 3-way join case study.

Uses COPY (via a raw psycopg2 connection) instead of the ORM -- an ORM
insert loop over six-figure row counts is far too slow to re-run casually.

ADDITIVE, NEVER DESTRUCTIVE
---------------------------
This script inserts on top of whatever is already in the database. It owns
exactly one slice of the data -- the ~50k synthetic users tagged with the
@indexdemo.example email domain, and every order / order_item /
flagged_order belonging to THOSE users -- and a rerun replaces only that
slice. scripts/seed.py's canonical demo rows (and any orders its 30 demo
users have placed) are never in range.

Its bulk orders deliberately target scripts/seed.py's DEMO_SALE_ID rather
than inventing a private sale, so demo_8_indexing measures its indexes
against the very sale every other demo uses. That is also why ownership is
keyed on user_id and not sale_id anywhere in this project: the two scripts
share a sale, so a sale-scoped DELETE would cross the boundary.

Requires scripts/seed.py to have been run at least once (it needs the
canonical product and sale to hang orders off). It checks, and says so
plainly rather than failing with a foreign-key error.

Run with: venv/bin/python -m scripts.seed_large
(takes roughly a minute or two, depending on your machine)
"""
import sys
import os
import csv
import io
import random
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from faker import Faker
from sqlalchemy import text
from app.database import Base, engine, SessionLocal
from app import models  # noqa: F401 -- registers every table on Base.metadata so create_all() sees them
from scripts.seed import DEMO_PRODUCT_ID, DEMO_SALE_ID

fake = Faker()
random.seed(42)

DEMO_EMAIL_DOMAIN = "indexdemo.example"   # this script's ownership tag

N_USERS = 50_000
N_ORDERS = 200_000
N_HOT_FINGERPRINTS = 50
HOT_FP_USER_RANGE = (150, 500)          # accounts sharing one "bot farm" fingerprint
SALE_DURATION_S = 1800                  # 30 minute flash sale window
ORDER_TIME_MEAN_S = 45                  # exponential decay -- most orders land in the first minute or two
FLAG_RATE = 0.04                        # ~4% of orders get flagged as suspicious

COPY_CHUNK = 20_000


class CanonicalDataMissing(RuntimeError):
    pass


def require_seed(db):
    """
    Bulk orders hang off scripts/seed.py's canonical product and sale, so
    those rows have to exist first. Checked explicitly to give a useful
    message instead of an opaque foreign-key violation 200k rows later.

    Returns the sale's start_time, which anchors the generated order
    timestamps to the real sale window.
    """
    row = db.execute(
        text("""
            SELECT f.start_time
            FROM flash_sale_events f
            JOIN products p ON p.id = f.product_id
            WHERE f.id = :sid AND p.id = :pid
        """),
        {"sid": str(DEMO_SALE_ID), "pid": str(DEMO_PRODUCT_ID)},
    ).fetchone()

    if row is None:
        raise CanonicalDataMissing(
            "The canonical demo product/sale rows are missing, so there is "
            "nothing for these synthetic orders to reference.\n\n"
            "  Run this first:  python -m scripts.seed\n\n"
            "(scripts/seed.py creates the fixed DEMO_PRODUCT_ID / DEMO_SALE_ID "
            "rows that this script's bulk orders attach to. Both scripts are "
            "additive, so order beyond that first run does not matter.)"
        )
    return row[0]


def cleanup(conn):
    """
    Remove only THIS script's previous bulk load.

    Ownership is keyed on the synthetic users' email domain, never on
    sale_id: these orders share scripts/seed.py's DEMO_SALE_ID, so deleting
    by sale would wipe the canonical demo's own orders too. Everything below
    is scoped to rows whose user_id belongs to an @indexdemo.example user,
    children deleted before parents.
    """
    print(f"Removing any previous @{DEMO_EMAIL_DOMAIN} bulk data "
          f"(leaves scripts/seed.py's rows and its users' orders alone)...")
    owned_users = "SELECT id FROM users WHERE email LIKE :pat"
    params = {"pat": f"%@{DEMO_EMAIL_DOMAIN}"}

    conn.execute(text(f"""
        DELETE FROM flagged_orders WHERE order_id IN (
            SELECT id FROM orders WHERE user_id IN ({owned_users})
        )
    """), params)
    conn.execute(text(f"""
        DELETE FROM order_items WHERE order_id IN (
            SELECT id FROM orders WHERE user_id IN ({owned_users})
        )
    """), params)
    # payments (app/models.py) is a newer FK-child of orders -- without
    # deleting it first, any order that ever had a /payment/create call
    # against it blocks the DELETE FROM orders below.
    conn.execute(text(f"""
        DELETE FROM payments WHERE order_id IN (
            SELECT id FROM orders WHERE user_id IN ({owned_users})
        )
    """), params)
    conn.execute(text(f"DELETE FROM user_behavior_logs WHERE user_id IN ({owned_users})"), params)
    conn.execute(text(f"DELETE FROM orders WHERE user_id IN ({owned_users})"), params)
    conn.execute(text("DELETE FROM users WHERE email LIKE :pat"), params)


def copy_rows(raw_conn, table, columns, rows):
    """COPY a batch of row-tuples into `table` via CSV over STDIN -- far
    faster than an ORM insert loop for six-figure row counts."""
    if not rows:
        return
    cur = raw_conn.cursor()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(rows)
    buf.seek(0)
    cols = ", ".join(columns)
    cur.copy_expert(f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv)", buf)
    cur.close()


def copy_in_chunks(raw_conn, table, columns, rows):
    for i in range(0, len(rows), COPY_CHUNK):
        copy_rows(raw_conn, table, columns, rows[i:i + COPY_CHUNK])


def gen_users():
    """Most users get a unique device fingerprint. A handful of "hot"
    fingerprints are shared by 150-500 accounts each -- the bot-farm
    signal the hash index exists to look up quickly."""
    hot_fps = [str(uuid.uuid4()) for _ in range(N_HOT_FINGERPRINTS)]
    hot_counts = [random.randint(*HOT_FP_USER_RANGE) for _ in hot_fps]
    n_hot_users = sum(hot_counts)

    rows = []
    idx = 0
    for fp, count in zip(hot_fps, hot_counts):
        for _ in range(count):
            uid = str(uuid.uuid4())
            rows.append((uid, fake.name()[:120], f"user{idx}@{DEMO_EMAIL_DOMAIN}", fp, datetime.now(timezone.utc)))
            idx += 1

    for _ in range(max(0, N_USERS - n_hot_users)):
        uid = str(uuid.uuid4())
        fp = str(uuid.uuid4())
        rows.append((uid, fake.name()[:120], f"user{idx}@{DEMO_EMAIL_DOMAIN}", fp, datetime.now(timezone.utc)))
        idx += 1

    random.shuffle(rows)
    hottest_idx = hot_counts.index(max(hot_counts))
    return rows, hot_fps[hottest_idx], hot_counts[hottest_idx]


def sample_offset():
    off = random.expovariate(1 / ORDER_TIME_MEAN_S)
    return min(off, SALE_DURATION_S - 1)


def gen_orders_and_items(sale_start, user_ids):
    """All bulk orders target scripts/seed.py's canonical DEMO_SALE_ID, with
    timestamps front-loaded after `sale_start` on an exponential decay curve
    so "orders in the last N seconds" queries are meaningful."""
    orders = []
    items = []
    flagged_ids = []

    sale_id = str(DEMO_SALE_ID)
    product_id = str(DEMO_PRODUCT_ID)

    for _ in range(N_ORDERS):
        oid = str(uuid.uuid4())
        uid = random.choice(user_ids)
        created = sale_start + timedelta(seconds=sample_offset())

        if random.random() < FLAG_RATE:
            status = "flagged"
            flagged_ids.append(oid)
        else:
            status = random.choices(["confirmed", "failed", "pending"], weights=[0.85, 0.10, 0.05])[0]

        orders.append((oid, uid, sale_id, status, created))

        for _ in range(random.randint(1, 3)):
            items.append((str(uuid.uuid4()), oid, product_id, random.randint(1, 3),
                          f"{random.uniform(10, 250):.2f}"))

    return orders, items, flagged_ids


def gen_flagged(flagged_ids):
    rows = []
    for oid in flagged_ids:
        score = f"{random.uniform(0.5, 0.99):.4f}"
        status = random.choice(["pending", "confirmed_bot", "cleared"])
        rows.append((str(uuid.uuid4()), oid, score, datetime.now(timezone.utc), status))
    return rows


def run():
    print("Creating tables (if not already present)...")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    sale_start = require_seed(db)
    cleanup(db)
    db.commit()
    db.close()

    raw_conn = engine.raw_connection()
    try:
        print(f"Attaching bulk orders to the canonical sale {DEMO_SALE_ID} "
              f"(started {sale_start:%Y-%m-%d %H:%M:%S %Z})...")

        print(f"Generating {N_USERS:,} users ({N_HOT_FINGERPRINTS} hot device-fingerprint clusters)...")
        user_rows, hot_fp, hot_fp_count = gen_users()
        copy_in_chunks(raw_conn, "users", ["id", "name", "email", "device_fingerprint", "created_at"], user_rows)
        user_ids = [u[0] for u in user_rows]

        print(f"Generating ~{N_ORDERS:,} orders and their order_items...")
        orders, items, flagged_ids = gen_orders_and_items(sale_start, user_ids)
        copy_in_chunks(raw_conn, "orders", ["id", "user_id", "sale_id", "status", "created_at"], orders)
        copy_in_chunks(raw_conn, "order_items", ["id", "order_id", "product_id", "quantity", "price_at_purchase"], items)

        print(f"Flagging {len(flagged_ids):,} orders ({FLAG_RATE*100:.0f}%) as suspicious...")
        flagged_rows = gen_flagged(flagged_ids)
        copy_in_chunks(raw_conn, "flagged_orders", ["id", "order_id", "anomaly_score", "flagged_at", "review_status"], flagged_rows)

        raw_conn.commit()
    except Exception:
        raw_conn.rollback()
        raise
    finally:
        raw_conn.close()

    print("Refreshing planner statistics (ANALYZE) so EXPLAIN reflects reality...")
    db = SessionLocal()
    for tbl in ("orders", "order_items", "users", "flagged_orders"):
        db.execute(text(f"ANALYZE {tbl}"))
    db.commit()
    db.close()

    # Only the randomised bits need writing out -- the sale id is now the
    # fixed DEMO_SALE_ID, but demo_8_indexing.py still reads it from here so
    # it keeps a single "has seed_large run yet?" signal.
    with open(os.path.join(os.path.dirname(__file__), "seed_ids_large.py"), "w") as f:
        f.write(f'PRIMARY_SALE_ID = "{DEMO_SALE_ID}"\n')
        f.write(f'PRIMARY_SALE_START = "{sale_start.isoformat()}"\n')
        f.write(f'HOT_DEVICE_FINGERPRINT = "{hot_fp}"\n')
        f.write(f'HOT_DEVICE_FINGERPRINT_COUNT = {hot_fp_count}\n')
        f.write(f'TOTAL_ORDERS = {len(orders)}\n')
        f.write(f'TOTAL_USERS = {len(user_ids)}\n')

    print("\nseed_large complete.")
    print(f"  users = {len(user_ids):,}  (hottest device_fingerprint shared by {hot_fp_count} accounts)")
    print(f"  orders = {len(orders):,}, order_items = {len(items):,}, flagged_orders = {len(flagged_rows):,}")
    print(f"  all attached to the canonical sale {DEMO_SALE_ID}")


if __name__ == "__main__":
    try:
        run()
    except CanonicalDataMissing as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
