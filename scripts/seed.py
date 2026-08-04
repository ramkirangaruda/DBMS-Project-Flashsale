"""
Seeds the CANONICAL demo data:
- A handful of users (some sharing a device fingerprint, to give the
  bot-detection demo something to find)
- One product with a flash sale that has deliberately scarce stock
  (so the oversell / concurrency demos have something to fight over)

OWNERSHIP MODEL (this is what makes seed.py and seed_large.py composable)
------------------------------------------------------------------------
Both seed scripts write to the same tables, so each one owns a disjoint,
clearly-tagged slice of the data and touches nothing else:

  scripts/seed.py       owns the fixed-UUID canonical rows below, plus the
                        30 demo users (DEMO_USER_IDS) and any orders /
                        order_items / behavior logs / flags belonging to
                        THOSE USERS.
  scripts/seed_large.py owns its ~50k synthetic users (tagged by the
                        @indexdemo.example email domain) and everything
                        belonging to them.

The rule is "a row belongs to whoever owns its user_id". That is why
neither script blanket-TRUNCATEs, and why nothing here deletes by
sale_id: seed_large.py's bulk orders deliberately target DEMO_SALE_ID too
(so the indexing demo has real volume on the same sale the other demos
use), so a sale-scoped delete would destroy the other script's data.

Both scripts are therefore safe to run in either order, any number of
times, without wiping each other.

FIXED IDS
---------
The canonical entities use hardcoded UUIDs rather than uuid4(), so a
sale_id or user_id pasted into Swagger/Postman stays valid across
reseeds. Bulk/synthetic rows (orders, order_items, seed_large's users)
still use uuid4() -- only the handful of entities a human actually copies
needs to be stable.

Run with: venv/bin/python -m scripts.seed
"""
import sys
import os
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.database import Base, engine, SessionLocal
from app import models  # noqa: F401 -- registers every table on Base.metadata so create_all() sees them

# --------------------------------------------------------------------------
# Canonical demo entities -- deliberately constant and obviously fake at a
# glance. These are the IDs the README documents and the demos import.
# --------------------------------------------------------------------------
DEMO_PRODUCT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DEMO_SALE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
DEMO_INVENTORY_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

# The 6 users sharing this fingerprint are the bot cluster the Section 10.2
# scoring pass is meant to find. Fixed so the signal is reproducible.
DEMO_SHARED_DEVICE_FINGERPRINT = "44444444-4444-4444-4444-444444444444"
DEMO_BOT_CLUSTER_SIZE = 6

N_DEMO_USERS = 30
DEMO_USER_IDS = [
    uuid.UUID(f"d0000000-0000-4000-8000-{i:012d}") for i in range(N_DEMO_USERS)
]

DEMO_TOTAL_STOCK = 5          # deliberately scarce -- the contested resource
DEMO_PRODUCT_NAME = "Limited Edition Sneaker"
DEMO_PRODUCT_CATEGORY = "footwear"
DEMO_EMAIL_DOMAIN = "example.com"

# String forms -- the demo scripts bind these straight into text() SQL.
DEMO_SALE_ID_STR = str(DEMO_SALE_ID)
DEMO_USER_ID_STRS = [str(u) for u in DEMO_USER_IDS]


def _clear_redis_stock_key():
    """
    Drop the Redis fast-path counter for the demo sale.

    Necessary precisely BECAUSE DEMO_SALE_ID is now stable: the key
    "stock:<sale_id>" used to be abandoned on every reseed (each run minted
    a new sale_id), but a fixed id means a counter left at 0 by a previous
    demo would survive the reseed and make /checkout/redis report "sold
    out" against freshly restocked inventory. app/main.py re-seeds the key
    lazily from PostgreSQL on the next request.

    Best-effort: Redis being down is not a reason for seeding to fail.
    """
    try:
        import redis

        r = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6390")),
            decode_responses=True,
        )
        r.delete(f"stock:{DEMO_SALE_ID}")
        return True
    except Exception as exc:                                  # noqa: BLE001
        print(f"  (could not clear the Redis stock key: {exc} -- continuing)")
        return False


def _purge_orders_of(db, where_users_sql, params):
    """
    Delete every transactional row belonging to a set of users, children
    first so no FK is ever violated.

    Scoped by USER, never by sale: seed_large.py's bulk orders sit on the
    same DEMO_SALE_ID, and a sale-scoped delete would take them out too.
    """
    db.execute(text(f"""
        DELETE FROM flagged_orders WHERE order_id IN (
            SELECT id FROM orders WHERE user_id IN ({where_users_sql})
        )
    """), params)
    db.execute(text(f"DELETE FROM user_behavior_logs WHERE user_id IN ({where_users_sql})"), params)
    db.execute(text(f"""
        DELETE FROM order_items WHERE order_id IN (
            SELECT id FROM orders WHERE user_id IN ({where_users_sql})
        )
    """), params)
    db.execute(text(f"DELETE FROM orders WHERE user_id IN ({where_users_sql})"), params)


def _drop_legacy_rows(db):
    """
    Remove canonical demo rows left behind by OLDER versions of this script,
    which minted the product/sale/inventory/users with uuid4() instead of the
    fixed IDs above. Without this, re-seeding an existing database hits the
    users.email UNIQUE constraint (same email, different random id).

    Strictly scoped to rows this script owns -- demo users are matched on the
    user%@example.com email pattern and the demo product on its category, so
    seed_large.py's @indexdemo.example users and synthetic_index_demo
    products are never in range. A no-op on a fresh database.
    """
    legacy_users = """
        SELECT id FROM users
        WHERE email LIKE 'user%@{domain}'
          AND NOT (id = ANY(CAST(:keep AS uuid[])))
    """.format(domain=DEMO_EMAIL_DOMAIN)
    params = {"keep": DEMO_USER_ID_STRS}

    n = db.execute(text(f"SELECT count(*) FROM ({legacy_users}) t"), params).scalar()
    if n:
        print(f"  migrating {n} legacy demo user(s) from random UUIDs to fixed IDs...")
    _purge_orders_of(db, legacy_users, params)
    db.execute(text(f"DELETE FROM users WHERE id IN ({legacy_users})"), params)

    # Legacy demo product/sale/inventory (same category, non-fixed id).
    legacy_sales = """
        SELECT f.id FROM flash_sale_events f
        JOIN products p ON p.id = f.product_id
        WHERE p.category = :cat AND p.id <> :pid
    """
    p2 = {"cat": DEMO_PRODUCT_CATEGORY, "pid": str(DEMO_PRODUCT_ID)}
    db.execute(text(f"""
        DELETE FROM flagged_orders WHERE order_id IN (
            SELECT id FROM orders WHERE sale_id IN ({legacy_sales})
        )
    """), p2)
    db.execute(text(f"DELETE FROM user_behavior_logs WHERE sale_id IN ({legacy_sales})"), p2)
    db.execute(text(f"""
        DELETE FROM order_items WHERE order_id IN (
            SELECT id FROM orders WHERE sale_id IN ({legacy_sales})
        )
    """), p2)
    db.execute(text(f"DELETE FROM orders WHERE sale_id IN ({legacy_sales})"), p2)
    db.execute(text(f"DELETE FROM inventory WHERE sale_id IN ({legacy_sales})"), p2)
    db.execute(text(f"DELETE FROM flash_sale_events WHERE id IN ({legacy_sales})"), p2)
    db.execute(
        text("DELETE FROM products WHERE category = :cat AND id <> :pid"), p2
    )


def run():
    print("Creating tables...")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        _drop_legacy_rows(db)

        # Reset THIS script's transactional data only. seed_large.py's bulk
        # rows belong to different users and are left completely alone.
        _purge_orders_of(
            db,
            "SELECT id FROM users WHERE id = ANY(CAST(:ids AS uuid[]))",
            {"ids": DEMO_USER_ID_STRS},
        )
        db.commit()

        print(f"Seeding {N_DEMO_USERS} demo users "
              f"({DEMO_BOT_CLUSTER_SIZE} sharing one device fingerprint for the bot demo)...")
        for i, uid in enumerate(DEMO_USER_IDS):
            fingerprint = (
                DEMO_SHARED_DEVICE_FINGERPRINT
                if i < DEMO_BOT_CLUSTER_SIZE
                # Derived from the user's own fixed id, so it is stable across
                # reseeds like everything else here.
                else f"fp-{uid}"
            )
            db.execute(
                text("""
                    INSERT INTO users (id, name, email, device_fingerprint, created_at)
                    VALUES (:id, :name, :email, :fp, now())
                    ON CONFLICT (id) DO UPDATE
                    SET name = EXCLUDED.name,
                        email = EXCLUDED.email,
                        device_fingerprint = EXCLUDED.device_fingerprint
                """),
                {"id": str(uid), "name": f"User {i}",
                 "email": f"user{i}@{DEMO_EMAIL_DOMAIN}", "fp": fingerprint},
            )

        print("Seeding product + flash sale with scarce stock...")
        db.execute(
            text("""
                INSERT INTO products (id, name, category, base_price)
                VALUES (:id, :name, :cat, :price)
                ON CONFLICT (id) DO UPDATE
                SET name = EXCLUDED.name,
                    category = EXCLUDED.category,
                    base_price = EXCLUDED.base_price
            """),
            {"id": str(DEMO_PRODUCT_ID), "name": DEMO_PRODUCT_NAME,
             "cat": DEMO_PRODUCT_CATEGORY, "price": 199.00},
        )

        # start_time is preserved on re-seed rather than reset to now(): once
        # seed_large.py has laid down 200k orders spread across this sale's
        # window, moving the window would desynchronise them from it.
        now = datetime.now(timezone.utc)
        db.execute(
            text("""
                INSERT INTO flash_sale_events (id, product_id, start_time, end_time, sale_price)
                VALUES (:id, :pid, :start, :end, :price)
                ON CONFLICT (id) DO UPDATE
                SET product_id = EXCLUDED.product_id,
                    end_time = GREATEST(flash_sale_events.end_time, EXCLUDED.end_time),
                    sale_price = EXCLUDED.sale_price
            """),
            {"id": str(DEMO_SALE_ID), "pid": str(DEMO_PRODUCT_ID),
             "start": now, "end": now + timedelta(minutes=10), "price": 99.00},
        )

        db.execute(
            text("""
                INSERT INTO inventory (id, sale_id, total_stock, reserved_stock, version)
                VALUES (:id, :sid, :total, 0, 0)
                ON CONFLICT (id) DO UPDATE
                SET total_stock = EXCLUDED.total_stock,
                    reserved_stock = 0,
                    version = 0
            """),
            {"id": str(DEMO_INVENTORY_ID), "sid": str(DEMO_SALE_ID), "total": DEMO_TOTAL_STOCK},
        )
        db.commit()

        _clear_redis_stock_key()

        remaining = db.execute(text("SELECT count(*) FROM orders")).scalar()

        print("\nSeed complete. These IDs are CONSTANT -- safe to paste into Swagger.")
        print(f"  product_id   = {DEMO_PRODUCT_ID}")
        print(f"  sale_id      = {DEMO_SALE_ID}")
        print(f"  inventory_id = {DEMO_INVENTORY_ID}")
        print(f"  total_stock  = {DEMO_TOTAL_STOCK}")
        print(f"  user_ids     = {DEMO_USER_ID_STRS[0]} ... (+{N_DEMO_USERS - 1} more)")
        print(f"\n  orders left in the database: {remaining:,} "
              f"(seed_large.py's bulk rows are untouched by this script)")

    finally:
        db.close()


if __name__ == "__main__":
    run()
