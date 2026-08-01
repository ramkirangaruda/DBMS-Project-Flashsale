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
from scripts.seed_ids import SALE_ID, USER_IDS


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
    finally:
        db.close()


def run():
    # Reset reserved_stock to 0 and total_stock to a very scarce number
    # so the race is easy to trigger and easy to see.
    reset_db = SessionLocal()
    reset_db.execute(text("UPDATE inventory SET reserved_stock = 0, total_stock = 1 WHERE sale_id = :sid"), {"sid": SALE_ID})
    reset_db.commit()
    reset_db.close()

    N = 8  # 8 concurrent buyers competing for 1 unit
    threads = []
    results = [None] * N
    print(f"Firing {N} concurrent checkout attempts at 1 unit of stock (no locking)...\n")
    for i in range(N):
        t = threading.Thread(target=naive_checkout, args=(USER_IDS[i], results, i))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    confirmed = sum(1 for r in results if r and r.startswith("CONFIRMED"))
    for i, r in enumerate(results):
        print(f"  buyer {i}: {r}")

    print(f"\nStock available was 1. Confirmed orders: {confirmed}")
    if confirmed > 1:
        print("OVERSOLD -- this is the lost-update problem in action.")
    else:
        print("No oversell this run (race conditions are timing-dependent -- "
              "re-run a few times, or see demo_2/3/4 for guaranteed-correct versions).")


if __name__ == "__main__":
    run()
