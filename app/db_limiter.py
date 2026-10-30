"""
A database concurrency limit that counts only checkouts actually running.

WHY A SECOND LIMIT
    app/admission_control.py's window bounds `granted + inflight`: admitted
    buyers who have not shown up yet hold a slot in the same window as
    checkouts running in the database. With no-shows, idle grants fill that
    window and starve real checkouts until their tokens expire (the pilot's
    aimd arm crawled at low show rates; research/pilot/). Once the claim
    window (app/claim_window.py) decides WHO may still win, the database only
    needs protecting from too many checkouts at once -- so this limit is
    enforced at the door, around the checkout handler, and idle grants never
    count against it.

THE CONTROL LAW IS NOT NEW
    It is admission_control.step(), called unchanged: slow start with the
    full-pipe test, AIMD (+max(1, 5%) / x0.5), a Vegas-style baseline and the
    latency / loss / stall signals. Only what the window gates differs, so
    coupled vs decoupled comparisons change one variable.

HOW IT IS ENFORCED
    The door (app/admission.py) calls acquire() before the handler. A Lua
    script admits the request if `dblim:inflight < dblim:limit`. Otherwise
    the request waits on the event loop -- a suspended coroutine, not one of
    the threads the sync handlers need -- polling every few milliseconds, up
    to DBLIM_MAX_WAIT_S, then gets 503. The leader recomputes the limit every
    step from latencies the door records (handler time only, gate wait
    excluded, so queueing at the gate does not look like a slow database).

    DB_LIMITER=aimd turns it on. Default off.

BREAKWATER-STYLE VARIANT (evaluation baseline only)
    DB_LIMITER=breakwater keeps the same gate but replaces the control law
    with Breakwater's (OSDI '20) credit update: below the delay target, add
    `BW_ADDITIVE` credits per step; above it, cut multiplicatively in
    proportion to the overshoot, credits *= max(1 - beta * (d - target) / d,
    0.5). The delay signal is the p90 of gate wait + handler time (queueing
    delay as the server sees it); the target is clamp(BW_TARGET_X x the best
    p90 seen, BW_TARGET_MIN_MS, ...). It is "Breakwater-style": Breakwater's
    RPC-layer credits and demand speculation are not reproduced.
"""
import asyncio
import json
import os
import time
from dataclasses import asdict, replace

from app import admission_control as ac

MODE = os.getenv("DB_LIMITER", "off").strip().lower()
ENABLED = MODE in ("aimd", "breakwater")

BW_ADDITIVE = float(os.getenv("BW_ADDITIVE", "1"))
BW_BETA = float(os.getenv("BW_BETA", "0.4"))
BW_TARGET_X = float(os.getenv("BW_TARGET_X", "2.0"))
BW_TARGET_MIN_MS = float(os.getenv("BW_TARGET_MIN_MS", "20"))

STATE_KEY = "dblim:state"
LIMIT_KEY = "dblim:limit"
INFLIGHT_KEY = "dblim:inflight"
OBS_KEY = "dblim:obs:{sec}"
BW_OBS_KEY = "dblim:bwobs:{sec}"         # gate wait + handler time (Breakwater-style signal)
WAITING_KEY = "dblim:waiting"          # requests waiting at the gate right now
WAIT_KEY = "dblim:wait"                # HASH n, sum_ms, max_ms, timeouts, b<k> histogram

MAX_WAIT_S = float(os.getenv("DBLIM_MAX_WAIT_S", "20"))
POLL_S = float(os.getenv("DBLIM_POLL_S", "0.005"))


def _env(name, default):
    return type(default)(os.getenv(name, default))


# Same defaults as the admission controller, except the cap: there is no
# point letting more checkouts in than the checkout pool has connections
# (app/database.py: pool_size 10 + max_overflow 20).
CFG = replace(ac.CFG,
              max_cwnd=_env("DBLIM_MAX_CWND", 30.0),
              init_cwnd=_env("DBLIM_INIT_CWND", ac.CFG.init_cwnd),
              min_cwnd=_env("DBLIM_MIN_CWND", ac.CFG.min_cwnd))

_ACQUIRE_LUA = """
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
local lim = tonumber(redis.call('GET', KEYS[2]) or ARGV[1])
if cur < lim then
    redis.call('INCR', KEYS[1])
    redis.call('EXPIRE', KEYS[1], 300)
    return 1
end
return 0
"""


# ---------------------------------------------------------------- door side (async)
def _bucket(ms):
    return ac.bucket_of(ms)


_gate = {"loop": None, "lock": None}


def _local_lock():
    """One FIFO lock per event loop. Only the request holding it polls Redis;
    everyone else waits on the lock. The first version had every waiter poll
    Redis every 5 ms, and with hundreds of admitted buyers at the gate that
    polling saturated the event loop and Redis, inflated every checkout's
    latency, and drove this very controller's window down to its floor."""
    loop = asyncio.get_running_loop()
    if _gate["loop"] is not loop:
        _gate["loop"], _gate["lock"] = loop, asyncio.Lock()
    return _gate["lock"]


async def _try(ar, init):
    try:
        return await ar.eval(_ACQUIRE_LUA, 2, INFLIGHT_KEY, LIMIT_KEY, init)
    except Exception:                                          # noqa: BLE001
        return 1                                               # Redis trouble: fail open, as the door does


async def acquire(ar):
    """Wait for a slot. Returns (acquired, waited_ms)."""
    started = time.perf_counter()
    deadline = started + MAX_WAIT_S
    init = int(CFG.init_cwnd)
    lock = _local_lock()

    async def done(ok):
        waited = (time.perf_counter() - started) * 1000.0
        await _record_wait(ar, waited, timed_out=not ok)
        return bool(ok), waited

    if not lock.locked() and await _try(ar, init):
        return await done(True)
    # Demand the controller must see: admission_control.step() treats
    # "nothing measured and nobody waiting" as the end of a drop and resets
    # the window. For a door-level gate, "waiting" means requests queued
    # HERE -- the waiting room can be empty while hundreds of admitted
    # buyers queue at the gate.
    await _bump_waiting(ar, 1)
    try:
        try:
            await asyncio.wait_for(lock.acquire(), timeout=max(0.0, deadline - time.perf_counter()))
        except TimeoutError:
            return await done(False)
        try:
            while True:
                if await _try(ar, init):
                    return await done(True)
                if time.perf_counter() >= deadline:
                    return await done(False)
                await asyncio.sleep(POLL_S)
        finally:
            lock.release()
    finally:
        await _bump_waiting(ar, -1)


async def _bump_waiting(ar, delta):
    try:
        v = await ar.incrby(WAITING_KEY, delta)
        await ar.expire(WAITING_KEY, 300)
        if v is not None and int(v) < 0:
            await ar.set(WAITING_KEY, 0, ex=300)
    except Exception:                                          # noqa: BLE001, S110
        pass


async def _record_wait(ar, waited_ms, timed_out):
    try:
        pipe = ar.pipeline()
        pipe.hincrby(WAIT_KEY, "n", 1)
        pipe.hincrbyfloat(WAIT_KEY, "sum_ms", waited_ms)
        pipe.hincrby(WAIT_KEY, f"b{_bucket(waited_ms)}", 1)
        if timed_out:
            pipe.hincrby(WAIT_KEY, "timeouts", 1)
        pipe.expire(WAIT_KEY, 3600)
        await pipe.execute()
    except Exception:                                          # noqa: BLE001, S110
        pass


async def release(ar):
    try:
        v = await ar.decr(INFLIGHT_KEY)
        if v is not None and int(v) < 0:
            await ar.set(INFLIGHT_KEY, 0, ex=300)
    except Exception:                                          # noqa: BLE001, S110
        pass


async def _record(ar, latency_ms, outcome, now, key_fmt=OBS_KEY):
    key = key_fmt.format(sec=int(now))
    pipe = ar.pipeline()
    pipe.hincrby(key, "n", 1)
    if outcome != "ok":
        pipe.hincrby(key, "loss", 1)
    pipe.hincrby(key, f"b{ac.bucket_of(latency_ms)}", 1)
    pipe.expire(key, 60)
    await pipe.execute()


_pending = set()


def record(ar, latency_ms, outcome, now=None, waited_ms=0.0):
    async def run():
        try:
            await _record(ar, latency_ms, outcome, now or time.time())
            if MODE == "breakwater":
                await _record(ar, latency_ms + waited_ms, outcome, now or time.time(), BW_OBS_KEY)
        except Exception:                                      # noqa: BLE001, S110
            pass
    t = asyncio.ensure_future(run())
    _pending.add(t)
    t.add_done_callback(_pending.discard)


# ---------------------------------------------------------------- Breakwater-style law (pure)
def breakwater_step(st, stats, cfg, now, additive=None, beta=None, target_x=None, target_min_ms=None):
    """One Breakwater-style credit update. `stats` is a read_window() result
    over gate wait + handler time. Mutates and returns `st` (an
    admission_control.State, reused for its fields)."""
    additive = BW_ADDITIVE if additive is None else additive
    beta = BW_BETA if beta is None else beta
    target_x = BW_TARGET_X if target_x is None else target_x
    target_min_ms = BW_TARGET_MIN_MS if target_min_ms is None else target_min_ms
    st.last_step_ts = now
    st.steps += 1
    n, d = stats["n"], stats["q_ms"]
    st.last_n, st.last_q_ms = n, d
    if n < cfg.min_samples or d is None:
        st.last_signal = "no_data"
        return st
    if st.baseline_ms is None or d < st.baseline_ms:
        st.baseline_ms = d
    target = max(target_min_ms, target_x * st.baseline_ms)
    if d <= target:
        st.cwnd = min(cfg.max_cwnd, st.cwnd + additive)
        st.last_signal = "increase"
    else:
        st.cwnd = max(cfg.min_cwnd, st.cwnd * max(1 - beta * (d - target) / d, 0.5))
        st.decreases += 1
        st.last_decrease_ts = now
        st.last_signal = "delay"
    return st


# ---------------------------------------------------------------- leader side (sync)
def read_window(r, now, window_s, since=0.0, q=None, key=None):
    """admission_control.read_window, over this limiter's own observations."""
    q = CFG.quantile if q is None else q
    first = max(int(now) - window_s + 1, int(since) + 1)
    secs = list(range(first, int(now) + 1))
    if not secs:
        return {"n": 0, "q_ms": None, "loss": 0.0, "secs": 0}
    pipe = r.pipeline()
    for s in secs:
        pipe.hgetall((key or OBS_KEY).format(sec=s))
    n = loss = 0
    hist = {}
    for row in pipe.execute():
        for k, v in (row or {}).items():
            if k == "n":
                n += int(v)
            elif k == "loss":
                loss += int(v)
            elif k.startswith("b"):
                hist[int(k[1:])] = hist.get(int(k[1:]), 0) + int(v)
    return {"n": n, "q_ms": ac.quantile_from_hist(hist, q),
            "loss": (loss / n) if n else 0.0, "secs": len(secs)}


def load_state(r):
    raw = r.get(STATE_KEY)
    return ac.State.from_json(raw) if raw else ac.State(cwnd=CFG.init_cwnd, ssthresh=CFG.max_cwnd)


def leader_tick(r, queue_depth, now=None, cfg=CFG):
    """Run one control step when due; publish the limit. No-op when off."""
    if not ENABLED:
        return None
    now = now or time.time()
    st = load_state(r)
    if now - st.last_step_ts >= cfg.step_s:
        inflight = max(0, int(r.get(INFLIGHT_KEY) or 0))
        # Demand = buyers still in the waiting room + requests queued at the
        # gate. Passing the waiting room alone (the first overnight grid did)
        # let step() decide "the drop is over" and reset the limit to its
        # initial 2 whenever the claim window had emptied the queue, even
        # with hundreds of checkouts waiting at the gate.
        queue_depth = queue_depth + max(0, int(r.get(WAITING_KEY) or 0))
        if MODE == "breakwater":
            stats = read_window(r, now, cfg.window_s, since=0.0, q=cfg.quantile, key=BW_OBS_KEY)
            st = breakwater_step(st, stats, cfg, now)
        else:
            stats = read_window(r, now, cfg.window_s, since=st.last_decrease_ts, q=cfg.quantile)
            st = ac.step(st, stats, inflight, queue_depth, cfg, now)
        st.cwnd = min(cfg.max_cwnd, st.cwnd)
        pipe = r.pipeline()
        pipe.set(STATE_KEY, st.to_json())
        pipe.set(LIMIT_KEY, max(1, int(st.cwnd)))
        pipe.execute()
    return st


def snapshot(r):
    if not ENABLED:
        return None
    raw = r.get(STATE_KEY)
    wait = r.hgetall(WAIT_KEY) or {}
    return {"state": json.loads(raw) if raw else None,
            "limit": int(r.get(LIMIT_KEY) or CFG.init_cwnd),
            "inflight": int(r.get(INFLIGHT_KEY) or 0),
            "wait": wait, "config": asdict(CFG)}


def reset(r):
    for pat in ("dblim:*",):
        for k in r.scan_iter(match=pat, count=1000):
            r.delete(k)
