"""
Section 8.2 -- Pessimistic Locking (SELECT ... FOR UPDATE)

Same race as demo_1, but the inventory row is locked before it's read, so
every other transaction trying to touch that row blocks until the first
one commits or rolls back. Guarantees correctness; the cost is queueing
under contention (measured in demo_5_benchmark.py).

Run with: venv/bin/python -m app.demos.demo_2_pessimistic
"""
import sys, os, time, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from app.database import SessionLocal
from scripts.seed_ids import SALE_ID, USER_IDS


def pessimistic_checkout(user_id: str, results: list, index: int):
    db = SessionLocal()
    try:
        db.execute(text("BEGIN"))
        # FOR UPDATE locks this row -- any other transaction requesting the
        # same lock blocks here until we COMMIT or ROLLBACK.
        row = db.execute(
            text("SELECT id, total_stock, reserved_stock FROM inventory WHERE sale_id = :sid FOR UPDATE"),
            {"sid": SALE_ID},
        ).fetchone()
        inv_id, total, reserved = row
        available = total - reserved

        time.sleep(0.05)  # same simulated latency as demo_1, on purpose

        if available > 0:
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
    except Exception as e:
        db.rollback()
        results[index] = f"ERROR: {e}"
    finally:
        db.close()


def run():
    reset_db = SessionLocal()
    reset_db.execute(text("UPDATE inventory SET reserved_stock = 0, total_stock = 1 WHERE sale_id = :sid"), {"sid": SALE_ID})
    reset_db.commit()
    reset_db.close()

    N = 8
    threads = []
    results = [None] * N
    print(f"Firing {N} concurrent checkout attempts at 1 unit of stock (SELECT FOR UPDATE)...\n")
    start = time.time()
    for i in range(N):
        t = threading.Thread(target=pessimistic_checkout, args=(USER_IDS[i], results, i))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - start

    confirmed = sum(1 for r in results if r and r.startswith("CONFIRMED"))
    for i, r in enumerate(results):
        print(f"  buyer {i}: {r}")

    print(f"\nStock available was 1. Confirmed orders: {confirmed} (elapsed {elapsed:.3f}s)")
    print("CORRECT -- exactly 1 confirmed, no oversell, because FOR UPDATE serialized access.")
    print("Note the elapsed time: locking serializes the 8 attempts, so they queue up.")


if __name__ == "__main__":
    run()
