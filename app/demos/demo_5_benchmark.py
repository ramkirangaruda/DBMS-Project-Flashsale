"""
Section 8.2 -- the three-way concurrency benchmark. This is the centerpiece
result of the whole project: measuring oversell rate and throughput for
pessimistic locking, optimistic concurrency control, and Redis atomic DECR,
all under the same simulated contention.

Run with: venv/bin/python -m app.demos.demo_5_benchmark
"""
import sys, os, time, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import redis
from sqlalchemy import text
from app.database import SessionLocal
from scripts.seed_ids import SALE_ID, USER_IDS

N_BUYERS = 40          # concurrent checkout attempts
STOCK = 5               # units available -- heavy contention (8x oversubscribed)
THINK_TIME = 0.03       # simulated per-request work, seconds

r = redis.Redis(host="localhost", port=6379, decode_responses=True)


def reset(stock=STOCK):
    db = SessionLocal()
    db.execute(
        text("UPDATE inventory SET reserved_stock = 0, total_stock = :s, version = 0 WHERE sale_id = :sid"),
        {"s": stock, "sid": SALE_ID},
    )
    db.execute(text("DELETE FROM orders WHERE sale_id = :sid"), {"sid": SALE_ID})
    db.commit()
    db.close()
    r.set(f"stock:{SALE_ID}", stock)


def buyer_ids(n):
    # cycle through seeded users if N_BUYERS > len(USER_IDS)
    return [USER_IDS[i % len(USER_IDS)] for i in range(n)]


# ---------- Strategy implementations (condensed from demo_2/3/4) ----------

def pessimistic_worker(user_id, out, i):
    db = SessionLocal()
    try:
        db.execute(text("BEGIN"))
        row = db.execute(text("SELECT id, total_stock, reserved_stock FROM inventory WHERE sale_id = :sid FOR UPDATE"), {"sid": SALE_ID}).fetchone()
        inv_id, total, reserved = row
        time.sleep(THINK_TIME)
        if total - reserved > 0:
            db.execute(text("UPDATE inventory SET reserved_stock = reserved_stock + 1 WHERE id = :iid"), {"iid": inv_id})
            db.execute(text("INSERT INTO orders (id, user_id, sale_id, status, created_at) VALUES (gen_random_uuid(), :uid, :sid, 'confirmed', now())"), {"uid": user_id, "sid": SALE_ID})
            db.commit()
            out[i] = ("confirmed", 0)
        else:
            db.rollback()
            out[i] = ("rejected", 0)
    except Exception:
        db.rollback()
        out[i] = ("error", 0)
    finally:
        db.close()


def optimistic_worker(user_id, out, i, max_retries=30):
    db = SessionLocal()
    retries = 0
    try:
        while retries < max_retries:
            row = db.execute(text("SELECT id, total_stock, reserved_stock, version FROM inventory WHERE sale_id = :sid"), {"sid": SALE_ID}).fetchone()
            inv_id, total, reserved, version = row
            time.sleep(THINK_TIME)
            if total - reserved <= 0:
                out[i] = ("rejected", retries)
                return
            result = db.execute(text("UPDATE inventory SET reserved_stock = reserved_stock + 1, version = version + 1 WHERE id = :iid AND version = :v"), {"iid": inv_id, "v": version})
            if result.rowcount == 1:
                db.execute(text("INSERT INTO orders (id, user_id, sale_id, status, created_at) VALUES (gen_random_uuid(), :uid, :sid, 'confirmed', now())"), {"uid": user_id, "sid": SALE_ID})
                db.commit()
                out[i] = ("confirmed", retries)
                return
            else:
                db.rollback()
                retries += 1
        out[i] = ("failed_max_retries", retries)
    finally:
        db.close()


def redis_worker(user_id, out, i):
    remaining = r.decr(f"stock:{SALE_ID}")
    if remaining < 0:
        r.incr(f"stock:{SALE_ID}")
        out[i] = ("rejected", 0)
        return
    db = SessionLocal()
    try:
        time.sleep(THINK_TIME)
        db.execute(text("INSERT INTO orders (id, user_id, sale_id, status, created_at) VALUES (gen_random_uuid(), :uid, :sid, 'confirmed', now())"), {"uid": user_id, "sid": SALE_ID})
        db.commit()
        out[i] = ("confirmed", 0)
    except Exception:
        db.rollback()
        r.incr(f"stock:{SALE_ID}")
        out[i] = ("error", 0)
    finally:
        db.close()


def run_strategy(name, worker_fn):
    reset()
    ids = buyer_ids(N_BUYERS)
    out = [None] * N_BUYERS
    threads = [threading.Thread(target=worker_fn, args=(ids[i], out, i)) for i in range(N_BUYERS)]
    start = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.time() - start

    confirmed = sum(1 for o in out if o and o[0] == "confirmed")
    rejected = sum(1 for o in out if o and o[0] == "rejected")
    errors = sum(1 for o in out if o and o[0] not in ("confirmed", "rejected"))
    total_retries = sum(o[1] for o in out if o)
    oversold = max(0, confirmed - STOCK)
    throughput = N_BUYERS / elapsed if elapsed > 0 else float("inf")

    return {
        "name": name,
        "confirmed": confirmed,
        "rejected": rejected,
        "errors": errors,
        "oversold": oversold,
        "total_retries": total_retries,
        "elapsed_s": round(elapsed, 3),
        "throughput_req_s": round(throughput, 1),
    }


def run():
    print(f"Benchmark: {N_BUYERS} concurrent buyers competing for {STOCK} units, "
          f"~{THINK_TIME*1000:.0f}ms simulated work per request.\n")

    results = [
        run_strategy("Pessimistic (SELECT FOR UPDATE)", pessimistic_worker),
        run_strategy("Optimistic (version column)", optimistic_worker),
        run_strategy("Redis atomic DECR", redis_worker),
    ]

    header = f"{'Strategy':38} {'Confirmed':>10} {'Oversold':>9} {'Retries':>8} {'Time(s)':>8} {'Req/s':>8}"
    print(header)
    print("-" * len(header))
    for r_ in results:
        print(f"{r_['name']:38} {r_['confirmed']:>10} {r_['oversold']:>9} {r_['total_retries']:>8} {r_['elapsed_s']:>8} {r_['throughput_req_s']:>8}")

    print(f"\nStock was {STOCK}, contested by {N_BUYERS} buyers ({N_BUYERS/STOCK:.0f}x oversubscribed).")
    print("All three strategies should show 0 oversold (all correct) -- the difference")
    print("is HOW they achieve correctness: queueing (pessimistic), retrying (optimistic),")
    print("or avoiding the DB entirely on the hot path (Redis). This table is the")
    print("centerpiece result for the report -- paste it directly into Section 8.2.")


if __name__ == "__main__":
    run()
