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

This data is additive and self-cleaning: everything it creates is tagged
(products.category = 'synthetic_index_demo', user emails
@indexdemo.example) so reruns only wipe rows it created itself, leaving
scripts/seed.py's small scarce-stock demo data (used by demos 1-7)
untouched.

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

fake = Faker()
random.seed(42)

DEMO_TAG_CATEGORY = "synthetic_index_demo"
DEMO_EMAIL_DOMAIN = "indexdemo.example"

N_PRODUCTS = 8
N_SALES = 6
N_USERS = 50_000
N_ORDERS = 200_000
N_HOT_FINGERPRINTS = 50
HOT_FP_USER_RANGE = (150, 500)          # accounts sharing one "bot farm" fingerprint
SALE_DURATION_S = 1800                  # 30 minute flash sale window
ORDER_TIME_MEAN_S = 45                  # exponential decay -- most orders land in the first minute or two
SALE_WEIGHTS = [0.35, 0.25, 0.15, 0.10, 0.10, 0.05]   # traffic isn't split evenly across sales
FLAG_RATE = 0.04                        # ~4% of orders get flagged as suspicious

COPY_CHUNK = 20_000


def cleanup(conn):
    print("Cleaning up any previous synthetic_index_demo data (leaves scripts/seed.py's data alone)...")
    tagged_sales = """
        SELECT f.id FROM flash_sale_events f
        JOIN products p ON p.id = f.product_id
        WHERE p.category = :cat
    """
    conn.execute(text(f"""
        DELETE FROM order_items WHERE order_id IN (
            SELECT o.id FROM orders o WHERE o.sale_id IN ({tagged_sales})
        )
    """), {"cat": DEMO_TAG_CATEGORY})
    conn.execute(text(f"""
        DELETE FROM flagged_orders WHERE order_id IN (
            SELECT o.id FROM orders o WHERE o.sale_id IN ({tagged_sales})
        )
    """), {"cat": DEMO_TAG_CATEGORY})
    conn.execute(text(f"DELETE FROM orders WHERE sale_id IN ({tagged_sales})"), {"cat": DEMO_TAG_CATEGORY})
    conn.execute(text(f"DELETE FROM inventory WHERE sale_id IN ({tagged_sales})"), {"cat": DEMO_TAG_CATEGORY})
    conn.execute(text("DELETE FROM flash_sale_events WHERE product_id IN (SELECT id FROM products WHERE category = :cat)"), {"cat": DEMO_TAG_CATEGORY})
    conn.execute(text("DELETE FROM products WHERE category = :cat"), {"cat": DEMO_TAG_CATEGORY})
    conn.execute(text("DELETE FROM users WHERE email LIKE :pat"), {"pat": f"%@{DEMO_EMAIL_DOMAIN}"})


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


def gen_products():
    return [
        (str(uuid.uuid4()), fake.catch_phrase()[:200], DEMO_TAG_CATEGORY, f"{random.uniform(20, 300):.2f}")
        for _ in range(N_PRODUCTS)
    ]


def gen_sales(product_ids):
    now = datetime.now(timezone.utc)
    sales = []
    for _ in range(N_SALES):
        pid = random.choice(product_ids)
        start = now - timedelta(hours=random.uniform(1, 72))
        end = start + timedelta(seconds=SALE_DURATION_S)
        price = f"{random.uniform(10, 250):.2f}"
        sales.append((str(uuid.uuid4()), pid, start, end, price))
    return sales


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


def gen_orders_and_items(sales, user_ids):
    orders = []
    items = []
    flagged_ids = []

    sale_order_counts = [round(N_ORDERS * w) for w in SALE_WEIGHTS]
    sale_order_counts[-1] += N_ORDERS - sum(sale_order_counts)  # fix rounding drift

    for (sale_id, product_id, start, end, sale_price), n in zip(sales, sale_order_counts):
        for _ in range(n):
            oid = str(uuid.uuid4())
            uid = random.choice(user_ids)
            created = start + timedelta(seconds=sample_offset())

            if random.random() < FLAG_RATE:
                status = "flagged"
                flagged_ids.append(oid)
            else:
                status = random.choices(["confirmed", "failed", "pending"], weights=[0.85, 0.10, 0.05])[0]

            orders.append((oid, uid, sale_id, status, created))

            for _ in range(random.randint(1, 3)):
                items.append((str(uuid.uuid4()), oid, product_id, random.randint(1, 3), sale_price))

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
    cleanup(db)
    db.commit()
    db.close()

    raw_conn = engine.raw_connection()
    try:
        print(f"Generating {N_PRODUCTS} products, {N_SALES} sale events...")
        products = gen_products()
        copy_rows(raw_conn, "products", ["id", "name", "category", "base_price"], products)
        product_ids = [p[0] for p in products]

        sales = gen_sales(product_ids)
        copy_rows(raw_conn, "flash_sale_events", ["id", "product_id", "start_time", "end_time", "sale_price"], sales)

        inventory_rows = [(str(uuid.uuid4()), sid, 1000, 0, 0) for sid, *_ in sales]
        copy_rows(raw_conn, "inventory", ["id", "sale_id", "total_stock", "reserved_stock", "version"], inventory_rows)

        print(f"Generating {N_USERS:,} users ({N_HOT_FINGERPRINTS} hot device-fingerprint clusters)...")
        user_rows, hot_fp, hot_fp_count = gen_users()
        copy_in_chunks(raw_conn, "users", ["id", "name", "email", "device_fingerprint", "created_at"], user_rows)
        user_ids = [u[0] for u in user_rows]

        print(f"Generating ~{N_ORDERS:,} orders and their order_items...")
        orders, items, flagged_ids = gen_orders_and_items(sales, user_ids)
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

    primary_sale_id, _, primary_start, _, _ = sales[0]  # sales[0] carries the heaviest SALE_WEIGHTS share
    with open(os.path.join(os.path.dirname(__file__), "seed_ids_large.py"), "w") as f:
        f.write(f'SALE_IDS = {[s[0] for s in sales]!r}\n')
        f.write(f'PRIMARY_SALE_ID = "{primary_sale_id}"\n')
        f.write(f'PRIMARY_SALE_START = "{primary_start.isoformat()}"\n')
        f.write(f'HOT_DEVICE_FINGERPRINT = "{hot_fp}"\n')
        f.write(f'HOT_DEVICE_FINGERPRINT_COUNT = {hot_fp_count}\n')
        f.write(f'TOTAL_ORDERS = {len(orders)}\n')
        f.write(f'TOTAL_USERS = {len(user_ids)}\n')

    print("\nseed_large complete.")
    print(f"  products = {len(products)}, sales = {len(sales)}")
    print(f"  users = {len(user_ids):,}  (hottest device_fingerprint shared by {hot_fp_count} accounts)")
    print(f"  orders = {len(orders):,}, order_items = {len(items):,}, flagged_orders = {len(flagged_rows):,}")
    print(f"  primary_sale_id (used by demo_8_indexing.py) = {primary_sale_id}")


if __name__ == "__main__":
    run()
