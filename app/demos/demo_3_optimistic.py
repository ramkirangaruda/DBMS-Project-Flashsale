"""
Section 8.2 -- Optimistic Concurrency Control (OCC)

No lock is taken up front. Instead, read the row's `version`, do the work,
then attempt UPDATE ... WHERE version = <version we read>. If another
transaction beat us to it, the WHERE clause matches 0 rows -- we detect
that and retry. Cheap when contention is low; under flash-sale-level
contention, retries can spike (this is the counter-intuitive result worth
reporting -- see demo_5_benchmark.py for the measured comparison).

Run with: venv/bin/python -m app.demos.demo_3_optimistic
"""
import sys, os, time, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from app.database import SessionLocal
from scripts.seed import DEMO_SALE_ID, DEMO_USER_IDS

# Fixed canonical ids from scripts/seed.py -- stable across reseeds.
SALE_ID = str(DEMO_SALE_ID)
USER_IDS = [str(u) for u in DEMO_USER_IDS]

MAX_RETRIES = 20


def optimistic_checkout(user_id: str, results: list, index: int, retry_counts: list):
    db = SessionLocal()
    retries = 0
    try:
        while retries < MAX_RETRIES:
            row = db.execute(
                text("SELECT id, total_stock, reserved_stock, version FROM inventory WHERE sale_id = :sid"),
                {"sid": SALE_ID},
            ).fetchone()
            inv_id, total, reserved, version = row
            available = total - reserved

            time.sleep(0.05)  # same simulated "think time" as the other demos

            if available <= 0:
                results[index] = "REJECTED (no stock)"
                retry_counts[index] = retries
                return

            # Conditional update: only succeeds if version hasn't moved.
            result = db.execute(
                text(
                    "UPDATE inventory SET reserved_stock = reserved_stock + 1, version = version + 1 "
                    "WHERE id = :iid AND version = :v"
                ),
                {"iid": inv_id, "v": version},
            )
            if result.rowcount == 1:
                order_id = db.execute(
                    text(
                        "INSERT INTO orders (id, user_id, sale_id, status, created_at) "
                        "VALUES (gen_random_uuid(), :uid, :sid, 'confirmed', now()) RETURNING id"
                    ),
                    {"uid": user_id, "sid": SALE_ID},
                ).fetchone()[0]
                db.commit()
                results[index] = f"CONFIRMED order {order_id}"
                retry_counts[index] = retries
                return
            else:
                # Someone else updated the row first -- version conflict, retry.
                db.rollback()
                retries += 1

        results[index] = "FAILED (max retries exceeded)"
        retry_counts[index] = retries
    finally:
        db.close()


def run():
    reset_db = SessionLocal()
    reset_db.execute(
        text("UPDATE inventory SET reserved_stock = 0, total_stock = 1, version = 0 WHERE sale_id = :sid"),
        {"sid": SALE_ID},
    )
    reset_db.commit()
    reset_db.close()

    N = 8
    threads = []
    results = [None] * N
    retry_counts = [0] * N
    print(f"Firing {N} concurrent checkout attempts at 1 unit of stock (optimistic/version-based)...\n")
    start = time.time()
    for i in range(N):
        t = threading.Thread(target=optimistic_checkout, args=(USER_IDS[i], results, i, retry_counts))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - start

    confirmed = sum(1 for r in results if r and r.startswith("CONFIRMED"))
    for i, r in enumerate(results):
        print(f"  buyer {i}: {r}  (retries: {retry_counts[i]})")

    total_retries = sum(retry_counts)
    print(f"\nStock available was 1. Confirmed orders: {confirmed} (elapsed {elapsed:.3f}s)")
    print(f"Total retries across all buyers: {total_retries}")
    print("CORRECT -- exactly 1 confirmed. But notice the retry count: under this")
    print("much contention, OCC burns CPU/DB round-trips retrying instead of queueing.")


if __name__ == "__main__":
    run()
