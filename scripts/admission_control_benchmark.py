"""
Benchmark: does the adaptive (AIMD) admission controller find a good admission
rate without being told one?

Each ARM starts a fresh API process with its own settings, runs the same herd
of buyers through the real waiting room and the real checkout endpoint, and
reports what the database experienced. Arms:

    static:K   the original behaviour, K buyers admitted per 500 ms tick
    aimd       ADMISSION_CONTROL=aimd, starting from a tiny batch and finding
               its own rate (app/admission_control.py)

    python -m scripts.admission_control_benchmark \
        --strategy pessimistic --stock 1500 \
        --arms static:5,static:20,static:80,static:320,aimd \
        --repeats 3 --out results/aimd_pessimistic_native.json

    # make every inventory UPDATE slower, to move the knee (benchmark-only DB
    # trigger, created and dropped by this script):
    python -m scripts.admission_control_benchmark ... --db-delay-ms 15

HOW THE HERD IS DRIVEN, AND TWO DELIBERATE SHORTCUTS
    1. The herd is PLACED in the waiting room with one pipelined ZADD (the same
       keys and scores POST /queue/join writes) instead of ~2,000 simultaneous
       HTTP joins. On two shared cores the join storm alone took ~15 s and
       dominated the first third of every run, burying what is being measured
       (admission control of CHECKOUT). The join burst has its own regression
       test, tests/test_queue_burst.py; `--join-mode http` restores it.
    2. Grant polling, below.

    Real clients poll GET /queue/status. Doing that for ~2,000 buyers from a
    Python process that shares two CPU cores with the server would measure the
    load generator, not the system. So the generator READS GRANTS DIRECTLY FROM
    REDIS (one MGET every 50 ms) and then hits /queue/join and /checkout/* over
    real HTTP exactly as a client would. Everything the controller observes --
    admission, checkout latency, version conflicts -- is real; only the
    polling of the status endpoint is elided. It is a shortcut, and it is
    stated in the write-up.

WHAT IS MEASURED (client side, so it cannot flatter the server)
    checkout latency p50/p95/p99     per attempt, excluding queue wait
    peak concurrent checkouts        how hard the database was actually hit
    time to sell out                 first join -> last confirmed order
    throughput                       confirmed orders / second while selling
    units sold vs stock              LIVENESS. The invariant (never oversell)
                                     is checked too, but it is the weaker one:
                                     results/horizontal_scaling_notes.md found
                                     a strategy that never oversold and still
                                     left 22% of the stock unsold.
    5xx / timeouts / expired tokens  what overload costs the buyers
"""
import argparse
import asyncio
import json
import os
import random
import statistics
import subprocess
import sys
import time
import uuid

import httpx
import redis
import redis.asyncio as aioredis
from sqlalchemy import text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.database import SessionLocal                       # noqa: E402
from app.queue import GRANT_KEY                             # noqa: E402
from app.redisconf import make_redis_client                 # noqa: E402

BASE = os.getenv("BENCH_BASE", "http://127.0.0.1:8010")
SALE_ID = os.getenv("BENCH_SALE_ID", "22222222-2222-2222-2222-222222222222")
MAX_TRIES = 30


# ----------------------------------------------------------------- plumbing
def sh_db(sql, **params):
    db = SessionLocal()
    try:
        out = db.execute(text(sql), params)
        rows = out.fetchall() if out.returns_rows else None
        db.commit()
        return rows
    finally:
        db.close()


def user_ids():
    return [str(r[0]) for r in sh_db("SELECT id FROM users ORDER BY email LIMIT 30")]


def clean_redis(r):
    for pat in ("queue:z:*", "queue:granted:*", "queue:tokens:*", "admission:*",
                "admctl:*", "stock:*", "idempotency:*"):
        for k in r.scan_iter(match=pat, count=1000):
            r.delete(k)


def start_server(arm, strategy, log_path):
    env = dict(os.environ, OTEL_SDK_DISABLED="true", ADMISSION_ENFORCED="true",
               QUEUE_ADMIT_INTERVAL="0.5", PYTHONPATH=ROOT)
    if arm.startswith("static:"):
        env["ADMISSION_CONTROL"] = "static"
        env["QUEUE_ADMIT_BATCH"] = arm.split(":")[1]
    else:
        # "aimd" or "aimd:LO-HI" -- the latency target range in ms
        env["ADMISSION_CONTROL"] = "aimd"
        env["QUEUE_ADMIT_BATCH"] = "10"
        if ":" in arm:
            lo, hi = arm.split(":")[1].split("-")
            env["AIMD_TARGET_MIN_MS"], env["AIMD_TARGET_MAX_MS"] = lo, hi
    code = ("import uvicorn; uvicorn.run('app.main:app', host='127.0.0.1', "
            "port=8010, log_level='warning')")
    log = open(log_path, "w")
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            if httpx.get(f"{BASE}/api", timeout=1).status_code == 200:
                return proc
        except Exception:                                      # noqa: BLE001
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError(f"server for arm {arm} did not start; see {log_path}")


def stop_server(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def server_latency(strategy):
    """p50/p95/p99 of checkout_latency_seconds as the SERVER measured it
    (app/metrics.py), read from /metrics before the process is stopped. This
    excludes the load generator's own queueing, so it is the fairer number for
    'how long did the database make buyers wait'. Interpolated linearly inside
    the histogram bucket."""
    try:
        txt = httpx.get(f"{BASE}/metrics", timeout=10).text
    except Exception:                                          # noqa: BLE001
        return None
    pts = []
    for line in txt.splitlines():
        if line.startswith("checkout_latency_seconds_bucket") and f'strategy="{strategy}"' in line:
            le = line.split('le="')[1].split('"')[0]
            pts.append((float("inf") if le == "+Inf" else float(le), float(line.rsplit(" ", 1)[1])))
    pts.sort()
    if not pts or pts[-1][1] == 0:
        return None
    total = pts[-1][1]

    def q(p):
        target, prev_edge, prev_c = p * total, 0.0, 0.0
        for edge, c in pts:
            if c >= target:
                if edge == float("inf"):
                    return round(prev_edge * 1000, 1)
                frac = (target - prev_c) / max(1e-9, c - prev_c)
                return round((prev_edge + frac * (edge - prev_edge)) * 1000, 1)
            prev_edge, prev_c = edge, c
        return None
    return {"p50": q(0.5), "p95": q(0.95), "p99": q(0.99), "count": int(total)}


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


# ----------------------------------------------------------------- the herd
class Buyer:
    __slots__ = ("i", "user", "qt", "token", "t_join", "t_grant", "outcome", "t_done",
                 "lats", "retries")

    def __init__(self, i, user):
        self.i, self.user = i, user
        self.qt = self.token = None
        self.t_join = self.t_grant = self.t_done = None
        self.outcome = "pending"
        self.lats = []
        self.retries = 0


async def run_herd(arm, strategy, n_buyers, stock, users, join_mode="redis"):
    ar = aioredis.Redis(host="localhost", port=int(os.getenv("REDIS_PORT", "6390")),
                        decode_responses=True)
    limits = httpx.Limits(max_connections=4000, max_keepalive_connections=4000)
    inflight = {"now": 0, "peak": 0, "sum": 0, "samples": 0}
    traj = []
    async with httpx.AsyncClient(base_url=BASE, timeout=90, limits=limits) as c:
        buyers = [Buyer(i, users[i % len(users)]) for i in range(n_buyers)]
        t0 = time.perf_counter()

        if join_mode == "http":
            async def join(b):
                r = await c.post(f"/queue/join/{SALE_ID}")
                b.qt = r.json()["queue_token"]
                b.t_join = time.perf_counter()

            await asyncio.gather(*[join(b) for b in buyers])
        else:
            # The herd is already in the waiting room when the drop opens:
            # one pipelined ZADD with the exact keys/scores POST /queue/join
            # writes. See the module docstring for why the join storm itself
            # is not part of this measurement (it is covered by
            # tests/test_queue_burst.py).
            from app.queue import Z_KEY
            pipe = ar.pipeline()
            base = time.time()
            for b in buyers:
                b.qt = uuid.uuid4().hex
                pipe.zadd(Z_KEY.format(sale_id=SALE_ID), {b.qt: base + b.i * 1e-6})
            await pipe.execute()
            now = time.perf_counter()
            for b in buyers:
                b.t_join = now

        async def sampler():
            while True:
                inflight["sum"] += inflight["now"]
                inflight["samples"] += 1
                if inflight["samples"] % 10 == 0:               # 1 Hz
                    entry = {"t": round(time.perf_counter() - t0, 1),
                             "inflight": inflight["now"]}
                    if arm.startswith("aimd"):
                        try:
                            st = (await c.get("/queue/control")).json().get("state") or {}
                            entry.update(cwnd=round(st.get("cwnd", 0), 1),
                                         phase=st.get("phase"), signal=st.get("last_signal"),
                                         q_ms=st.get("last_q_ms"),
                                         tput=st.get("throughput"))
                        except Exception:                      # noqa: BLE001
                            pass
                    traj.append(entry)
                await asyncio.sleep(0.1)

        async def checkout(b):
            for _ in range(MAX_TRIES):
                inflight["now"] += 1
                inflight["peak"] = max(inflight["peak"], inflight["now"])
                t = time.perf_counter()
                try:
                    r = await c.post(f"/checkout/{strategy}",
                                     params={"sale_id": SALE_ID, "user_id": b.user},
                                     headers={"X-Admission-Token": b.token})
                except httpx.TimeoutException:
                    b.outcome, b.t_done = "timeout", time.perf_counter()
                    return
                except Exception:                              # noqa: BLE001
                    b.outcome, b.t_done = "client_error", time.perf_counter()
                    return
                finally:
                    inflight["now"] -= 1
                b.lats.append((time.perf_counter() - t) * 1000)
                try:
                    j = r.json()
                except Exception:                              # noqa: BLE001
                    j = {}
                st = j.get("status")
                if st == "confirmed":
                    b.outcome = "confirmed"
                elif st == "sold_out":
                    b.outcome = "sold_out"
                elif r.status_code == 403:
                    b.outcome = "expired"
                elif r.status_code >= 500:
                    b.outcome = "error_5xx"
                elif st == "failed" and r.status_code == 409:
                    b.retries += 1                             # OCC conflict: try again
                    await asyncio.sleep(random.uniform(0, 0.02))
                    continue
                else:
                    b.outcome = "other"
                b.t_done = time.perf_counter()
                return
            b.outcome, b.t_done = "gave_up", time.perf_counter()

        samp = asyncio.create_task(sampler())
        pending = {b.qt: b for b in buyers}
        tasks = []
        deadline = time.perf_counter() + 240
        while pending and time.perf_counter() < deadline:
            keys = list(pending)
            grants = await ar.mget([GRANT_KEY.format(sale_id=SALE_ID, queue_token=k) for k in keys])
            now = time.perf_counter()
            for k, g in zip(keys, grants):
                if g:
                    b = pending.pop(k)
                    b.token, b.t_grant = g, now
                    tasks.append(asyncio.create_task(checkout(b)))
            await asyncio.sleep(0.05)
        await asyncio.gather(*tasks, return_exceptions=True)
        samp.cancel()
        t_end = time.perf_counter()
    await ar.aclose()

    done = [b for b in buyers if b.outcome != "pending"]
    conf = [b for b in done if b.outcome == "confirmed"]
    lats = [x for b in buyers for x in b.lats]
    waits = [(b.t_grant - b.t_join) for b in buyers if b.t_grant]
    sell_window = (max(b.t_done for b in conf) - min(b.t_done for b in conf)) if len(conf) > 1 else 0
    outcomes = {}
    for b in buyers:
        outcomes[b.outcome] = outcomes.get(b.outcome, 0) + 1
    return {
        "arm": arm, "strategy": strategy, "buyers": n_buyers, "stock": stock,
        "confirmed": len(conf),
        "units_sold_pct": round(100 * len(conf) / stock, 1),
        "outcomes": outcomes,
        "time_to_sell_out_s": round(max((b.t_done for b in conf), default=t_end) - t0, 2),
        "throughput_orders_per_s": round(len(conf) / sell_window, 1) if sell_window else None,
        "checkout_latency_ms": {"p50": round(pct(lats, 50), 1), "p95": round(pct(lats, 95), 1),
                                "p99": round(pct(lats, 99), 1)} if lats else None,
        "queue_wait_s": {"p50": round(pct(waits, 50), 2), "p95": round(pct(waits, 95), 2)} if waits else None,
        "peak_concurrent_checkouts": inflight["peak"],
        "mean_concurrent_checkouts": round(inflight["sum"] / max(1, inflight["samples"]), 1),
        "occ_retries": sum(b.retries for b in buyers),
        "trajectory": traj,
    }


# ----------------------------------------------------------------- one arm
def run_arm(arm, args, users, r, rep):
    log_path = os.path.join(ROOT, "results", f".bench_server_{arm.replace(':', '_')}.log")
    clean_redis(r)
    sh_db("UPDATE flash_sale_events SET start_time = now() - interval '1 hour', "
          "end_time = now() + interval '3 hours' WHERE id = :s", s=SALE_ID)
    proc = start_server(arm, args.strategy, log_path)
    try:
        httpx.post(f"{BASE}/admin/reset-sale/{SALE_ID}", params={"stock": args.stock}, timeout=30)
        httpx.post(f"{BASE}/queue/reset/{SALE_ID}", timeout=30)
        httpx.post(f"{BASE}/queue/control/reset", timeout=30)
        before = sh_db("SELECT count(*) FROM orders WHERE sale_id = :s", s=SALE_ID)[0][0]
        n = int(args.stock * args.oversubscribe)
        res = asyncio.run(run_herd(arm, args.strategy, n, args.stock, users, args.join_mode))
        after = sh_db("SELECT count(*) FROM orders WHERE sale_id = :s", s=SALE_ID)[0][0]
        reserved = sh_db("SELECT reserved_stock, total_stock FROM inventory WHERE sale_id = :s",
                         s=SALE_ID)[0]
        res["server_checkout_latency_ms"] = server_latency(args.strategy)
        res["db_orders_created"] = after - before
        res["db_reserved_stock"] = reserved[0]
        res["invariant_ok"] = (after - before) <= args.stock and reserved[0] <= reserved[1]
        res["rep"] = rep
        return res
    finally:
        stop_server(proc)


def summarise(runs):
    """Median across repeats, per metric."""
    def med(f):
        xs = [f(r) for r in runs if f(r) is not None]
        return round(statistics.median(xs), 1) if xs else None
    return {
        "arm": runs[0]["arm"], "reps": len(runs),
        "units_sold_pct": med(lambda r: r["units_sold_pct"]),
        "time_to_sell_out_s": med(lambda r: r["time_to_sell_out_s"]),
        "throughput": med(lambda r: r["throughput_orders_per_s"]),
        "p50_ms": med(lambda r: r["checkout_latency_ms"]["p50"]),
        "p95_ms": med(lambda r: r["checkout_latency_ms"]["p95"]),
        "p99_ms": med(lambda r: r["checkout_latency_ms"]["p99"]),
        "srv_p50_ms": med(lambda r: (r.get("server_checkout_latency_ms") or {}).get("p50")),
        "srv_p95_ms": med(lambda r: (r.get("server_checkout_latency_ms") or {}).get("p95")),
        "peak_concurrent": med(lambda r: r["peak_concurrent_checkouts"]),
        "5xx": med(lambda r: r["outcomes"].get("error_5xx", 0) + r["outcomes"].get("timeout", 0)),
        "expired": med(lambda r: r["outcomes"].get("expired", 0)),
        "occ_retries": med(lambda r: r["occ_retries"]),
        "invariant_ok": all(r["invariant_ok"] for r in runs),
    }


def markdown(summaries, args):
    L = [f"### strategy={args.strategy}, stock={args.stock}, buyers={int(args.stock * args.oversubscribe)}, "
         f"db_delay={args.db_delay_ms} ms, median of {args.repeats}", "",
         "| arm | sold % | sell-out s | orders/s | server p50 ms | server p95 ms | client p95 ms | peak conc. | 5xx+timeout | expired | OCC retries | invariant |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in summaries:
        L.append(f"| {s['arm']} | {s['units_sold_pct']} | {s['time_to_sell_out_s']} | {s['throughput']} | "
                 f"{s['srv_p50_ms']} | {s['srv_p95_ms']} | {s['p95_ms']} | {s['peak_concurrent']} | "
                 f"{s['5xx']} | {s['expired']} | {s['occ_retries']} | {'ok' if s['invariant_ok'] else 'VIOLATED'} |")
    return "\n".join(L)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", default="pessimistic", choices=["pessimistic", "optimistic"])
    p.add_argument("--stock", type=int, default=1500)
    p.add_argument("--oversubscribe", type=float, default=1.15)
    p.add_argument("--arms", default="static:5,static:20,static:80,static:320,aimd")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--db-delay-ms", type=float, default=0.0,
                   help="benchmark-only trigger adding pg_sleep to every inventory UPDATE")
    p.add_argument("--join-mode", default="redis", choices=["redis", "http"],
                   help="redis: herd pre-placed in the queue (default); http: every buyer calls "
                        "POST /queue/join at t0 (loads the server with the join storm too)")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    r = make_redis_client()
    users = user_ids()
    if not users:
        sys.exit("no users; run python -m scripts.seed first")

    if args.db_delay_ms > 0:
        sh_db("CREATE OR REPLACE FUNCTION bench_delay() RETURNS trigger AS $$ "
              f"BEGIN PERFORM pg_sleep({args.db_delay_ms / 1000.0}); RETURN NEW; END $$ LANGUAGE plpgsql")
        sh_db("DROP TRIGGER IF EXISTS bench_delay ON inventory")
        sh_db("CREATE TRIGGER bench_delay BEFORE UPDATE ON inventory "
              "FOR EACH ROW EXECUTE FUNCTION bench_delay()")
    all_runs = {}
    try:
        for rep in range(args.repeats):
            for arm in args.arms.split(","):          # interleaved, so drift hits every arm
                print(f"[rep {rep + 1}/{args.repeats}] {arm} ...", flush=True)
                res = run_arm(arm, args, users, r, rep)
                all_runs.setdefault(arm, []).append(res)
                print(f"    sold {res['units_sold_pct']}%  sell-out {res['time_to_sell_out_s']}s  "
                      f"p95 {res['checkout_latency_ms']['p95']}ms  peak {res['peak_concurrent_checkouts']}  "
                      f"{res['outcomes']}", flush=True)
    finally:
        if args.db_delay_ms > 0:
            sh_db("DROP TRIGGER IF EXISTS bench_delay ON inventory")
            sh_db("DROP FUNCTION IF EXISTS bench_delay()")
        clean_redis(r)

    summaries = [summarise(v) for v in all_runs.values()]
    print()
    print(markdown(summaries, args))
    if args.out:
        with open(os.path.join(ROOT, args.out), "w") as f:
            json.dump({"args": vars(args), "summary": summaries, "runs": all_runs}, f, indent=1)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
