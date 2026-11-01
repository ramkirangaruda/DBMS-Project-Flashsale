"""
Benchmark for futility-aware admission: a herd of buyers who do NOT all show
up, through the real waiting room, door and checkout endpoints.

WHY A SEPARATE SCRIPT
    scripts/admission_control_benchmark.py is the harness behind
    docs/aimd_admission.md, and its numbers must stay reproducible, so it is
    left as it is. This script reuses its plumbing (database helpers, server
    latency from /metrics, percentiles) and adds what that harness assumes
    away: every admitted buyer there checks out immediately (p = 1, no delay),
    which leaves a show-rate estimator nothing to learn.

THE BUYER MODEL (seeded per run, identical across arms for the same seed)
    show          Bernoulli(p_show). A no-show is told they are admitted and
                  then never checks out; their token simply expires.
    show delay    seconds from being told to the first checkout attempt,
                  drawn from --delay (default lognormal, median 2 s, sigma 1).
                  A buyer slower than the token TTL arrives with an expired
                  token and is refused (403) like any late buyer.
    payment       with probability 1 - p_pay a confirmed buyer's payment
                  "fails" after a 0.3-1.0 s hold and the unit is released the
                  same way the Stripe webhook releases it
                  (app/payments._release_reserved_unit). Default p_pay = 1.

TWO SHORTCUTS, BOTH INHERITED FROM THE AIMD HARNESS AND STATED IN WRITE-UPS
    * The herd is pre-placed in the queue with one pipelined ZADD (same keys
      and scores POST /queue/join writes).
    * Grants are read straight from Redis every 50 ms instead of every buyer
      polling. Each admitted buyer then calls GET /queue/status ONCE over
      HTTP -- that call is the notification that starts their token clock --
      and checks out over HTTP. The sold-out notice (FA_SOLDOUT_NOTIFY) is
      likewise read from its Redis key, which is what /queue/status serves.

ARMS
    S-K:<K>        static K buyers per 500 ms (renew TTL)          baseline
    aimd           ADMISSION_CONTROL=aimd, coupled window (renew)   baseline
    cw1            claim window, p fixed at 1           + DB limiter, fixed TTL, sold-out notice
    cw-naive       claim window, naive p_hat            + same
    cw-censored    claim window, censoring-aware p_hat  + same
    cw-oracle      claim window, true effective p       + same
    cw-censored-coupled   claim window + coupled AIMD window (no DB limiter)
    cwp:<m>[:warm|:warmg][:pace]
                   posterior-weighted window (FA_WEIGHTING=posterior), censored
                   p_hat, FA_MARGIN=m. cold by default; warm seeds p_hat from
                   the previous sale, warmg also seeds G (its show delays);
                   pace adds FA_PACING. "Previous sale" = the cwp:1.0 cold run
                   of the same cell with the previous seed (seed 0 is a
                   seeding-only run).
    cwc:<m>[...]   the same with the original count window (every
                   unresolved claim counts one), for the weighting ablation

    Every arm runs with FA_TIMING_HEADERS=on so each buyer's admission-to-
    verdict time splits into show delay, gate wait and handler (DB) time.

    python -m scripts.fa_benchmark --arm cw-censored --strategy pessimistic \
        --stock 200 --ratio 10 --p-show 0.4 --seed 1
    python -m scripts.fa_benchmark --grid firstpass      # resumable; see GRIDS
"""
import argparse
import asyncio
import json
import math
import os
import random
import subprocess
import sys
import time
import uuid

import httpx
import redis.asyncio as aioredis

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.queue import GRANT_KEY, Z_KEY
from app.redisconf import make_redis_client
from scripts.admission_control_benchmark import (
    BASE,
    SALE_ID,
    pct,
    server_latency,
    sh_db,
    stop_server,
    user_ids,
)

MAX_TRIES = 30
TTL_S = int(os.getenv("QUEUE_ADMISSION_TTL", "30"))
REDIS_PORT = int(os.getenv("REDIS_PORT", "6390"))

FA_COMMON = {"DB_LIMITER": "aimd", "FA_SOLDOUT_NOTIFY": "on", "ADMISSION_CONTROL": "static"}


def arm_env(arm, oracle_p, prior=None):
    """Environment for one arm. Every FA flag not set here stays off."""
    return {"FA_TIMING_HEADERS": "on", **_arm_env(arm, oracle_p, prior)}


def parse_cwp(arm):
    """'cwp:1.25:warmg:pace' -> (1.25, 'warmg', True)."""
    parts = arm.split(":")
    margin = float(parts[1])
    start = next((x for x in parts[2:] if x in ("cold", "warm", "warmg")), "cold")
    return margin, start, "pace" in parts[2:]


def _arm_env(arm, oracle_p, prior):
    if arm.startswith(("cwp:", "cwc:")):
        margin, start, pace = parse_cwp(arm)
        env = {**FA_COMMON, "CLAIM_WINDOW": "censored", "FA_MARGIN": str(margin)}
        if arm.startswith("cwp:"):
            env["FA_WEIGHTING"] = "posterior"
        if start != "cold":
            if not prior:
                raise ValueError(f"{arm} needs a previous sale's prior")
            env["FA_PRIOR_P"] = f"{prior['p_hat']:.6f}"
            if start == "warmg":
                env["FA_PRIOR_DELAYS"] = prior["delays_path"]
        if pace:
            env["FA_PACING"] = "on"
        return env
    if arm.startswith("S-K:"):
        return {"ADMISSION_CONTROL": "static", "QUEUE_ADMIT_BATCH": arm.split(":")[1]}
    if arm == "aimd":
        return {"ADMISSION_CONTROL": "aimd", "QUEUE_ADMIT_BATCH": "10"}
    if arm == "cw1":
        return {**FA_COMMON, "CLAIM_WINDOW": "fixed:1"}
    if arm == "cw-naive":
        return {**FA_COMMON, "CLAIM_WINDOW": "naive"}
    if arm == "cw-censored":
        return {**FA_COMMON, "CLAIM_WINDOW": "censored"}
    if arm == "cw-oracle":
        return {**FA_COMMON, "CLAIM_WINDOW": "oracle", "FA_ORACLE_P": f"{oracle_p:.6f}"}
    if arm.startswith("breakwater:"):
        # Static admission at K, Breakwater-style credit gate at the door.
        return {"ADMISSION_CONTROL": "static", "QUEUE_ADMIT_BATCH": arm.split(":")[1],
                "DB_LIMITER": "breakwater"}
    if arm.startswith("protego:"):
        return {"ADMISSION_CONTROL": "aimd", "QUEUE_ADMIT_BATCH": "10",
                "PROTEGO_LOCK_TIMEOUT_MS": arm.split(":")[1]}
    if arm == "cw-censored-coupled":
        return {"CLAIM_WINDOW": "censored", "ADMISSION_CONTROL": "aimd",
                "FA_SOLDOUT_NOTIFY": "on", "QUEUE_ADMIT_BATCH": "10"}
    raise ValueError(f"unknown arm {arm}")


# ----------------------------------------------------------------- buyer model
def parse_delay(spec):
    """'lognormal:2,1' (median s, sigma) | 'uniform:0.05,0.3' | 'exp:2' (mean s)."""
    kind, _, params = spec.partition(":")
    vals = [float(x) for x in params.split(",")] if params else []
    if kind == "lognormal":
        median, sigma = vals
        return (lambda rng: rng.lognormvariate(math.log(median), sigma),
                lambda x: 0.5 * (1 + math.erf((math.log(x) - math.log(median)) / (sigma * math.sqrt(2)))))
    if kind == "uniform":
        lo, hi = vals
        return (lambda rng: rng.uniform(lo, hi), lambda x: min(1.0, max(0.0, (x - lo) / (hi - lo))))
    if kind == "exp":
        (mean,) = vals
        return (lambda rng: rng.expovariate(1 / mean), lambda x: 1 - math.exp(-x / mean))
    raise ValueError(spec)


def effective_p(p_show, delay_spec, ttl=TTL_S):
    """The show rate a perfect estimator would converge to: show at all, and
    before the token expires."""
    _draw, cdf = parse_delay(delay_spec)
    return p_show * cdf(ttl)


class Buyer:
    __slots__ = (
        "delay",
        "drops",
        "gate_ms",
        "handler_ms",
        "i",
        "lats",
        "outcome",
        "pays",
        "qt",
        "retries",
        "show",
        "t_done",
        "t_grant",
        "t_notified",
        "t_show",
        "token",
        "user",
    )

    def __init__(self, i, user, show, delay, pays):
        self.i, self.user, self.show, self.delay, self.pays = i, user, show, delay, pays
        self.qt = self.token = None
        self.t_grant = self.t_notified = self.t_show = self.t_done = None
        self.outcome = "queued"
        self.lats = []
        self.retries = 0
        self.drops = 0
        self.gate_ms = 0.0
        self.handler_ms = 0.0


def make_buyers(n, users, p_show, delay_spec, p_pay, seed):
    rng = random.Random(seed)
    draw, _cdf = parse_delay(delay_spec)
    out = []
    for i in range(n):
        show = rng.random() < p_show
        delay = draw(rng)
        pays = rng.random() < p_pay
        out.append(Buyer(i, users[i % len(users)], show, delay, pays))
    return out


def release_unit(strategy):
    """A failed payment: release the unit exactly as the Stripe webhook does."""
    from app.database import SessionLocal
    from app.payments import _release_reserved_unit
    db = SessionLocal()
    try:
        _release_reserved_unit(db, SALE_ID, strategy)
        db.commit()
    finally:
        db.close()


# ----------------------------------------------------------------- one run
async def run_herd(arm, strategy, buyers, stock, deadline_s, notify_soldout, ttl=TTL_S):
    ar = aioredis.Redis(host="localhost", port=REDIS_PORT, decode_responses=True)
    limits = httpx.Limits(max_connections=4000, max_keepalive_connections=4000)
    conc = {"now": 0, "peak": 0}
    traj = []
    releases = []
    async with httpx.AsyncClient(base_url=BASE, timeout=90, limits=limits) as c:
        pipe = ar.pipeline()
        base = time.time()
        for b in buyers:
            b.qt = uuid.uuid4().hex
            pipe.zadd(Z_KEY.format(sale_id=SALE_ID), {b.qt: base + b.i * 1e-6})
        await pipe.execute()
        t0 = time.perf_counter()

        async def sampler():
            while True:
                entry = {"t": round(time.perf_counter() - t0, 1), "conc": conc["now"]}
                try:
                    ctl = (await c.get("/queue/control")).json()
                    cw = (ctl.get("claim_window") or {}).get(SALE_ID) or {}
                    st = cw.get("state") or {}
                    entry.update({k: st.get(k) for k in ("p_hat", "free", "window", "unresolved")})
                    dl = ctl.get("db_limiter") or {}
                    if dl:
                        entry["db_limit"] = dl.get("limit")
                    if ctl.get("state"):
                        entry["cwnd"] = round(ctl["state"].get("cwnd", 0), 1)
                except Exception:                              # noqa: BLE001, S110
                    pass
                traj.append(entry)
                await asyncio.sleep(1.0)

        async def checkout(b):
            for _ in range(MAX_TRIES):
                conc["now"] += 1
                conc["peak"] = max(conc["peak"], conc["now"])
                t = time.perf_counter()
                try:
                    r = await c.post(f"/checkout/{strategy}",
                                     params={"sale_id": SALE_ID, "user_id": b.user},
                                     headers={"X-Admission-Token": b.token})
                except httpx.TimeoutException:
                    b.outcome, b.t_done = "timeout", time.perf_counter() - t0
                    return
                except Exception:                              # noqa: BLE001
                    b.outcome, b.t_done = "client_error", time.perf_counter() - t0
                    return
                finally:
                    conc["now"] -= 1
                b.lats.append((time.perf_counter() - t) * 1000)
                try:
                    b.gate_ms += float(r.headers.get("X-FA-Gate-Ms") or 0.0)
                    b.handler_ms += float(r.headers.get("X-FA-Handler-Ms") or 0.0)
                except ValueError:
                    pass
                try:
                    j = r.json()
                except Exception:                              # noqa: BLE001
                    j = {}
                st = j.get("status")
                if st == "confirmed":
                    b.outcome = "confirmed"
                elif st == "sold_out":
                    b.outcome = "sold_out"
                elif st == "dropped":
                    b.drops += 1                               # Protego-style drop: retry
                    await asyncio.sleep(random.uniform(0, 0.02))
                    continue
                elif r.status_code == 403:
                    b.outcome = "expired"
                elif r.status_code == 503:
                    b.outcome = "gate_timeout"
                elif r.status_code >= 500:
                    b.outcome = "error_5xx"
                elif st == "failed" and r.status_code == 409:
                    b.retries += 1                             # OCC conflict: try again
                    await asyncio.sleep(random.uniform(0, 0.02))
                    continue
                else:
                    b.outcome = "other"
                b.t_done = time.perf_counter() - t0
                return
            b.outcome, b.t_done = "gave_up", time.perf_counter() - t0

        async def flow(b):
            # The notification: one status call, as a client that has just
            # seen "admitted" would make. It starts the token clock.
            try:
                await c.get(f"/queue/status/{SALE_ID}", params={"token": b.qt})
            except Exception:                                  # noqa: BLE001, S110
                pass
            b.t_notified = time.perf_counter() - t0
            if not b.show:
                b.outcome = "noshow"
                return
            # A buyer slower than the TTL can only meet an expired token; cap
            # the wait just past expiry so one long-tail buyer does not hold
            # the run open until the deadline. Same outcome (403), less idle.
            await asyncio.sleep(min(b.delay, ttl + 1.0))
            b.t_show = time.perf_counter() - t0
            await checkout(b)
            if b.outcome == "confirmed" and not b.pays:
                await asyncio.sleep(random.uniform(0.3, 1.0))
                await asyncio.to_thread(release_unit, strategy)
                releases.append(time.perf_counter() - t0)
                b.outcome = "confirmed_payment_failed"

        samp = asyncio.create_task(sampler())
        queued = {b.qt: b for b in buyers}
        tasks = []
        soldout_key = f"fa:soldout:{SALE_ID}"
        while time.perf_counter() - t0 < deadline_s:
            if queued:
                keys = list(queued)
                grants = await ar.mget([GRANT_KEY.format(sale_id=SALE_ID, queue_token=k) for k in keys])
                now = time.perf_counter() - t0
                for k, g in zip(keys, grants, strict=True):
                    if g:
                        b = queued.pop(k)
                        b.token, b.t_grant, b.outcome = g, now, "admitted"
                        tasks.append(asyncio.create_task(flow(b)))
                if notify_soldout and queued and await ar.get(soldout_key):
                    for b in queued.values():
                        b.outcome, b.t_done = "told_sold_out", now
                    queued.clear()
            if not queued and all(t.done() for t in tasks):
                break
            await asyncio.sleep(0.05)
        t_stop = time.perf_counter() - t0
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        samp.cancel()
        try:
            final_ctl = (await c.get("/queue/control")).json()
        except Exception:                                      # noqa: BLE001
            final_ctl = {}
    await ar.aclose()
    return summarise_run(buyers, stock, t_stop, deadline_s, traj, conc, final_ctl, releases)


def summarise_run(buyers, stock, t_stop, deadline_s, traj, conc, final_ctl, releases):
    outcomes = {}
    for b in buyers:
        outcomes[b.outcome] = outcomes.get(b.outcome, 0) + 1
    kept = [b for b in buyers if b.outcome == "confirmed"]
    admitted = [b for b in buyers if b.t_grant is not None]
    shown = [b for b in admitted if b.t_show is not None]
    lats = [x for b in buyers for x in b.lats]
    done_conf = sorted(b.t_done for b in kept)
    sold = len(kept)
    losers = [b for b in buyers if b.outcome in ("sold_out", "told_sold_out")]
    verdict = [b.t_done for b in losers]
    cw = (final_ctl.get("claim_window") or {}).get(SALE_ID) or {}
    dl = final_ctl.get("db_limiter") or {}
    wait = dl.get("wait") or {}
    # Admitted buyers who showed and could not get a unit, however they lost:
    # sold out at checkout, token expired while waiting, or turned away at the gate.
    futile = [b for b in shown if b.outcome in ("sold_out", "expired", "gate_timeout")]
    return {
        "buyers": len(buyers), "stock": stock,
        "sold": sold, "sold_pct": round(100 * sold / stock, 1),
        "outcomes": outcomes,
        "t_sellout_s": round(done_conf[-1], 2) if sold >= stock and done_conf else None,
        "t_last_confirm_s": round(done_conf[-1], 2) if done_conf else None,
        "elapsed_s": round(t_stop, 2), "hit_deadline": t_stop >= deadline_s - 0.1,
        "admitted": len(admitted), "shown": len(shown),
        "noshow": outcomes.get("noshow", 0),
        # Futility: buyers who were admitted, showed up, and could only lose.
        "admitted_lost": outcomes.get("sold_out", 0),
        "futile_pct_of_admitted": round(100 * outcomes.get("sold_out", 0) / max(1, len(admitted)), 1),
        "told_sold_out": outcomes.get("told_sold_out", 0),
        "expired_403": outcomes.get("expired", 0),
        "still_queued_at_stop": outcomes.get("queued", 0),
        "verdict_s": {"p50": round(pct(verdict, 50), 2), "p95": round(pct(verdict, 95), 2)} if verdict else None,
        "occ_retries": sum(b.retries for b in buyers),
        "protego_drops": sum(b.drops for b in buyers),
        "checkout_attempts": len(lats),
        "client_latency_ms": {"p50": round(pct(lats, 50), 1), "p95": round(pct(lats, 95), 1),
                              "p99": round(pct(lats, 99), 1)} if lats else None,
        "peak_concurrent_client": conc["peak"],
        "gate_wait": {"n": int(wait.get("n", 0)), "mean_ms": round(float(wait.get("sum_ms", 0)) / max(1, int(wait.get("n", 0))), 1),
                      "timeouts": int(wait.get("timeouts", 0))} if wait else None,
        "cw_final": cw, "db_limiter_final_limit": dl.get("limit"),
        "payment_releases": len(releases),
        "futile_admitted": len(futile),
        "a2v": {"winners": a2v_summary(kept), "losers": a2v_summary(futile)},
        "trajectory": traj,
    }


def a2v_summary(bs):
    """Admission-to-verdict for these buyers (grant to final answer), and its
    parts: notification (grant to status call), show delay (the buyer's own
    think time), gate wait and handler time summed over attempts, and the
    rest (network, client, retry back-off)."""
    bs = [b for b in bs if b.t_grant is not None and b.t_done is not None and b.t_show is not None]
    if not bs:
        return None
    tot = [b.t_done - b.t_grant for b in bs]
    parts = {
        "notify_s": [b.t_notified - b.t_grant for b in bs],
        "show_delay_s": [b.t_show - b.t_notified for b in bs],
        "gate_s": [b.gate_ms / 1000.0 for b in bs],
        "handler_s": [b.handler_ms / 1000.0 for b in bs],
    }
    rest = [t - sum(v[i] for v in parts.values()) for i, t in enumerate(tot)]
    out = {"n": len(bs), "total_p50": round(pct(tot, 50), 3), "total_p95": round(pct(tot, 95), 3),
           "total_mean": round(sum(tot) / len(tot), 3)}
    for k, v in {**parts, "rest_s": rest}.items():
        out[k + "_mean"] = round(sum(v) / len(v), 3)
        out[k + "_p50"] = round(pct(v, 50), 3)
    out["system_s_mean"] = round(out["total_mean"] - out["show_delay_s_mean"], 3)
    out["system_s_p50"] = round(pct([t - d for t, d in zip(tot, parts["show_delay_s"], strict=True)], 50), 3)
    return out


# ----------------------------------------------------------------- server
def clean_redis(r):
    for pat in ("queue:*", "admission:*", "admctl:*", "stock:*", "idempotency:*",
                "fa:*", "dblim:*"):
        for k in r.scan_iter(match=pat, count=1000):
            r.delete(k)


def start_server(env_extra, log_path, ttl=TTL_S):
    env = dict(os.environ, OTEL_SDK_DISABLED="true", ADMISSION_ENFORCED="true",
               QUEUE_ADMIT_INTERVAL="0.5", QUEUE_ADMISSION_TTL=str(ttl), PYTHONPATH=ROOT)
    for k in ("CLAIM_WINDOW", "DB_LIMITER", "FA_TTL_MODE", "FA_SOLDOUT_NOTIFY", "FA_ORACLE_P",
              "ADMISSION_CONTROL", "PROTEGO_LOCK_TIMEOUT_MS", "FA_WEIGHTING", "FA_MARGIN",
              "FA_PRIOR_P", "FA_PRIOR_DELAYS", "FA_PACING", "FA_TIMING_HEADERS"):
        env.pop(k, None)
    env.update(env_extra)
    port = int(BASE.rsplit(":", 1)[1])
    code = ("import uvicorn; uvicorn.run('app.main:app', host='127.0.0.1', "
            f"port={port}, log_level='warning')")
    log = open(log_path, "w")                 # noqa: SIM115 -- the server owns it for its lifetime
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            if httpx.get(f"{BASE}/api", timeout=1).status_code == 200:
                return proc
        except Exception:                                      # noqa: BLE001
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError(f"server did not start; see {log_path}")


def sale_prior(r, final_ctl_state):
    """What a later sale may learn from this one: its final p_hat and every
    show delay the server observed (show time minus clock start)."""
    t0 = r.hgetall(f"fa:t0:{SALE_ID}")
    shows = r.hgetall(f"fa:show:{SALE_ID}")
    delays = sorted(round(float(shows[k]) - float(t0[k]), 3) for k in shows if k in t0)
    return {"p_hat": (final_ctl_state or {}).get("p_hat"), "delays": delays}


def run_one(cell, users, r, prior=None):
    """cell: dict(arm, strategy, stock, ratio, p_show, delay, p_pay, seed, deadline_s)."""
    ttl = cell.get("ttl", TTL_S)
    oracle = effective_p(cell["p_show"], cell["delay"], ttl)
    env = arm_env(cell["arm"], oracle, prior)
    os.makedirs(os.path.join(ROOT, "results", "fa", "logs"), exist_ok=True)
    log_path = os.path.join(ROOT, "results", "fa", "logs", f"server_{cell['arm'].replace(':', '_')}.log")
    clean_redis(r)
    sh_db("UPDATE flash_sale_events SET start_time = now() - interval '1 hour', "
          "end_time = now() + interval '3 hours' WHERE id = :s", s=SALE_ID)
    proc = start_server(env, log_path, ttl=ttl)
    try:
        httpx.post(f"{BASE}/admin/reset-sale/{SALE_ID}", params={"stock": cell["stock"]}, timeout=30)
        httpx.post(f"{BASE}/queue/reset/{SALE_ID}", timeout=30)
        httpx.post(f"{BASE}/queue/control/reset", timeout=30)
        time.sleep(0.6)                      # let the stock view publish the new stock
        before = sh_db("SELECT count(*) FROM orders WHERE sale_id = :s", s=SALE_ID)[0][0]
        n = round(cell["stock"] * cell["ratio"])
        buyers = make_buyers(n, users, cell["p_show"], cell["delay"], cell["p_pay"], cell["seed"])
        res = asyncio.run(run_herd(cell["arm"], cell["strategy"], buyers, cell["stock"],
                                   cell["deadline_s"], notify_soldout=env.get("FA_SOLDOUT_NOTIFY") == "on",
                                   ttl=ttl))
        after = sh_db("SELECT count(*) FROM orders WHERE sale_id = :s", s=SALE_ID)[0][0]
        inv = sh_db("SELECT reserved_stock, total_stock FROM inventory WHERE sale_id = :s", s=SALE_ID)[0]
        res["server_latency_ms"] = server_latency(cell["strategy"])
        res["db_orders_created"] = after - before
        res["db_reserved_stock"], res["db_total_stock"] = inv[0], inv[1]
        # Never oversell: the database must not hold more reservations than stock.
        res["invariant_ok"] = inv[0] <= inv[1] and res["sold"] <= cell["stock"]
        res["oracle_p"] = round(oracle, 4)
        if env.get("CLAIM_WINDOW"):
            res["prior_out"] = sale_prior(r, (res.get("cw_final") or {}).get("state"))
        if prior:
            res["prior_in"] = {"p_hat": prior["p_hat"], "n_delays": len(prior["delays"]),
                               "from": prior["from"]}
        res["cell"] = cell
        res["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return res
    finally:
        stop_server(proc)


# ----------------------------------------------------------------- grids
GRIDS = {
    # Ramkiran's first pass (plan section 10). S-K uses the best static K per
    # strategy from the fresh P0 baselines (research/DECISIONS.md).
    "firstpass": {
        "arms_by_strategy": {
            "pessimistic": ["S-K:40", "aimd", "cw1", "cw-naive", "cw-censored", "cw-oracle"],
            "optimistic": ["S-K:40", "aimd", "cw1", "cw-naive", "cw-censored", "cw-oracle"],
        },
        "p_show": [0.4, 0.7, 1.0], "ratio": [3, 10], "seeds": [1, 2, 3, 4, 5],
        "stock": 200, "delay": "lognormal:2,1", "p_pay": 1.0, "deadline_s": 90.0},
    # P6 generic baselines, at one first-pass operating point so they can be
    # set against the first-pass arms in the same cells.
    "p6": {
        "arms_by_strategy": {
            "pessimistic": ["protego:50", "protego:100", "protego:250", "breakwater:40"],
            "optimistic": ["protego:50", "protego:100", "protego:250", "breakwater:40"],
        },
        "p_show": [0.7], "ratio": [10], "seeds": [1, 2, 3, 4, 5],
        "stock": 200, "delay": "lognormal:2,1", "p_pay": 1.0, "deadline_s": 90.0},
}

# The frontier (Ramkiran's decision 1): sweep the posterior window's margin
# and set every other arm, re-run here with timing headers, as points on the
# same plot. Seed 0 is a seeding-only sale for the warm arms of seed 1.
MARGINS = [0.9, 1.0, 1.1, 1.25, 1.5, 2.0, 3.0]
FRONTIER_ARMS = ([f"cwp:{m}:warmg:pace" for m in MARGINS]                 # the mechanism
                 + [f"cwp:{m}" for m in MARGINS]                           # cold, unpaced
                 + [f"cwc:{m}:warmg:pace" for m in (1.0, 1.25, 1.5, 2.0)]  # ablation: count weighting
                 + [f"cwp:{m}:pace" for m in (1.0, 1.25, 1.5)]             # ablation: cold start
                 + [f"cwp:{m}:warm:pace" for m in (1.0, 1.25, 1.5)]        # ablation: warm p_hat only
                 + [f"cwp:{m}:warmg" for m in (1.0, 1.5)]                  # ablation: no pacing
                 + ["cw-oracle", "S-K:40", "aimd", "protego:100", "breakwater:40"])
for _name, _p in (("frontier07", 0.7), ("frontier04", 0.4), ("frontier10", 1.0)):
    GRIDS[_name] = {
        "arms_by_strategy": {"pessimistic": FRONTIER_ARMS},
        "seed0_arms": ["cwp:1.0"],
        "p_show": [_p], "ratio": [10], "seeds": [0, 1, 2, 3, 4, 5],
        "stock": 200, "delay": "lognormal:2,1", "p_pay": 1.0, "deadline_s": 90.0}

# Round 3 (research/DECISIONS.md D27): isolate the weighting (count window
# with the same warm-p start as the posterior arm) and test a mis-specified
# prior (a warm arm seeded from a sale with the other p_show). No seeding
# runs: priors come from the cold cwp:1.0 runs already in the frontier grids,
# so every warm arm here gets exactly the prior its frontier counterpart got.
ISOLATE_ARMS = ([f"cwc:{m}:warm:pace" for m in (1.0, 1.25, 1.5, 2.0)]
                + ["cwp:2.0:warm:pace"])
for _name, _p, _other in (("round3_07", 0.7, "04"), ("round3_04", 0.4, "07")):
    GRIDS[_name] = {
        "arms_by_strategy": {"pessimistic": ISOLATE_ARMS + [f"cwp:{m}:{s}:pace:from{_other}"
                                                            for s in ("warm", "warmg") for m in (1.25, 2.0)]},
        "prior_file": f"results/fa/grid/frontier{_name[-2:]}.jsonl",
        "p_show": [_p], "ratio": [10], "seeds": [1, 2, 3, 4, 5],
        "stock": 200, "delay": "lognormal:2,1", "p_pay": 1.0, "deadline_s": 90.0}

# Round 3b (D28): warm p had no margin between 1.0 and 1.25, where warm-G
# has its low-futile points; fill that gap before comparing the two.
for _name, _p in (("round3b_07", 0.7), ("round3b_04", 0.4)):
    GRIDS[_name] = {**GRIDS["round3_" + _name[-2:]],
                    "arms_by_strategy": {"pessimistic": [f"cwp:{m}:warm:pace" for m in (1.05, 1.1, 1.15)]}}

# Round 4 (D29): the main figure's two warm starts and the baselines in two
# more cells, p_show 1.0 (pessimistic) and optimistic at p_show 0.7. Reduced
# arm set: both warm starts paced across the useful margins, plus baselines.
MAIN_ARMS = (["cwp:1.0"]                                # cold; also the next seed's prior
             + [f"cwp:{m}:warm:pace" for m in (1.0, 1.1, 1.25, 1.5, 2.0)]
             + [f"cwp:{m}:warmg:pace" for m in (1.0, 1.25, 1.5, 2.0, 3.0)]
             + ["cw-oracle", "S-K:40", "aimd", "protego:100", "breakwater:40"])
for _name, _strategy, _p in (("main10", "pessimistic", 1.0), ("main07opt", "optimistic", 0.7)):
    GRIDS[_name] = {
        "arms_by_strategy": {_strategy: MAIN_ARMS},
        "seed0_arms": ["cwp:1.0"],
        "p_show": [_p], "ratio": [10], "seeds": [0, 1, 2, 3, 4, 5],
        "stock": 200, "delay": "lognormal:2,1", "p_pay": 1.0, "deadline_s": 90.0}

PRIOR_SOURCE_ARM = "cwp:1.0"


def prior_source(name, out_path, cell):
    """(results file, cell) to take a warm arm's prior from. Frontier grids:
    this grid's own file. Round 3: the frontier grid of the same p_show, or,
    for an arm tagged ':fromNN', the frontier grid with p_show 0.NN (the
    mis-specified prior)."""
    tag = next((x for x in cell["arm"].split(":") if x.startswith("from")), None)
    if tag:
        return os.path.join(ROOT, "results", "fa", "grid", f"frontier{tag[4:]}.jsonl"), \
            {**cell, "p_show": int(tag[4:]) / 10}
    if "prior_file" in GRIDS[name]:
        return os.path.join(ROOT, GRIDS[name]["prior_file"]), cell
    return out_path, cell


def cell_key(c):
    return f"{c['arm']}|{c['strategy']}|{c['p_show']}|{c['ratio']}|{c['seed']}"


def grid_cells(name):
    g = GRIDS[name]
    cells = []
    for seed in g["seeds"]:                       # seed-major: an interrupted grid still has whole seeds
        for p in g["p_show"]:
            for ratio in g["ratio"]:
                for strategy, arms in g["arms_by_strategy"].items():
                    if seed == 0 and "seed0_arms" in g:
                        arms = g["seed0_arms"]
                    for arm in arms:
                        cells.append({"arm": arm, "strategy": strategy, "stock": g["stock"], "ratio": ratio,
                                          "p_show": p, "delay": g["delay"], "p_pay": g["p_pay"], "seed": seed,
                                          "deadline_s": g["deadline_s"]})
    return cells


def find_prior(out_path, cell):
    """The previous sale for a warm arm: the PRIOR_SOURCE_ARM run of the same
    cell with the previous seed, as recorded in this grid's results file."""
    want = cell_key({**cell, "arm": PRIOR_SOURCE_ARM, "seed": cell["seed"] - 1})
    with open(out_path) as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            if cell_key(row["cell"]) == want and row.get("prior_out"):
                pr = row["prior_out"]
                d = os.path.join(ROOT, "results", "fa", "priors")
                os.makedirs(d, exist_ok=True)
                path = os.path.join(d, want.replace("|", "_").replace(":", "_") + ".json")
                if not os.path.exists(path):          # same content when it exists; never rewrite
                    with open(path, "w") as g:
                        json.dump(pr["delays"], g)
                return {"p_hat": pr["p_hat"], "delays": pr["delays"], "delays_path": path, "from": want}
    raise RuntimeError(f"no prior run {want} in {out_path}")


def needs_prior(arm):
    return arm.startswith(("cwp:", "cwc:")) and parse_cwp(arm)[1] != "cold"


def run_grid(name, out_path, users, r, limit=None):
    done = set()
    if os.path.exists(out_path):
        with open(out_path) as f:
            for line in f:
                if line.strip():
                    done.add(cell_key(json.loads(line)["cell"]))
    cells = [c for c in grid_cells(name) if cell_key(c) not in done]
    print(f"grid {name}: {len(done)} done, {len(cells)} to go", flush=True)
    for i, cell in enumerate(cells[:limit] if limit else cells):
        t = time.time()
        prior = find_prior(*prior_source(name, out_path, cell)) if needs_prior(cell["arm"]) else None
        res = run_one(cell, users, r, prior)
        with open(out_path, "a") as f:
            f.write(json.dumps(res) + "\n")
        print(f"[{i + 1}/{len(cells)}] {cell_key(cell)}  sold {res['sold']}/{cell['stock']}  "
              f"lost {res['admitted_lost']}  futile {res['futile_admitted']}  told {res['told_sold_out']}  "
              f"t {res['t_sellout_s']}  "
              f"ok {res['invariant_ok']}  ({time.time() - t:.0f}s)", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--grid", default=None, choices=sorted(GRIDS))
    p.add_argument("--out", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--arm", default="cw-censored")
    p.add_argument("--strategy", default="pessimistic", choices=["pessimistic", "optimistic", "redis"])
    p.add_argument("--stock", type=int, default=200)
    p.add_argument("--ratio", type=float, default=10)
    p.add_argument("--p-show", type=float, default=0.7)
    p.add_argument("--delay", default="lognormal:2,1")
    p.add_argument("--p-pay", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--deadline", type=float, default=90.0)
    p.add_argument("--ttl", type=int, default=TTL_S)
    args = p.parse_args()

    r = make_redis_client()
    users = user_ids()
    if not users:
        sys.exit("no users; run python -m scripts.seed first")
    try:
        if args.grid:
            out = args.out or os.path.join("results", "fa", "grid", f"{args.grid}.jsonl")
            os.makedirs(os.path.dirname(os.path.join(ROOT, out)), exist_ok=True)
            run_grid(args.grid, os.path.join(ROOT, out), users, r, args.limit)
        else:
            cell = {"arm": args.arm, "strategy": args.strategy, "stock": args.stock, "ratio": args.ratio,
                        "p_show": args.p_show, "delay": args.delay, "p_pay": args.p_pay, "seed": args.seed,
                        "deadline_s": args.deadline, "ttl": args.ttl}
            res = run_one(cell, users, r)
            print(json.dumps({k: v for k, v in res.items() if k != "trajectory"}, indent=1))
            if args.out:
                with open(os.path.join(ROOT, args.out), "a") as f:
                    f.write(json.dumps(res) + "\n")
    finally:
        clean_redis(r)


if __name__ == "__main__":
    main()
