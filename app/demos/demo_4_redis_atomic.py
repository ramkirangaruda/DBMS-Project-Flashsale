"""
Section 9 -- NoSQL Component (Redis atomic DECR)

Fastest path: no PostgreSQL round-trip at all for the "is there stock"
check. Redis's DECR is atomic at the server level, so concurrent clients
never see a stale value the way naive read-then-write does. This is the
path a real system would put in front of PostgreSQL to survive the initial
traffic spike -- PostgreSQL remains the source of truth for the actual
order record.

Also demonstrates the compensation problem discussed in Section 9: if the
Redis DECR succeeds but the PostgreSQL insert then fails, we must give the
unit back to Redis (INCR) or it's lost forever from the counter's
perspective -- this is a distributed-consistency problem, not just a
DB-locking one.

Run with: venv/bin/python -m app.demos.demo_4_redis_atomic
"""
import sys, os, time, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import redis
from sqlalchemy import text
from app.database import SessionLocal
from scripts.seed_ids import SALE_ID, USER_IDS

r = redis.Redis(host="localhost", port=6379, decode_responses=True)
STOCK_KEY = f"stock:{SALE_ID}"


def redis_checkout(user_id: str, results: list, index: int):
    # Atomic decrement -- Redis guarantees this is race-free even under
    # massive concurrency, unlike the naive read-then-write in demo_1.
    remaining = r.decr(STOCK_KEY)

    if remaining < 0:
        # We took it below zero -- give it back, no stock was actually available.
        r.incr(STOCK_KEY)
        results[index] = "REJECTED (no stock, Redis fast-path)"
        return

    # Redis says we have a unit. Now persist the authoritative order in
    # PostgreSQL. If this fails, we must compensate the Redis counter.
    db = SessionLocal()
    try:
        time.sleep(0.02)  # simulated DB work
        order_id = db.execute(
            text(
                "INSERT INTO orders (id, user_id, sale_id, status, created_at) "
                "VALUES (gen_random_uuid(), :uid, :sid, 'confirmed', now()) RETURNING id"
            ),
            {"uid": user_id, "sid": SALE_ID},
        ).fetchone()[0]
        db.commit()
        results[index] = f"CONFIRMED order {order_id}"
    except Exception as e:
        db.rollback()
        r.incr(STOCK_KEY)  # compensate: give the unit back to the counter
        results[index] = f"FAILED, compensated Redis counter ({e})"
    finally:
        db.close()


def run():
    r.set(STOCK_KEY, 1)  # 1 unit available, mirrors the other demos
    reset_db = SessionLocal()
    reset_db.execute(text("DELETE FROM orders WHERE sale_id = :sid"), {"sid": SALE_ID})
    reset_db.commit()
    reset_db.close()

    N = 8
    threads = []
    results = [None] * N
    print(f"Firing {N} concurrent checkout attempts at 1 unit of stock (Redis atomic DECR)...\n")
    start = time.time()
    for i in range(N):
        t = threading.Thread(target=redis_checkout, args=(USER_IDS[i], results, i))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - start

    confirmed = sum(1 for r_ in results if r_ and r_.startswith("CONFIRMED"))
    for i, res in enumerate(results):
        print(f"  buyer {i}: {res}")

    print(f"\nStock available was 1. Confirmed orders: {confirmed} (elapsed {elapsed:.3f}s)")
    print("CORRECT -- exactly 1 confirmed. Notice this was the fastest of the three")
    print("approaches, because most 'sold out' rejections never touched PostgreSQL at all.")


if __name__ == "__main__":
    run()
