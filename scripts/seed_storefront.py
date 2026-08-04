"""
Seeds a handful of extra flash sales so the storefront homepage has a hero
slider and a product grid to render, and -- more importantly -- so a DROP can
be scheduled a few seconds into the future.

Why this exists: scripts/seed.py creates exactly one sale whose start_time is
already in the past. That is fine for the demo scripts, but the storefront's
whole party trick is several people opening the page, watching a synchronized
"Opens in 3... 2... 1", and all tapping Buy Now at the same instant. That needs
a sale that has NOT started yet.

    python -m scripts.seed_storefront                 # catalogue, drop in 60s
    python -m scripts.seed_storefront --drop-in 30    # drop 30s from now
    python -m scripts.seed_storefront --drop-in 0     # everything live already
    python -m scripts.seed_storefront --stock 3       # scarcer headline drop

OWNERSHIP: follows the same rule as the other seed scripts -- this one owns
rows tagged with the 'storefront_demo' product category and nothing else, so
it never disturbs scripts/seed.py's canonical sale or seed_large.py's bulk
data. Re-running replaces only its own catalogue. See scripts/seed.py's
docstring for the full ownership model.
"""
import sys
import os
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.database import Base, engine, SessionLocal
from app import models  # noqa: F401 -- registers tables on Base.metadata

# OWNERSHIP TAG: the fixed product-id prefix below, NOT products.category --
# category has to stay free to carry the real display category (earbuds,
# phone, ...) that the storefront renders its icons from.
STOREFRONT_PRODUCT_PREFIX = "5f000000-0000-4000-8000-"

# Fixed ids so a sale_id pasted into a phone stays valid across reseeds, the
# same convention scripts/seed.py uses for its canonical rows.
def _pid(n): return uuid.UUID(f"5f000000-0000-4000-8000-{n:012d}")
def _sid(n): return uuid.UUID(f"5a1e0000-0000-4000-8000-{n:012d}")
def _iid(n): return uuid.UUID(f"1e000000-0000-4000-8000-{n:012d}")

# (name, display category, base_price, sale_price, stock, starts_in_s, runs_for_s)
# Index 0 is the headline drop -- scarce stock, biggest discount, and the one
# whose start time --drop-in controls.
CATALOGUE = [
    ("Aurora Wireless Earbuds Pro", "earbuds",   199.00,  49.00,  5,  None, 900),
    ("Nimbus 5G Smartphone",        "phone",     899.00, 449.00, 12,   -60, 3600),
    ("Pulse Fitness Watch",         "watch",     249.00,  99.00,  8,  -120, 3600),
    ("Vortex Pro Controller",       "joystick",  129.00,  59.00, 20,  -180, 3600),
    ("Halo Studio Headphones",      "headphone", 349.00, 174.00,  6,  -240, 3600),
    ("Lumen 4K Action Camera",      "camera",    459.00, 229.00, 10,  -300, 3600),
]


def cleanup(db):
    """Remove only this script's previous catalogue, children first."""
    owned_sales = """
        SELECT f.id FROM flash_sale_events f
        WHERE f.product_id::text LIKE :pat
    """
    p = {"pat": STOREFRONT_PRODUCT_PREFIX + "%"}
    db.execute(text(f"""
        DELETE FROM flagged_orders WHERE order_id IN (
            SELECT id FROM orders WHERE sale_id IN ({owned_sales})
        )
    """), p)
    db.execute(text(f"""
        DELETE FROM order_items WHERE order_id IN (
            SELECT id FROM orders WHERE sale_id IN ({owned_sales})
        )
    """), p)
    db.execute(text(f"DELETE FROM user_behavior_logs WHERE sale_id IN ({owned_sales})"), p)
    db.execute(text(f"DELETE FROM orders WHERE sale_id IN ({owned_sales})"), p)
    db.execute(text(f"DELETE FROM inventory WHERE sale_id IN ({owned_sales})"), p)
    db.execute(text(f"DELETE FROM flash_sale_events WHERE id IN ({owned_sales})"), p)
    db.execute(text("DELETE FROM products WHERE id::text LIKE :pat"), p)


def clear_redis_keys(sale_ids):
    """The Redis fast path caches stock per sale_id. Those ids are fixed, so a
    counter left at 0 by a previous drop would survive this reseed and make
    /checkout/redis report 'sold out' against fresh stock."""
    try:
        import redis
        r = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6390")),
            decode_responses=True,
        )
        for sid in sale_ids:
            r.delete(f"stock:{sid}")
    except Exception as exc:                                  # noqa: BLE001
        print(f"  (could not clear Redis stock keys: {exc} -- continuing)")


def run(drop_in_seconds=60, headline_stock=None):
    print("Creating tables (if not already present)...")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        cleanup(db)
        db.commit()

        now = datetime.now(timezone.utc)
        created = []

        for i, (name, category, base, sale, stock, starts_in, runs_for) in enumerate(CATALOGUE):
            if i == 0:
                starts_in = drop_in_seconds
                if headline_stock is not None:
                    stock = headline_stock
            start = now + timedelta(seconds=starts_in)
            end = start + timedelta(seconds=runs_for)

            db.execute(
                text("""
                    INSERT INTO products (id, name, category, base_price)
                    VALUES (:id, :name, :cat, :price)
                    ON CONFLICT (id) DO UPDATE
                    SET name = EXCLUDED.name, category = EXCLUDED.category,
                        base_price = EXCLUDED.base_price
                """),
                {"id": str(_pid(i)), "name": name, "cat": category, "price": base},
            )
            db.execute(
                text("""
                    INSERT INTO flash_sale_events (id, product_id, start_time, end_time, sale_price)
                    VALUES (:id, :pid, :start, :end, :price)
                    ON CONFLICT (id) DO UPDATE
                    SET product_id = EXCLUDED.product_id, start_time = EXCLUDED.start_time,
                        end_time = EXCLUDED.end_time, sale_price = EXCLUDED.sale_price
                """),
                {"id": str(_sid(i)), "pid": str(_pid(i)), "start": start, "end": end, "price": sale},
            )
            db.execute(
                text("""
                    INSERT INTO inventory (id, sale_id, total_stock, reserved_stock, version)
                    VALUES (:id, :sid, :total, 0, 0)
                    ON CONFLICT (id) DO UPDATE
                    SET total_stock = EXCLUDED.total_stock, reserved_stock = 0, version = 0
                """),
                {"id": str(_iid(i)), "sid": str(_sid(i)), "total": stock},
            )
            created.append((name, _sid(i), stock, start, sale, base))

        db.commit()
        clear_redis_keys([s for _, s, _, _, _, _ in created])

        headline = created[0]
        print("\nStorefront catalogue seeded.")
        for name, sid, stock, start, sale, base in created:
            delta = (start - now).total_seconds()
            when = "LIVE now" if delta <= 0 else f"opens in {delta:.0f}s"
            print(f"  {name:<32} stock {stock:>3}  ${sale:>7.2f} (was ${base:>7.2f})  {when}")

        print(f"\nHeadline drop: {headline[0]}")
        print(f"  sale_id  = {headline[1]}")
        print(f"  stock    = {headline[2]}")
        if drop_in_seconds > 0:
            print(f"  OPENS IN {drop_in_seconds}s -- open the storefront on a few devices now,")
            print("  they will all unlock Buy Now at the same instant (server clock).")
        else:
            print("  Already live.")
        print("\nRe-run this script to reset stock and schedule another drop.")

    finally:
        db.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    drop_in = 60
    stock = None
    if "--drop-in" in args:
        drop_in = int(args[args.index("--drop-in") + 1])
    if "--stock" in args:
        stock = int(args[args.index("--stock") + 1])
    run(drop_in_seconds=drop_in, headline_stock=stock)
