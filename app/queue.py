"""
Virtual waiting room -- buyers queue for a sale before they may attempt
checkout at all.

THE PROBLEM THIS SOLVES
    Every earlier phase measured the same thing from a different angle: when
    1000 buyers arrive simultaneously, all 1000 reach a checkout handler and
    fight there. The pessimistic strategy serialises them on a row lock and
    the queue is invisible (it lives inside PostgreSQL). The optimistic
    strategy lets them collide and rejects the losers. Either way the
    contention is handled at the most expensive possible layer -- inside a
    database transaction, holding a pooled connection.

    A waiting room moves the queue somewhere cheap. Buyers wait in a Redis
    sorted set, which costs one O(log N) write each, and are released to
    checkout K at a time. The checkout endpoints stop seeing a herd and start
    seeing a rate-bounded stream. This is what Ticketmaster and SNKRS-style
    drops do, and the reason is not politeness -- it is that a lock queue
    1000 deep and a lock queue 10 deep are very different systems.

THE HOT PATH TOUCHES REDIS ONLY
    /queue/join does not query PostgreSQL. That is deliberate and it is the
    whole point: if absorbing the stampede required a database round trip,
    the stampede would just move from the checkout endpoint to the join
    endpoint and exhaust the same connection pool. Sale validation is done
    against a set of ids refreshed in the background (see _refresh_sales), so
    the request path stays Redis-only.

KEYS
    queue:z:{sale_id}                      ZSET  member=queue_token score=join ts
    queue:granted:{sale_id}:{queue_token}  STR   the admission token, TTL
    queue:tokens:{sale_id}                 SET   granted tokens, for reset
    admission:{admission_token}            STR   JSON {sale_id, queue_token, ...}

FLOW
    POST /queue/join/{sale_id}      -> {queue_token, position, estimated_wait_s}
    GET  /queue/status/{sale_id}?token=...
                                    -> {position, estimated_wait_s, admitted}
                                       and, once admitted, {admission_token}
    POST /checkout/{strategy}       with header X-Admission-Token
"""
import json
import math
import os
import threading
import time
import uuid

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from app.metrics import (
    QUEUE_ADMISSION_LEADER,
    QUEUE_ADMITS,
    QUEUE_DEPTH,
    QUEUE_JOINS,
    QUEUE_WAIT,
)

# How many buyers are released per tick, and how often. Together these set the
# admission RATE (default 10 per 500ms = 20/s), which is the single knob that
# decides how much concurrency the checkout endpoints ever see.
#
# THIS IS THE RATE ONE LEADER ADMITS AT, NOT THE FLEET'S RATE PRE-LEASE FIX.
# Before the admission-worker leader election below existed, N horizontally
# scaled replicas each ran their own admission-worker thread with no
# coordination, so the fleet's real admission rate was silently N times this
# number -- 3 replicas meant 60/s against a configured 20/s, discovered
# during the horizontal-scaling phase. The lease in start_admission_worker()
# ensures exactly one replica's loop is ever active, so this constant is now
# genuinely the fleet's rate again, regardless of replica count.
ADMIT_BATCH = int(os.getenv("QUEUE_ADMIT_BATCH", "10"))
ADMIT_INTERVAL_S = float(os.getenv("QUEUE_ADMIT_INTERVAL", "0.5"))

# How long an admission is good for. Long enough to complete a checkout and
# retry it once; short enough that a buyer who wanders off frees their slot.
ADMISSION_TTL_S = int(os.getenv("QUEUE_ADMISSION_TTL", "30"))

SALE_REFRESH_S = 5.0            # how often the known-sale set is re-read

# How long the admission-worker LEASE lasts before it must be renewed, and how
# often a replica tries to renew/acquire it. The lease is deliberately several
# ticks wide (not equal to ADMIT_INTERVAL_S) so ordinary scheduling jitter --
# the GIL, a slow tick -- never causes two replicas to both believe they hold
# it. A crashed leader's replacement takes over within one lease window, which
# trades a brief admission pause for never double-admitting.
ADMISSION_LEASE_MS = int(ADMIT_INTERVAL_S * 1000 * 4)

Z_KEY = "queue:z:{sale_id}"
GRANT_KEY = "queue:granted:{sale_id}:{queue_token}"
TOKENS_KEY = "queue:tokens:{sale_id}"
ADMISSION_KEY = "admission:{token}"
ADMISSION_LEADER_KEY = "queue:admission:leader"

router = APIRouter(prefix="/queue", tags=["queue"])

_redis = None                   # sync client, used by the background worker
_aredis = None                  # async client, used by the request path (see below)
_worker_started = False
_known_sales = set()            # refreshed in the background; empty = fail open


# --------------------------------------------------------------------------
# Core operations (pure Redis)
# --------------------------------------------------------------------------
def estimated_wait_s(position):
    """
    Seconds until this position is reached, at the current admission rate.

    Deliberately a straight-line estimate from the rate, not a prediction: it
    assumes nobody ahead abandons and the rate does not change. It is honest
    about being an estimate and it is what the client uses to decide how long
    to sleep between polls, which is the job that actually matters.
    """
    if position <= 0:
        return 0.0
    return round(math.ceil(position / max(1, ADMIT_BATCH)) * ADMIT_INTERVAL_S, 2)


# EVERYTHING ON THE REQUEST PATH IS ASYNC, AND THIS IS NOT A STYLE CHOICE.
#
# It was written with ordinary `def` endpoints first, and the N=1000 run
# showed exactly why that is wrong. FastAPI runs a sync endpoint in a bounded
# worker thread pool -- 40 threads by default -- and the checkout handlers are
# sync too, each holding a thread for the ~3.4s a contended checkout takes.
# So the waiting room ended up queueing for the same scarce resource as the
# thing it was built to protect: /queue/status polls stacked up behind
# checkouts, buyers learned they had been admitted long after the fact, and
# 559 admission tokens expired before their owner ever managed to use them.
#
# A waiting room that competes with checkout for threads is not a waiting
# room. These handlers touch nothing but Redis, so with an async client they
# run entirely on the event loop and cost no thread at all.
async def join(r, sale_id):
    """Add a buyer to the back of the queue. One ZADD, one ZRANK, nothing else."""
    queue_token = uuid.uuid4().hex
    now = time.time()
    zkey = Z_KEY.format(sale_id=sale_id)
    await r.zadd(zkey, {queue_token: now})
    rank = await r.zrank(zkey, queue_token)
    position = (rank if rank is not None else 0) + 1
    QUEUE_JOINS.labels(sale_id=sale_id).inc()
    return {
        "sale_id": sale_id,
        "queue_token": queue_token,
        "position": position,
        "estimated_wait_s": estimated_wait_s(position),
        "admitted": False,
        "joined_at": now,
    }


async def status(r, sale_id, queue_token):
    """
    Where a buyer stands. Three outcomes: still waiting, admitted, or gone.

    "Gone" covers both a token that never existed and one whose admission has
    expired, and those are deliberately not distinguished -- from the client's
    point of view the remedy is the same, rejoin.
    """
    grant_key = GRANT_KEY.format(sale_id=sale_id, queue_token=queue_token)
    grant = await r.get(grant_key)
    if grant:
        # THE WINDOW STARTS WHEN THE BUYER IS TOLD, NOT WHEN THE SERVER
        # DECIDED. Both keys get their full TTL again here, and that fixes a
        # real bug rather than being a nicety.
        #
        # Measured at N=1000: admission itself was near-instant (server-side
        # wait p50 0.3s, p95 3.7s), but a saturated event loop meant buyers
        # did not SEE the reply for tens of seconds. Against a TTL counted
        # from the grant, 627 buyers who had queued properly and been
        # admitted properly arrived at checkout holding a token that had
        # already expired, and were bounced with a 403. They were punished
        # for the server being slow to answer them.
        #
        # Counting the window from notification makes the grant mean what it
        # says -- "you have 30 seconds to shop" -- under any latency.
        pipe = r.pipeline()
        pipe.expire(ADMISSION_KEY.format(token=grant), ADMISSION_TTL_S)
        pipe.expire(grant_key, ADMISSION_TTL_S)
        await pipe.execute()
        return {
            "sale_id": sale_id,
            "queue_token": queue_token,
            "admitted": True,
            "admission_token": grant,
            "position": 0,
            "estimated_wait_s": 0.0,
            "expires_in_s": ADMISSION_TTL_S,
        }

    zkey = Z_KEY.format(sale_id=sale_id)
    rank = await r.zrank(zkey, queue_token)
    if rank is None:
        return {
            "sale_id": sale_id,
            "queue_token": queue_token,
            "admitted": False,
            "position": None,
            "estimated_wait_s": None,
            "expired": True,
            "message": "Unknown or expired queue token -- rejoin the queue.",
        }

    position = rank + 1
    return {
        "sale_id": sale_id,
        "queue_token": queue_token,
        "admitted": False,
        "position": position,
        "estimated_wait_s": estimated_wait_s(position),
        "ahead_of_you": rank,
        "queue_depth": await r.zcard(zkey),
    }


async def validate(r, admission_token, sale_id=None):
    """
    Is this admission token real, unexpired, and for this sale?

    Called by app/admission.py on every checkout request, from inside the
    event loop -- hence async, for the same reason as the endpoints above. A
    blocking Redis round trip here would stall every other request in the
    process for its duration, on the hottest path in the system.

    Deliberately does NOT consume the token: an admission is a shopping
    window, not a single-use ticket. An optimistic-strategy buyer who hits a
    version conflict must be able to retry within the window, and burning the
    token on the first attempt would turn a retryable 409 into a dead end.
    The TTL, not a use count, is what bounds the grant.
    """
    if not admission_token:
        return None
    raw = await r.get(ADMISSION_KEY.format(token=admission_token))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:                                          # noqa: BLE001
        return None
    if sale_id and data.get("sale_id") != sale_id:
        return None
    return data


def admit(r, sale_id, batch=None):
    """
    Release the next `batch` buyers from the front of the queue.

    ZPOPMIN is atomic and pops lowest-score-first, which is join order, so
    the queue is FIFO and two admission workers could not hand the same slot
    to two buyers. Everything after the pop is per-buyer bookkeeping.
    """
    batch = batch or ADMIT_BATCH
    zkey = Z_KEY.format(sale_id=sale_id)
    popped = r.zpopmin(zkey, batch)
    if not popped:
        return 0

    now = time.time()
    # One pipeline for the whole batch, not one per buyer. Each Redis round
    # trip costs a few milliseconds over Docker's port mapping, so a batch of
    # 10 done individually spent ~40 round trips inside a tick that is only
    # 500ms long -- the admission worker was eating its own interval and the
    # effective rate drifted below the configured one.
    pipe = r.pipeline()
    tokens_key = TOKENS_KEY.format(sale_id=sale_id)
    for queue_token, joined_at in popped:
        admission_token = uuid.uuid4().hex
        payload = json.dumps({
            "sale_id": sale_id,
            "queue_token": queue_token,
            "admitted_at": now,
            "waited_s": round(now - float(joined_at), 3),
        })
        pipe.set(ADMISSION_KEY.format(token=admission_token), payload, ex=ADMISSION_TTL_S)
        pipe.set(GRANT_KEY.format(sale_id=sale_id, queue_token=queue_token),
                 admission_token, ex=ADMISSION_TTL_S)
        # Tracked so POST /queue/reset can actually revoke them; without this
        # a reset would leave live tokens from the previous run able to check
        # out during the next one.
        pipe.sadd(tokens_key, admission_token)
        QUEUE_WAIT.observe(max(0.0, now - float(joined_at)))
        QUEUE_ADMITS.labels(sale_id=sale_id).inc()
    pipe.expire(tokens_key, ADMISSION_TTL_S * 4)
    pipe.execute()

    return len(popped)


async def reset(r, sale_id):
    """Empty a sale's queue and revoke every outstanding admission for it."""
    tokens_key = TOKENS_KEY.format(sale_id=sale_id)
    tokens = await r.smembers(tokens_key) or set()
    dropped = await r.zcard(Z_KEY.format(sale_id=sale_id))

    pipe = r.pipeline()
    for t in tokens:
        pipe.delete(ADMISSION_KEY.format(token=t))
    pipe.delete(tokens_key)
    pipe.delete(Z_KEY.format(sale_id=sale_id))
    await pipe.execute()

    async for key in r.scan_iter(match=GRANT_KEY.format(sale_id=sale_id, queue_token="*"),
                                 count=500):
        await r.delete(key)

    QUEUE_DEPTH.labels(sale_id=sale_id).set(0)
    return {"sale_id": sale_id, "dropped_from_queue": dropped,
            "revoked_admissions": len(tokens)}


# --------------------------------------------------------------------------
# Admission-worker leader election
#
# WHY THIS EXISTS: every API replica runs its own copy of the admission
# worker (see start_admission_worker() below), and until this lease existed
# nothing stopped every one of them from calling admit() on the same tick.
# With 3 replicas that meant admitting 3x the configured batch per interval
# -- a rate limiter that silently multiplies itself under horizontal
# scaling, found (not guessed) during the scaling phase by sweeping K and
# noticing the fleet's effective admission rate moved in lockstep with
# replica count rather than with K.
#
# The fix is a Redis lease, not a bigger architectural move (a dedicated
# singleton service/container) -- REJECTED for exactly the reason
# app/idempotency.py rejects a Redis lock in front of Stripe's own
# idempotency layer in app/payments.py: it would work, but it would ALSO
# mean this module behaves differently depending on how many processes
# happen to be running it, which is the opposite of what "horizontally
# scaled" is supposed to mean. A lease acquired fresh every tick means the
# code is identical whether it runs as the single host process or as N
# container replicas -- with N=1 the lease is always trivially free, so
# nothing changes for that deployment shape either.
# --------------------------------------------------------------------------
_worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

# Atomic compare-and-renew: extend the lease ONLY if we still hold it.
# Plain GET-then-PEXPIRE would have a real race -- the lease could expire
# and a different replica could win it in the gap between our GET and our
# PEXPIRE, and a bare PEXPIRE does not check the value, so we would extend
# a lease that had just become someone else's. Lua makes the read-and-act
# a single atomic step on the Redis server, closing that window entirely.
_RENEW_LEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], ARGV[2])
else
    return 0
end
"""


def _try_lead(r, lease_ms=None):
    """
    True if THIS process holds the admission-worker lease for this tick,
    having either just acquired it (nobody held it) or renewed it (we
    already did). False means a different replica is leading right now --
    admit() must not be called this tick.
    """
    lease_ms = lease_ms or ADMISSION_LEASE_MS
    if r.set(ADMISSION_LEADER_KEY, _worker_id, nx=True, px=lease_ms):
        return True
    return bool(r.eval(_RENEW_LEASE_SCRIPT, 1, ADMISSION_LEADER_KEY, _worker_id, lease_ms))


def is_admission_leader(r):
    """Read-only check, for /queue/config and debugging -- not used on the
    hot path, which calls _try_lead() directly."""
    return r.get(ADMISSION_LEADER_KEY) == _worker_id


# --------------------------------------------------------------------------
# Background admission worker
# --------------------------------------------------------------------------
def _queued_sale_ids(r):
    prefix = Z_KEY.format(sale_id="")
    return [k[len(prefix):] for k in r.scan_iter(match=f"{prefix}*", count=500)]


def _refresh_sales(session_factory):
    """
    Active sale ids, re-read every few seconds so /queue/join can validate
    without touching PostgreSQL on the request path. Read-only.
    """
    global _known_sales
    db = session_factory()
    try:
        rows = db.execute(text(
            "SELECT id FROM flash_sale_events WHERE end_time > now()"
        )).fetchall()
        _known_sales = {str(row[0]) for row in rows}
    finally:
        db.close()


def async_redis():
    """The event-loop-safe client. Exposed so app/admission.py can validate a
    token from inside its middleware without blocking the loop."""
    return _aredis


def sale_exists(sale_id):
    """Fails OPEN while the cache is cold -- a buyer must never be turned away
    from the queue because a background refresh has not run yet."""
    return (not _known_sales) or (sale_id in _known_sales)


def start_admission_worker(r, session_factory, interval_s=None):
    """
    TWO threads, and the split is the whole point.

      admit loop     pure Redis, fixed cadence, never touches PostgreSQL
      sale refresher slow, blocking, allowed to stall

    They were one thread first, with the sale refresh done inline every 5
    seconds, and that quietly destroyed the rate limiter. `_refresh_sales`
    takes a session from the SAME connection pool the checkout handlers are
    saturating; once ~30 checkouts are in flight the pool is empty, so the
    admission worker blocked waiting for a connection and admissions simply
    stopped until one came free.

    The symptom was unmistakable once measured: a sweep of K = 2, 5, 10, 25
    -- a 12x range of configured admission rate -- produced wall times of
    90.5s, 93.4s, 90.3s and 93.0s. The rate limiter was not limiting at the
    configured rate; it was limiting at whatever rate the exhausted pool let
    it run, and K had no effect at all.

    The general lesson is the same one the async endpoints taught: an
    admission controller must not share a scarce resource with the thing it
    is admitting people to, or it stops working exactly when it is needed.

    THIRD THING THIS FUNCTION GUARDS AGAINST, ADDED LATER: horizontal
    scaling. Guarding against double-start (below) stops ONE process from
    running the loop twice; it does nothing about N separate PROCESSES each
    running their own copy, which is exactly what 3 API replicas do. Every
    tick now goes through `_try_lead()`, a Redis lease that only one
    replica can hold at a time, before calling admit() -- see that
    function's docstring for why a lease was chosen over a dedicated
    singleton service.

    Guarded against double-start. `python -m app.main` executes this package
    once as __main__ and again when uvicorn imports "app.main:app", so an
    unguarded thread launch here would run TWO admission workers and silently
    double the admission rate.
    """
    global _worker_started
    if _worker_started:
        return None
    _worker_started = True

    interval_s = interval_s or ADMIT_INTERVAL_S

    def refresher():
        while True:
            try:
                _refresh_sales(session_factory)
            except Exception:                                  # noqa: BLE001
                pass
            time.sleep(SALE_REFRESH_S)

    threading.Thread(target=refresher, daemon=True,
                     name="queue-sale-refresher").start()

    def loop():
        # ABSOLUTE SCHEDULE, NOT sleep(interval) AT THE END OF THE BODY.
        #
        # The naive version drifts, and it drifted measurably: with a fixed
        # 500ms sleep the real period became sleep + work, which clocked in
        # around 800ms and dragged the effective admission rate down to
        # 12.5/s against a configured 20/s. That is not a rounding error --
        # it is a rate limiter that does not honour its own rate, which makes
        # every "K per interval" claim in results/queue_notes.md wrong by
        # 40%. Advancing a fixed deadline keeps the average exact and lets a
        # slow tick be absorbed by the next one.
        next_tick = time.monotonic()
        while True:
            try:
                # Redis only, by construction. Nothing in this block may
                # touch PostgreSQL -- see the docstring.
                #
                # Leadership gates ONLY admit() -- the one operation that
                # must never run twice for the same tick. QUEUE_DEPTH stays
                # unconditional: it is a read of shared Redis state (same
                # value regardless of who reads it), and every replica's
                # OWN /metrics series needs it populated -- Prometheus
                # scrapes each replica separately, so gating this too would
                # leave a permanent gap on every non-leader replica's series
                # rather than a real signal.
                is_leader = _try_lead(r)
                QUEUE_ADMISSION_LEADER.set(1 if is_leader else 0)
                for sale_id in _queued_sale_ids(r):
                    if is_leader:
                        admit(r, sale_id)
                    QUEUE_DEPTH.labels(sale_id=sale_id).set(
                        r.zcard(Z_KEY.format(sale_id=sale_id))
                    )
            except Exception:                                  # noqa: BLE001
                pass                                           # never kill the thread

            next_tick += interval_s
            delay = next_tick - time.monotonic()
            if delay < 0:
                # Fell behind (a slow tick, or the GIL held elsewhere). Resync
                # rather than trying to make up an unbounded backlog in one
                # burst, which would defeat the point of rate limiting.
                next_tick = time.monotonic()
            else:
                time.sleep(delay)

    t = threading.Thread(target=loop, daemon=True, name="queue-admission-worker")
    t.start()
    return t


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@router.post("/join/{sale_id}")
async def join_queue(sale_id: str):
    """
    Take a place in the waiting room.

    Returns immediately -- no polling, no blocking, no database access, and
    no worker thread. One Redis ZADD, one ZRANK, done.
    """
    if not sale_exists(sale_id):
        raise HTTPException(404, "Sale not found or already ended.")
    return await join(_aredis, sale_id)


@router.get("/status/{sale_id}")
async def queue_status(sale_id: str, token: str = Query(..., description="queue_token from /queue/join")):
    """
    Poll this until `admitted` is true, then send the returned
    `admission_token` as the `X-Admission-Token` header on checkout.

    `estimated_wait_s` is there to be used: a client 900 places back should
    sleep seconds between polls, not milliseconds. A thousand buyers polling
    tightly would rebuild the very stampede the queue exists to prevent --
    just against a different endpoint.
    """
    return await status(_aredis, sale_id, token)


@router.post("/reset/{sale_id}")
async def queue_reset(sale_id: str):
    """Clear the queue and revoke outstanding admissions -- for reruns.
    Same spirit as POST /admin/reset-sale, which rewinds stock."""
    return await reset(_aredis, sale_id)


@router.get("/config")
def queue_config():
    """What the admission rate currently is. The comparison in
    results/queue_notes.md turns on these three numbers, so they are readable
    rather than buried in environment variables.

    `admission_rate_per_s` is the FLEET rate, valid regardless of replica
    count, because exactly one replica's `_try_lead()` ever wins the lease
    per tick -- `is_leader_here` says whether it's THIS process, which is
    otherwise invisible from outside (there's no header or port that
    distinguishes which replica answered a request through nginx)."""
    return {
        "admit_batch": ADMIT_BATCH,
        "admit_interval_s": ADMIT_INTERVAL_S,
        "admission_rate_per_s": round(ADMIT_BATCH / ADMIT_INTERVAL_S, 2),
        "admission_ttl_s": ADMISSION_TTL_S,
        "is_leader_here": is_admission_leader(_redis),
        "worker_id": _worker_id,
    }


def install(app, redis_client, session_factory):
    """
    Mount the queue endpoints and start the admission worker.

    Builds a second, ASYNC Redis client pointed at the same server as the
    app's existing one -- connection details are read off that client rather
    than from the environment a second time, so the two can never drift apart
    and end up talking to different Redis instances. The sync client stays
    with the background worker (which is a thread and wants blocking calls);
    the async client serves the request path. See the note above join().
    """
    global _redis, _aredis
    import redis.asyncio as aioredis

    _redis = redis_client
    kw = redis_client.connection_pool.connection_kwargs
    _aredis = aioredis.Redis(
        host=kw.get("host", "localhost"),
        port=kw.get("port", 6379),
        db=kw.get("db", 0),
        password=kw.get("password"),
        decode_responses=True,
        max_connections=256,
    )

    app.include_router(router)
    start_admission_worker(redis_client, session_factory)
    return app
