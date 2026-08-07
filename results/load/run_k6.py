"""
Drive results/load/flashsale.js against the scaled deployment and collect
everything k6 cannot see by itself.

    python results/load/run_k6.py --arm queued
    python results/load/run_k6.py --arm raw        # needs ADMISSION_ENFORCED=false
    python results/load/run_k6.py --report

k6 generates the load. This wrapper does the parts that need a database or a
container runtime:

  * exports real user ids (orders.user_id is a foreign key)
  * resets stock and the queue through the existing admin endpoints
  * counts confirmed orders in PostgreSQL before and after -- the stock
    invariant, still the same ground truth scripts/chaos_verification.py uses
  * samples container CPU and pg_stat_activity DURING the run, which is how
    the "who is actually the bottleneck" question gets answered instead of
    guessed
  * reconstructs peak concurrent checkouts by sweep line over k6's per-request
    JSON, the same method the Python harness used, so the numbers are
    comparable across phases

Nothing here modifies the application. Reads and the two existing admin
endpoints only.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

import httpx
from sqlalchemy import text

from app.database import SessionLocal

BASE_HOST = os.getenv("K6_BASE_URL", "http://127.0.0.1:8080")
BASE_IN_NET = os.getenv("K6_BASE_IN_NET", "http://nginx:80")
NETWORK = os.getenv("K6_NETWORK", "flashsale-engine_default")
K6_IMAGE = os.getenv("K6_IMAGE", "grafana/k6:latest")
STRATEGIES = ["pessimistic", "optimistic", "redis"]
API_CONTAINERS = ["flashsale-engine-api1-1", "flashsale-engine-api2-1",
                  "flashsale-engine-api3-1", "flashsale-engine-nginx-1"]


# --------------------------------------------------------------------------
# Setup the load generator cannot do for itself
# --------------------------------------------------------------------------
def export_users(n):
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT id FROM users ORDER BY created_at NULLS LAST LIMIT :n"),
            {"n": n},
        ).fetchall()
    finally:
        db.close()
    ids = [str(r[0]) for r in rows]
    with open(os.path.join(HERE, "users.json"), "w", encoding="utf-8") as f:
        json.dump(ids, f)
    return ids


def count_confirmed(sale_id):
    """Ground truth for the invariant. Baseline-delta, no clock agreement
    needed between host and container -- same reasoning as
    chaos_verification.count_confirmed_orders()."""
    db = SessionLocal()
    try:
        return db.execute(
            text("SELECT count(*) FROM orders WHERE sale_id = :sid AND status = 'confirmed'"),
            {"sid": sale_id},
        ).scalar() or 0
    finally:
        db.close()


def sales():
    r = httpx.get(f"{BASE_HOST}/sales", timeout=30.0)
    r.raise_for_status()
    return r.json().get("sales", [])


def reset(sale_id, stock):
    httpx.post(f"{BASE_HOST}/admin/reset-sale/{sale_id}",
               params={"stock": stock}, timeout=60.0).raise_for_status()
    try:
        httpx.post(f"{BASE_HOST}/queue/reset/{sale_id}", timeout=60.0)
    except Exception:                                          # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# Resource sampling -- runs in a thread alongside k6
# --------------------------------------------------------------------------
class ResourceSampler(threading.Thread):
    """
    Samples container CPU and PostgreSQL session state while load is running.

    This is the measurement that decides the phase's central question. Peak
    concurrency alone cannot distinguish "the API tier is saturated" from
    "PostgreSQL is saturated" from "the load generator never pushed hard
    enough" -- but CPU per container plus pg_stat_activity does.
    """

    def __init__(self, interval=2.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.samples = []
        # NOT self._stop -- threading.Thread already has a private _stop()
        # method that join() calls, and shadowing it with an Event makes
        # join() raise "'Event' object is not callable".
        self._stop_evt = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def _docker_cpu(self):
        try:
            out = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}}"],
                capture_output=True, text=True, timeout=25).stdout
        except Exception:                                      # noqa: BLE001
            return {}
        cpu = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in API_CONTAINERS:
                try:
                    cpu[parts[0].replace("flashsale-engine-", "").replace("-1", "")] = \
                        float(parts[1].rstrip("%"))
                except ValueError:
                    pass
        return cpu

    def _pg(self):
        sql = ("SELECT count(*), "
               "count(*) FILTER (WHERE state='active'), "
               "count(*) FILTER (WHERE wait_event_type='Lock'), "
               "count(*) FILTER (WHERE state='idle in transaction') "
               "FROM pg_stat_activity WHERE datname='flashsale'")
        try:
            out = subprocess.run(
                ["docker", "compose", "exec", "-T", "postgres",
                 "psql", "-U", "postgres", "-d", "flashsale", "-t", "-A", "-F", ",", "-c", sql],
                capture_output=True, text=True, timeout=25, cwd=ROOT).stdout
            a, b, c, d = out.strip().splitlines()[0].split(",")
            return {"conns": int(a), "active": int(b),
                    "waiting_on_lock": int(c), "idle_in_txn": int(d)}
        except Exception:                                      # noqa: BLE001
            return {}

    def run(self):
        while not self._stop_evt.is_set():
            self.samples.append({"t": round(time.time(), 2),
                                 "cpu": self._docker_cpu(), "pg": self._pg()})
            self._stop_evt.wait(self.interval)

    def summary(self):
        if not self.samples:
            return {}
        cpu_keys = sorted({k for s in self.samples for k in s.get("cpu", {})})
        out = {"samples": len(self.samples), "cpu_peak_pct": {}, "cpu_mean_pct": {}}
        for k in cpu_keys:
            vals = [s["cpu"][k] for s in self.samples if k in s.get("cpu", {})]
            if vals:
                out["cpu_peak_pct"][k] = round(max(vals), 1)
                out["cpu_mean_pct"][k] = round(sum(vals) / len(vals), 1)
        pg = [s["pg"] for s in self.samples if s.get("pg")]
        if pg:
            out["pg_peak"] = {k: max(p[k] for p in pg) for k in pg[0]}
            out["pg_mean_conns"] = round(sum(p["conns"] for p in pg) / len(pg), 1)
        return out


# --------------------------------------------------------------------------
# k6
# --------------------------------------------------------------------------
def run_k6(strategy, sale_id, n, use_queue, nonce, tag):
    summary_name = f"summary_{tag}_{strategy}.json"
    raw_name = f"raw_{tag}_{strategy}.json"
    for f in (summary_name, raw_name):
        p = os.path.join(HERE, f)
        if os.path.exists(p):
            os.remove(p)

    cmd = [
        "docker", "run", "--rm", "--network", NETWORK,
        "-v", f"{HERE}:/scripts",
        "-e", f"BASE_URL={BASE_IN_NET}",
        "-e", f"STRATEGY={strategy}",
        "-e", f"SALE_ID={sale_id}",
        "-e", f"USE_QUEUE={'true' if use_queue else 'false'}",
        "-e", f"N={n}",
        "-e", f"RUN_NONCE={nonce}",
        "-e", f"SUMMARY_PATH=/scripts/{summary_name}",
        K6_IMAGE, "run",
        "--out", f"json=/scripts/{raw_name}",
        "--quiet",
        "/scripts/flashsale.js",
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    wall = time.perf_counter() - t0
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
    return wall, proc.stdout, summary_name, raw_name


def peak_concurrency(raw_path):
    """
    Peak simultaneous checkout requests, by sweep line over k6's per-request
    output -- identical method to StrategyRun.max_checkout_concurrency in the
    Python harness, so the two phases' numbers mean the same thing.

    k6 emits one http_req_duration point per request with the END timestamp
    and the duration, so the interval is [end - duration, end].
    """
    events = []
    if not os.path.exists(raw_path):
        return None
    with open(raw_path, encoding="utf-8") as f:
        for line in f:
            if '"http_req_duration"' not in line:
                continue
            try:
                p = json.loads(line)
            except Exception:                                  # noqa: BLE001
                continue
            if p.get("type") != "Point":
                continue
            d = p.get("data", {})
            if (d.get("tags") or {}).get("phase") != "checkout":
                continue
            end = _ts(d.get("time"))
            if end is None:
                continue
            dur = float(d.get("value", 0)) / 1000.0
            events.append((end - dur, 1))
            events.append((end, -1))
    if not events:
        return None
    events.sort(key=lambda e: (e[0], e[1]))
    cur = peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


def _ts(s):
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:                                          # noqa: BLE001
        return None


def metric(summary, name, field="count", default=None):
    m = (summary.get("metrics") or {}).get(name) or {}
    v = (m.get("values") or {}).get(field)
    return default if v is None else v


def counter(summary, name):
    """k6 omits a Counter from the summary entirely if it never incremented,
    so an absent counter means zero -- not 'unknown'. Reporting it as null
    would make 'no version conflicts at all' look like a failed measurement."""
    return metric(summary, name, "count", default=0)


# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--arm", choices=["raw", "queued"])
    p.add_argument("--n", type=int, default=1000)
    p.add_argument("--stock", type=int, default=60)
    p.add_argument("--strategies", default="pessimistic,optimistic,redis")
    p.add_argument("--report", action="store_true")
    p.add_argument("--keep-raw", action="store_true",
                   help="keep k6's per-request JSON (tens of MB per run)")
    args = p.parse_args()

    if args.report:
        return report()
    if not args.arm:
        p.error("--arm is required unless --report")

    use_queue = args.arm == "queued"
    available = sales()
    if len(available) < 3:
        print("need at least 3 active sales; run: python -m scripts.seed_storefront")
        return 1

    users = export_users(max(args.n, 100))
    print(f"exported {len(users)} user ids")

    targets = [s.strip() for s in args.strategies.split(",") if s.strip()]
    assigned = {st: available[i % len(available)]["sale_id"]
                for i, st in enumerate(targets)}
    nonce = uuid.uuid4().hex[:12]

    print(f"\n=== k6 arm: {args.arm.upper()}  N={args.n}  stock={args.stock} ===")
    results = {"arm": args.arm, "n": args.n, "stock": args.stock,
               "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "strategies": {}}

    for strategy in targets:
        sale_id = assigned[strategy]
        reset(sale_id, args.stock)
        time.sleep(1.0)
        baseline = count_confirmed(sale_id)

        sampler = ResourceSampler()
        sampler.start()
        wall, out, summary_name, raw_name = run_k6(
            strategy, sale_id, args.n, use_queue, nonce, args.arm)
        sampler.stop()
        sampler.join(timeout=30)

        time.sleep(1.5)                       # let aborted commits land
        confirmed_db = count_confirmed(sale_id) - baseline

        spath = os.path.join(HERE, summary_name)
        summary = json.load(open(spath, encoding="utf-8")) if os.path.exists(spath) else {}
        rpath = os.path.join(HERE, raw_name)
        peak = peak_concurrency(rpath)
        if not args.keep_raw and os.path.exists(rpath):
            os.remove(rpath)

        rec = {
            "sale_id": sale_id,
            "wall_s": round(wall, 2),
            "confirmed_db": confirmed_db,
            "starting_stock": args.stock,
            "invariant_holds": confirmed_db <= args.stock,
            "peak_concurrent_checkouts": peak,
            "confirmed": counter(summary, "fs_confirmed"),
            "sold_out": counter(summary, "fs_sold_out"),
            "version_conflict": counter(summary, "fs_version_conflict"),
            "refused_403": counter(summary, "fs_refused_403"),
            "other_failure": counter(summary, "fs_other_failure"),
            "never_admitted": counter(summary, "fs_never_admitted"),
            "join_failed": counter(summary, "fs_join_failed"),
            "checkout_p50_ms": metric(summary, "fs_checkout_ms", "med"),
            "checkout_p95_ms": metric(summary, "fs_checkout_ms", "p(95)"),
            "checkout_p99_ms": metric(summary, "fs_checkout_ms", "p(99)"),
            "checkout_max_ms": metric(summary, "fs_checkout_ms", "max"),
            "queue_wait_p50_ms": metric(summary, "fs_queue_wait_ms", "med"),
            "queue_wait_p95_ms": metric(summary, "fs_queue_wait_ms", "p(95)"),
            "resources": sampler.summary(),
        }
        results["strategies"][strategy] = rec

        flag = "HOLDS" if rec["invariant_holds"] else "VIOLATED"
        print(f"[{strategy}] confirmed(DB)={confirmed_db}/{args.stock}  "
              f"peak_concurrent={peak}  wall={rec['wall_s']}s  "
              f"p50={_f(rec['checkout_p50_ms'])}ms p95={_f(rec['checkout_p95_ms'])}ms "
              f"p99={_f(rec['checkout_p99_ms'])}ms  "
              f"conflicts={rec['version_conflict']}  refused={rec['refused_403']}  "
              f"INVARIANT {flag}")
        r = rec["resources"]
        if r:
            print(f"           cpu_peak={r.get('cpu_peak_pct')}  pg_peak={r.get('pg_peak')}")

    out_path = os.path.join(ROOT, "results", f"k6_{args.arm}_n{args.n}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\nwrote {out_path}")
    return 0


def _f(v, nd=1):
    return "--" if v is None else f"{v:.{nd}f}"


def report():
    paths = {a: os.path.join(ROOT, "results", f"k6_{a}_n1000.json")
             for a in ("raw", "queued")}
    data = {}
    for arm, p in paths.items():
        if not os.path.exists(p):
            print(f"missing {p} -- run that arm first")
            return 1
        data[arm] = json.load(open(p, encoding="utf-8"))

    print(f"{'':<26}{'raw herd':>16}{'through queue':>16}")
    print("-" * 60)
    for st in STRATEGIES:
        r = data["raw"]["strategies"].get(st, {})
        q = data["queued"]["strategies"].get(st, {})
        print(f"\n  [{st}]")
        for key, label, nd in (
            ("peak_concurrent_checkouts", "peak concurrent checkouts", 0),
            ("wall_s", "wall time (s)", 1),
            ("checkout_p50_ms", "checkout p50 (ms)", 1),
            ("checkout_p95_ms", "checkout p95 (ms)", 1),
            ("checkout_p99_ms", "checkout p99 (ms)", 1),
            ("version_conflict", "OCC version conflicts", 0),
            ("refused_403", "refused at door", 0),
            ("confirmed_db", "confirmed in DB", 0),
        ):
            print(f"    {label:<28}{_f(r.get(key), nd):>14}{_f(q.get(key), nd):>16}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
