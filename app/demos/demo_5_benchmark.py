"""
Section 8.2 -- the three-way concurrency benchmark. This is the centerpiece
result of the whole project: measuring oversell rate and throughput for
pessimistic locking, optimistic concurrency control, and Redis atomic DECR,
all under the same simulated contention.

Also doubles as a data generator for Section 10.2: a fraction of the
simulated buyers are deliberately bot-like (near-zero browsing time,
shared IP, and reusing the shared-device-fingerprint user cluster that
scripts/seed.py already sets up -- the first 6 seeded users), so a
scoring pass (scripts/run_bot_scoring.py) run afterward has something
real to catch, tied to real Order rows from this actual benchmark run.
Every strategy pays the same logging overhead, so the strategy-to-strategy
comparison above stays fair even though the recorded numbers now include
a bit more than pure lock/retry latency.

Run with: venv/bin/python -m app.demos.demo_5_benchmark
"""
import sys, os, time, random, threading
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import redis
from sqlalchemy import text
from app.database import SessionLocal
from scripts.seed_ids import SALE_ID, USER_IDS

N_BUYERS = 40          # concurrent checkout attempts
STOCK = 5               # units available -- heavy contention (8x oversubscribed)
THINK_TIME = 0.03       # simulated per-request work, seconds

# The first 6 seeded users share one device_fingerprint (scripts/seed.py's
# bot-detection cluster) -- reuse that same cluster here as our "bot-like"
# buyers instead of inventing a separate one.
BOT_USER_IDS = set(USER_IDS[:6])

r = redis.Redis(host="localhost", port=6379, decode_responses=True)


def reset(stock=STOCK):
    db = SessionLocal()
    db.execute(
        text("UPDATE inventory SET reserved_stock = 0, total_stock = :s, version = 0 WHERE sale_id = :sid"),
        {"s": stock, "sid": SALE_ID},
    )
    db.execute(text("DELETE FROM flagged_orders WHERE order_id IN (SELECT id FROM orders WHERE sale_id = :sid)"), {"sid": SALE_ID})
    db.execute(text("DELETE FROM user_behavior_logs WHERE sale_id = :sid"), {"sid": SALE_ID})
    db.execute(text("DELETE FROM orders WHERE sale_id = :sid"), {"sid": SALE_ID})
    db.commit()
    db.close()
    r.set(f"stock:{SALE_ID}", stock)


def buyer_ids(n):
    # cycle through seeded users if N_BUYERS > len(USER_IDS)
    return [USER_IDS[i % len(USER_IDS)] for i in range(n)]


def log_behavior(user_id, order_id):
    """
    Records a UserBehaviorLog row for this simulated checkout, on its own
    connection so it survives regardless of the checkout's own commit/
    rollback -- same reasoning as app/main.py's log_checkout_attempt().
    Bot-cluster buyers get a near-zero page_load-to-checkout gap and a
    shared IP; everyone else gets an ordinary human-ish browsing session.
    """
    is_bot = user_id in BOT_USER_IDS
    checkout_time = datetime.now(timezone.utc)
    if is_bot:
        page_load_time = checkout_time - timedelta(milliseconds=random.randint(50, 300))
        ip_address = "203.0.113.66"  # one shared IP across all bot-cluster buyers -- classic farm signature
    else:
        page_load_time = checkout_time - timedelta(milliseconds=random.randint(8_000, 60_000))
        ip_address = f"203.0.113.{random.randint(100, 199)}"
    session_duration_ms = max(0, int((checkout_time - page_load_time).total_seconds() * 1000))

    log_db = SessionLocal()
    try:
        log_db.execute(
            text(
                "INSERT INTO user_behavior_logs "
                "(id, user_id, sale_id, order_id, page_load_time, checkout_time, ip_address, session_duration_ms) "
                "VALUES (gen_random_uuid(), :uid, :sid, :oid, :plt, :ct, :ip, :dur)"
            ),
            {
                "uid": user_id, "sid": SALE_ID, "oid": order_id,
                "plt": page_load_time, "ct": checkout_time, "ip": ip_address, "dur": session_duration_ms,
            },
        )
        log_db.commit()
    finally:
        log_db.close()


# ---------- Strategy implementations (condensed from demo_2/3/4) ----------

def pessimistic_worker(user_id, out, i):
    db = SessionLocal()
    order_id = None
    try:
        db.execute(text("BEGIN"))
        row = db.execute(text("SELECT id, total_stock, reserved_stock FROM inventory WHERE sale_id = :sid FOR UPDATE"), {"sid": SALE_ID}).fetchone()
        inv_id, total, reserved = row
        time.sleep(THINK_TIME)
        if total - reserved > 0:
            db.execute(text("UPDATE inventory SET reserved_stock = reserved_stock + 1 WHERE id = :iid"), {"iid": inv_id})
            order_id = db.execute(
                text("INSERT INTO orders (id, user_id, sale_id, status, created_at) VALUES (gen_random_uuid(), :uid, :sid, 'confirmed', now()) RETURNING id"),
                {"uid": user_id, "sid": SALE_ID},
            ).fetchone()[0]
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
    log_behavior(user_id, order_id)


def optimistic_worker(user_id, out, i, max_retries=30):
    db = SessionLocal()
    order_id = None
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
                order_id = db.execute(
                    text("INSERT INTO orders (id, user_id, sale_id, status, created_at) VALUES (gen_random_uuid(), :uid, :sid, 'confirmed', now()) RETURNING id"),
                    {"uid": user_id, "sid": SALE_ID},
                ).fetchone()[0]
                db.commit()
                out[i] = ("confirmed", retries)
                return
            else:
                db.rollback()
                retries += 1
        out[i] = ("failed_max_retries", retries)
    finally:
        db.close()
        log_behavior(user_id, order_id)


def redis_worker(user_id, out, i):
    order_id = None
    remaining = r.decr(f"stock:{SALE_ID}")
    if remaining < 0:
        r.incr(f"stock:{SALE_ID}")
        out[i] = ("rejected", 0)
        log_behavior(user_id, order_id)
        return
    db = SessionLocal()
    try:
        time.sleep(THINK_TIME)
        order_id = db.execute(
            text("INSERT INTO orders (id, user_id, sale_id, status, created_at) VALUES (gen_random_uuid(), :uid, :sid, 'confirmed', now()) RETURNING id"),
            {"uid": user_id, "sid": SALE_ID},
        ).fetchone()[0]
        db.commit()
        out[i] = ("confirmed", 0)
    except Exception:
        db.rollback()
        r.incr(f"stock:{SALE_ID}")
        order_id = None
        out[i] = ("error", 0)
    finally:
        db.close()
    log_behavior(user_id, order_id)


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
          f"~{THINK_TIME*1000:.0f}ms simulated work per request.")
    print(f"{len(BOT_USER_IDS)} of the buyer identities are the bot-cluster users from scripts/seed.py")
    print("(shared device_fingerprint) -- logged with bot-like session timing so")
    print("scripts/run_bot_scoring.py has real anomalies to catch after this runs.\n")

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
    print("\nNote: only the LAST strategy run above (Redis) is the one whose orders/logs")
    print("survive in the database afterward -- each run_strategy() call resets state for")
    print("the next one. Run `python -m scripts.run_bot_scoring` right after this script")
    print("finishes to score that final batch while it's still in the database.")


if __name__ == "__main__":
    run()
