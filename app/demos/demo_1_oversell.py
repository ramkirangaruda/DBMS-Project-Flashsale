"""
Section 8.1 -- The Oversell Problem (demonstrate the failure first)

Two "checkout" threads both read stock, both see availability, both insert
an order -- with no locking, this classic lost-update race lets more orders
through than there is stock. This is deliberately naive: it's the baseline
every other demo in this project improves on.

Run with: venv/bin/python -m app.demos.demo_1_oversell
"""
import sys, os, time, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from app.database import SessionLocal
from scripts.seed import DEMO_SALE_ID, DEMO_USER_IDS

# Fixed canonical ids from scripts/seed.py -- stable across reseeds.
SALE_ID = str(DEMO_SALE_ID)
USER_IDS = [str(u) for u in DEMO_USER_IDS]


def naive_checkout(user_id: str, results: list, index: int):
    db = SessionLocal()
    try:
        # Step 1: read current stock (NO LOCK)
        row = db.execute(
            text("SELECT id, total_stock, reserved_stock FROM inventory WHERE sale_id = :sid"),
            {"sid": SALE_ID},
        ).fetchone()
        inv_id, total, reserved = row
        available = total - reserved

        # Simulate real-world latency between "check" and "act" (network,
        # app logic, etc.) -- this is exactly the window that causes the race.
        time.sleep(0.05)

        if available > 0:
            # Step 2: naive read-then-write, no WHERE guard on the value we read
            db.execute(
                text("UPDATE inventory SET reserved_stock = reserved_stock + 1 WHERE id = :iid"),
                {"iid": inv_id},
            )
            order_id = db.execute(
                text(
                    "INSERT INTO orders (id, user_id, sale_id, status, created_at) "
                    "VALUES (gen_random_uuid(), :uid, :sid, 'confirmed', now()) RETURNING id"
                ),
                {"uid": user_id, "sid": SALE_ID},
            ).fetchone()[0]
            db.commit()
            results[index] = f"CONFIRMED order {order_id}"
        else:
            db.rollback()
            results[index] = "REJECTED (no stock)"
    except Exception as e:  # noqa: BLE001
        db.rollback()
        reason = "CHECK ck_inventory_no_oversell" if "ck_inventory_no_oversell" in str(e) else type(e).__name__
        results[index] = f"REFUSED BY DATABASE ({reason})"
    finally:
        db.close()


def _fire(label):
    reset_db = SessionLocal()
    reset_db.execute(text("UPDATE inventory SET reserved_stock = 0, total_stock = 1 WHERE sale_id = :sid"), {"sid": SALE_ID})
    reset_db.commit()
    reset_db.close()

    N = 8  # 8 concurrent buyers competing for 1 unit
    results = [None] * N
    print(f"{label}\nFiring {N} concurrent checkout attempts at 1 unit of stock (no locking)...\n")
    threads = [threading.Thread(target=naive_checkout, args=(USER_IDS[i], results, i)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for i, r in enumerate(results):
        print(f"  buyer {i}: {r}")
    return sum(1 for r in results if r and r.startswith("CONFIRMED"))


def run():
    """
    PART A removes the database-level CHECK (reserved_stock <= total_stock)
    so the application-level race is visible on its own. PART B restores it
    and replays the identical naive code: the race still happens, but the
    database refuses every increment past total_stock, so no oversell.
    """
    admin = SessionLocal()
    admin.execute(text("ALTER TABLE inventory DROP CONSTRAINT IF EXISTS ck_inventory_no_oversell"))
    admin.commit()
    try:
        confirmed = _fire("PART A -- naive checkout, database backstop REMOVED")
        print(f"\nStock available was 1. Confirmed orders: {confirmed}")
        if confirmed > 1:
            print("OVERSOLD -- this is the lost-update problem in action.")
        else:
            print("No oversell this run (race conditions are timing-dependent -- re-run a few times).")
    finally:
        admin.execute(text("UPDATE inventory SET reserved_stock = 0, total_stock = 1 WHERE sale_id = :sid"), {"sid": SALE_ID})
        admin.execute(text("ALTER TABLE inventory ADD CONSTRAINT ck_inventory_no_oversell CHECK (reserved_stock <= total_stock)"))
        admin.commit()
        admin.close()

    print("\n" + "=" * 70)
    confirmed = _fire("PART B -- same naive code, CHECK ck_inventory_no_oversell RESTORED")
    print(f"\nStock available was 1. Confirmed orders: {confirmed}")
    print("The code is just as racy, but the database itself refused every")
    print("increment past total_stock. Correct strategies (demo_2/3/4) avoid the")
    print("wasted work; the CHECK constraint guarantees the DATA stays valid even")
    print("if a handler has a locking bug.")


if __name__ == "__main__":
    run()
