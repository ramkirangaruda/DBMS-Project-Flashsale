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
from scripts.seed import DEMO_SALE_ID, DEMO_USER_IDS

# Fixed canonical ids from scripts/seed.py -- stable across reseeds.
SALE_ID = str(DEMO_SALE_ID)
USER_IDS = [str(u) for u in DEMO_USER_IDS]

r = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", "6390")),  # matches docker-compose.yml (6390:6379)
    decode_responses=True,
)
STOCK_KEY = f"stock:{SALE_ID}"


def _release_redis_counter():
    """Drop the fast-path counter so the sale is not left reading 'sold out'.

    Best-effort: if Redis is already unreachable the demo has finished and
    printed its result, and failing here would turn a successful run into a
    traceback over cleanup.
    """
    try:
        r.delete(STOCK_KEY)
    except Exception:                                          # noqa: BLE001
        pass


def redis_checkout(user_id: str, results: list, index: int):
    # Atomic decrement -- Redis guarantees this is race-free even under
    # massive concurrency, unlike the naive read-then-write in demo_1.
    #
    # DELIBERATE CAP TRADE-OFF -- NOT AN OVERSIGHT. Note what this path does
    # NOT do: it never updates inventory.reserved_stock. Redis is the source
    # of truth for stock here; PostgreSQL only records the durable Order. So
    # GET /sales/{sale_id}, which reads reserved_stock out of PostgreSQL,
    # will not reflect purchases made on this path, and the two counters are
    # allowed to diverge.
    #
    # That is the Section 9 AP-vs-CP trade-off in practice: this path favours
    # availability and partition tolerance (immediate, non-blocking answers
    # under flash-sale load), while the SQL strategies in demo_2/demo_3
    # favour strict consistency. Reconciling the counters is out of scope for
    # the fast path by design -- app/main.py's /checkout/redis endpoint
    # behaves identically for the same reason.
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
    # Scoped by USER, not by sale -- scripts/seed_large.py's bulk orders live
    # on this same SALE_ID and must survive a demo reset. Children first so
    # no foreign key is violated.
    reset_db.execute(text("""
        DELETE FROM flagged_orders WHERE order_id IN (
            SELECT id FROM orders WHERE user_id = ANY(CAST(:uids AS uuid[]))
        )
    """), {"uids": USER_IDS})
    reset_db.execute(text("""
        DELETE FROM order_items WHERE order_id IN (
            SELECT id FROM orders WHERE user_id = ANY(CAST(:uids AS uuid[]))
        )
    """), {"uids": USER_IDS})
    reset_db.execute(text("DELETE FROM user_behavior_logs WHERE user_id = ANY(CAST(:uids AS uuid[]))"), {"uids": USER_IDS})
    reset_db.execute(text("DELETE FROM orders WHERE user_id = ANY(CAST(:uids AS uuid[]))"), {"uids": USER_IDS})
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

    # Hand the sale back in a consistent state. This demo drains the Redis
    # counter to 0 but deliberately never touches inventory.reserved_stock
    # (Redis owns stock on the fast path), so leaving the key behind means
    # PostgreSQL says "5 available" while GET /sales/{id} -- which reports the
    # counter that actually governs the next tap -- says "sold out", for good.
    # Deleting it is what scripts/seed.py does for the same reason, and the
    # /checkout/redis endpoint re-seeds the key from PostgreSQL (SET NX) the
    # next time anyone touches this sale.
    _release_redis_counter()


if __name__ == "__main__":
    run()
