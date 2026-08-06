"""
Chaos verification harness -- black-box concurrency testing of the three
checkout strategies.

Treats the running system entirely as a black box: every purchase goes over
the real HTTP API, exactly as a browser would. Nothing here imports or
patches the checkout code, and nothing writes to the database. Stock is set
up through the existing POST /admin/reset-sale endpoint; the only direct
database access is read-only counting for the invariant check (see
count_confirmed_orders() for why the API cannot answer that question).

THE ONE HARD INVARIANT
    confirmed orders created during a run <= stock the sale started with

Anything else it reports -- latency, duplicate outcomes, aborted requests --
is context. That single line is the pass/fail.

    python -m scripts.chaos_verification
    python -m scripts.chaos_verification --n 500 --chaos 0.4
    python -m scripts.chaos_verification --strategy redis --n 1000 --stock 10

Requires the API to be running (python -m app.main).
"""
import argparse
import asyncio
import os
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from sqlalchemy import text

from app.database import SessionLocal

STRATEGIES = ["pessimistic", "optimistic", "redis"]
DEFAULT_BASE_URL = os.getenv("CHAOS_BASE_URL", "http://127.0.0.1:8010")


# --------------------------------------------------------------------------
# Result records
# --------------------------------------------------------------------------
@dataclass
class Attempt:
    """One HTTP request. `logical_id` groups a duplicate with its original."""
    logical_id: int
    is_duplicate: bool
    status: str            # confirmed | sold_out | failed | aborted | error
    latency_ms: float | None
    http_code: int | None = None
    order_id: str | None = None
    detail: str = ""
    # True when the server served this from the idempotency cache rather than
    # re-executing checkout (the Idempotent-Replay response header).
    replayed: bool = False
    # Monotonic start/end of the CHECKOUT request only -- excludes any time
    # spent in the waiting room. These two are what make it possible to
    # measure how many checkout requests were ever in flight at once, which
    # is the whole claim the queue layer has to justify.
    sent_at: float | None = None
    done_at: float | None = None
    # Seconds this buyer spent queued before being admitted (None if the run
    # bypassed the queue).
    queue_wait_s: float | None = None


@dataclass
class StrategyRun:
    strategy: str
    sale_id: str
    n: int
    chaos_fraction: float
    starting_stock: int
    use_idempotency: bool = False
    use_queue: bool = False
    attempts: list[Attempt] = field(default_factory=list)
    wall_time_s: float = 0.0
    confirmed_in_db: int = 0          # ground truth, counted server-side
    baseline_orders: int = 0
    # Availability as each store reports it afterwards. Kept separate on
    # purpose: the strategies do not all write to the same counter, so a
    # single "available" number would look self-contradictory in the report.
    final_reported: int | None = None
    final_sql: int | None = None
    final_redis: int | None = None
    error: str | None = None

    # ---- derived ----
    @property
    def client_confirmed(self):
        return sum(1 for a in self.attempts if a.status == "confirmed")

    @property
    def sold_out(self):
        return sum(1 for a in self.attempts if a.status == "sold_out")

    @property
    def aborted(self):
        return sum(1 for a in self.attempts if a.status == "aborted")

    @property
    def failed(self):
        return sum(1 for a in self.attempts if a.status in ("failed", "error"))

    @property
    def oversold(self):
        """How far past the starting stock we went. 0 means the guarantee held."""
        return max(0, self.confirmed_in_db - self.starting_stock)

    @property
    def invariant_holds(self):
        return self.confirmed_in_db <= self.starting_stock

    @property
    def duplicate_pairs(self):
        """(logical_id, [statuses]) for every logical buyer that sent twice."""
        by_logical = {}
        for a in self.attempts:
            by_logical.setdefault(a.logical_id, []).append(a)
        return {k: v for k, v in by_logical.items() if len(v) > 1}

    @property
    def duplicates_both_confirmed(self):
        """
        Pairs where both requests came back 'confirmed'.

        NOTE this is NOT the same as "bought twice", and the distinction only
        appears once idempotency is on. A replayed response is also
        'confirmed' -- correctly so, it is the original result being handed
        back -- so this count staying high while replays happen is the
        mechanism working, not failing. See duplicates_distinct_orders(),
        which is the metric that actually tracks the bug.
        """
        return sum(
            1 for v in self.duplicate_pairs.values()
            if sum(1 for a in v if a.status == "confirmed") > 1
        )

    @property
    def duplicates_distinct_orders(self):
        """
        Pairs that produced TWO DIFFERENT order_ids -- i.e. one purchase
        intent that really did buy twice. This is the defect idempotency
        exists to remove, and the number that should go to zero.
        """
        n = 0
        for attempts in self.duplicate_pairs.values():
            ids = {a.order_id for a in attempts
                   if a.status == "confirmed" and a.order_id}
            if len(ids) > 1:
                n += 1
        return n

    @property
    def replayed(self):
        """Requests the server answered from the idempotency cache."""
        return sum(1 for a in self.attempts if a.replayed)

    @property
    def duplicates_replayed(self):
        """Duplicate pairs where the retry was served from cache -- the
        mechanism by which double-confirm is meant to be prevented."""
        return sum(
            1 for v in self.duplicate_pairs.values()
            if any(a.replayed for a in v)
        )

    @property
    def admission_refused(self):
        """Requests the door turned away (HTTP 403). Should be 0 in both
        arms: zero when the queue is off because there is no door, and zero
        when it is on because every buyer queued first. Anything else means
        the harness raced its own admission."""
        return sum(1 for a in self.attempts if a.http_code == 403)

    @property
    def max_checkout_concurrency(self):
        """
        The most checkout requests ever in flight simultaneously, by sweep
        line over each request's (sent_at, done_at).

        Measured client-side and deliberately so: it is an independent check
        on the server's `checkout_in_flight` gauge, which Prometheus only
        samples every 2s and can therefore miss a peak between scrapes. This
        sees every interval exactly.
        """
        events = []
        for a in self.attempts:
            if a.sent_at is not None and a.done_at is not None:
                events.append((a.sent_at, 1))
                events.append((a.done_at, -1))
        if not events:
            return None
        # -1 before +1 at equal timestamps, so a request that ends exactly as
        # another begins is not double-counted.
        events.sort(key=lambda e: (e[0], e[1]))
        cur = peak = 0
        for _, delta in events:
            cur += delta
            peak = max(peak, cur)
        return peak

    @property
    def queue_waits_s(self):
        return [a.queue_wait_s for a in self.attempts if a.queue_wait_s is not None]

    def queue_wait_percentile(self, p):
        waits = sorted(self.queue_waits_s)
        if not waits:
            return None
        k = max(0, min(len(waits) - 1, int(round((p / 100) * len(waits) + 0.5)) - 1))
        return waits[k]

    def percentile(self, p):
        lat = sorted(a.latency_ms for a in self.attempts if a.latency_ms is not None)
        if not lat:
            return None
        k = max(0, min(len(lat) - 1, int(round((p / 100) * len(lat) + 0.5)) - 1))
        return lat[k]


# --------------------------------------------------------------------------
# Read-only database access (counting only -- never writes)
# --------------------------------------------------------------------------
def count_confirmed_orders(sale_id):
    """
    Ground truth for the invariant.

    Deliberately NOT read from GET /sales/{id}: that reports availability,
    and the Redis fast path does not decrement inventory.reserved_stock at
    all (by design -- see the CAP note on /checkout/redis), so SQL
    availability would under-report Redis purchases and quietly hide a real
    oversell. The orders table is the only place all three strategies agree
    on what was actually sold.

    Equally deliberately NOT counted from the harness's own successful
    responses: a client that aborts mid-flight can leave a committed order it
    never saw the confirmation for. Counting client-side would miss exactly
    the orders chaos testing exists to find.

    Counted as an absolute total, and attributed to a run by subtracting a
    baseline taken just before it starts.

    An earlier version filtered `created_at >= <cutoff captured on this
    host>` instead, which was subtly and dangerously wrong: the application
    clock and the PostgreSQL container's clock are not synchronised, so a
    host-generated cutoff silently excluded real orders whose server-side
    timestamp fell on the wrong side of the skew. It reported 6 confirmed
    orders in a run where 38 clients were told "confirmed" -- and because it
    UNDER-counted, it would have hidden an oversell rather than surfaced one.
    A before/after delta needs no clock agreement at all.

    SELECT only. This function is the sole database contact in this file.
    """
    db = SessionLocal()
    try:
        return db.execute(
            text("SELECT count(*) FROM orders "
                 "WHERE sale_id = :sid AND status = 'confirmed'"),
            {"sid": sale_id},
        ).scalar() or 0
    finally:
        db.close()


def fetch_user_ids(limit):
    """Real user ids for the FK on orders.user_id. Read-only."""
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT id FROM users ORDER BY created_at NULLS LAST LIMIT :lim"),
            {"lim": max(1, limit)},
        ).fetchall()
        return [str(r[0]) for r in rows]
    finally:
        db.close()


# --------------------------------------------------------------------------
# Setup via the existing API
# --------------------------------------------------------------------------
async def discover_sales(client):
    r = await client.get("/sales")
    r.raise_for_status()
    return r.json().get("sales", [])


async def reset_queue(client, sale_id):
    """Empty the waiting room and revoke outstanding admissions between runs.
    Without this, tokens granted to the previous run are still live for their
    30s TTL and could let a request through the door it should have waited
    at."""
    try:
        r = await client.post(f"/queue/reset/{sale_id}")
        return r.json() if r.status_code == 200 else None
    except Exception:                                          # noqa: BLE001
        return None


async def queue_config(client):
    try:
        r = await client.get("/queue/config")
        return r.json() if r.status_code == 200 else None
    except Exception:                                          # noqa: BLE001
        return None


async def acquire_admission(client, sale_id, timeout_s=240.0):
    """
    Join the waiting room and poll until admitted. Returns
    (admission_token, waited_seconds), or (None, waited) on timeout.

    THE POLL INTERVAL IS DERIVED FROM THE SERVER'S OWN ESTIMATE, not fixed.
    A thousand buyers polling every 100ms would put ~10k requests/second on
    /queue/status and rebuild the exact stampede the queue exists to prevent
    -- just aimed at a different endpoint. So a buyer 900 places back sleeps
    seconds between checks and one near the front sleeps milliseconds, which
    keeps total poll traffic roughly proportional to the queue's drain rate
    instead of to its depth.
    """
    started = time.perf_counter()
    try:
        r = await client.post(f"/queue/join/{sale_id}")
        if r.status_code != 200:
            return None, time.perf_counter() - started
        body = r.json()
    except Exception:                                          # noqa: BLE001
        return None, time.perf_counter() - started

    token = body.get("queue_token")
    est = body.get("estimated_wait_s") or 0.5
    deadline = started + timeout_s

    while time.perf_counter() < deadline:
        await asyncio.sleep(min(max(est * 0.5, 0.2), 3.0))
        try:
            s = await client.get(f"/queue/status/{sale_id}", params={"token": token})
            if s.status_code != 200:
                continue
            sb = s.json()
        except Exception:                                      # noqa: BLE001
            continue
        if sb.get("admitted"):
            return sb.get("admission_token"), time.perf_counter() - started
        if sb.get("expired"):
            return None, time.perf_counter() - started
        est = sb.get("estimated_wait_s") or 0.5

    return None, time.perf_counter() - started


async def reset_sale(client, sale_id, stock, token=""):
    """Uses the existing admin endpoint -- resets PostgreSQL inventory AND the
    Redis counter together, which is what makes 'starting stock' exact for
    all three strategies."""
    params = {"stock": stock}
    if token:
        params["token"] = token
    r = await client.post(f"/admin/reset-sale/{sale_id}", params=params)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------
# Chaos
# --------------------------------------------------------------------------
class Chaos:
    """
    Client-side chaos only -- the server is never touched or degraded.

      delay     a random pause before firing, smearing the thundering herd
      abort     the request is cancelled mid-flight (dropped client). The
                server may still commit; that is the point.
      duplicate the same logical purchase is sent a second time.

    Duplicates are sent as two INDEPENDENT requests because the API has no
    idempotency key yet. Both succeeding is therefore the expected outcome,
    not a bug -- it is recorded so the eventual idempotency work has a
    before-picture.
    """

    def __init__(self, fraction, rng):
        self.fraction = fraction
        self.rng = rng

    def roll(self):
        if self.rng.random() >= self.fraction:
            return {"delay": 0.0, "abort": False, "duplicate": False}
        kind = self.rng.choice(["delay", "abort", "duplicate"])
        return {
            "delay": self.rng.uniform(0.005, 0.25) if kind == "delay" else 0.0,
            "abort": kind == "abort",
            "duplicate": kind == "duplicate",
        }


async def one_purchase(client, strategy, sale_id, user_id, logical_id,
                       plan, barrier, results, is_duplicate=False,
                       idem_key=None, admission_token=None, queue_wait_s=None):
    """
    A single checkout request.

    `idem_key` is one UUID per LOGICAL purchase intent, reused verbatim by
    that intent's retry. That is the whole point: a retry is the same
    purchase being asked for again, not a second purchase. Passing None
    reproduces the original behaviour (two independent requests).

    `admission_token` is the waiting-room grant, sent as X-Admission-Token.
    Passing None reproduces the pre-queue behaviour, which the server only
    accepts when ADMISSION_ENFORCED=false.
    """
    await barrier.wait()

    if plan["delay"]:
        await asyncio.sleep(plan["delay"])

    started = time.perf_counter()
    params = {"sale_id": sale_id, "user_id": user_id}
    headers = {}
    if idem_key:
        headers["Idempotency-Key"] = idem_key
    if admission_token:
        headers["X-Admission-Token"] = admission_token
    headers = headers or None
    try:
        if plan["abort"] and not is_duplicate:
            # Cancel mid-flight: the connection dies before the response is
            # read. Whether the server committed is genuinely unknown here --
            # which is exactly the ambiguity being tested.
            try:
                await asyncio.wait_for(
                    client.post(f"/checkout/{strategy}", params=params, headers=headers),
                    timeout=random.uniform(0.001, 0.02),
                )
            except (asyncio.TimeoutError, httpx.HTTPError):
                results.append(Attempt(logical_id, is_duplicate, "aborted", None,
                                       detail="client aborted mid-flight",
                                       sent_at=started, done_at=time.perf_counter(),
                                       queue_wait_s=queue_wait_s))
                return
            # It answered before we could cut it -- fall through as normal.

        resp = await client.post(f"/checkout/{strategy}", params=params, headers=headers)
        finished = time.perf_counter()
        elapsed = (finished - started) * 1000
        try:
            body = resp.json()
        except Exception:                                      # noqa: BLE001
            body = {}
        results.append(Attempt(
            logical_id, is_duplicate,
            body.get("status", "failed"), elapsed, resp.status_code,
            body.get("order_id"), body.get("message", "")[:120],
            replayed=resp.headers.get("idempotent-replay", "").lower() == "true",
            sent_at=started, done_at=finished, queue_wait_s=queue_wait_s,
        ))
    except Exception as exc:                                   # noqa: BLE001
        finished = time.perf_counter()
        results.append(Attempt(logical_id, is_duplicate, "error",
                               (finished - started) * 1000,
                               detail=f"{type(exc).__name__}: {exc}"[:120],
                               sent_at=started, done_at=finished,
                               queue_wait_s=queue_wait_s))


async def one_buyer(client, strategy, sale_id, user_id, logical_id, plan,
                    barrier, results, idem_key=None, use_queue=True,
                    dup_plan=None):
    """
    One buyer's whole journey: wait for the drop, queue if the queue is on,
    then send their checkout -- and their retry, if chaos gave them one.

    THE RETRY REUSES THE SAME ADMISSION. A buyer who times out and resends is
    still the same person in the same shopping window; making them rejoin the
    back of a 1000-deep queue would be wrong behaviourally and would also
    wreck the idempotency measurement, since the retry would then land ~50s
    after its original rather than in the same contention window. One buyer,
    one queue slot, up to two requests.
    """
    await barrier.wait()

    if plan["delay"]:
        # The chaos delay smears arrival at the FRONT DOOR -- the queue when
        # queueing, checkout when not -- so it means the same thing in both
        # arms: buyers do not all show up on the same millisecond.
        await asyncio.sleep(plan["delay"])

    token, waited = None, None
    if use_queue:
        token, waited = await acquire_admission(client, sale_id)
        if token is None:
            results.append(Attempt(logical_id, False, "error", None,
                                   detail="never admitted from the queue",
                                   queue_wait_s=waited))
            return

    # Already delayed above; don't charge it twice.
    fire = dict(plan, delay=0.0)
    tasks = [one_purchase(client, strategy, sale_id, user_id, logical_id, fire,
                          barrier, results, idem_key=idem_key,
                          admission_token=token, queue_wait_s=waited)]
    if dup_plan is not None:
        tasks.append(one_purchase(client, strategy, sale_id, user_id, logical_id,
                                  dup_plan, barrier, results, is_duplicate=True,
                                  idem_key=idem_key, admission_token=token,
                                  queue_wait_s=waited))
    await asyncio.gather(*tasks, return_exceptions=True)


def make_idem_key(run_nonce, strategy, logical_id):
    """
    One stable UUID per (invocation, strategy, logical buyer).

    Deliberately NOT drawn from the seeded RNG. Seeded keys look attractive
    for reproducibility but are actively wrong here, in two ways:

      * Across invocations -- re-running the same command would regenerate
        the same keys, and the 24h cache would replay the PREVIOUS run's
        responses. The "after" arm would create zero orders and look like a
        catastrophic regression rather than a repeat.
      * Across strategies -- every strategy seeds its RNG identically, so
        `--strategy all` would have pessimistic's keys collide with
        optimistic's, and strategies 2 and 3 would just replay strategy 1.

    A per-invocation nonce fixes both while keeping the property that
    actually matters for the experiment: BOTH ARMS of a --compare share the
    nonce, so the with/without comparison still sends identical keys.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"chaos:{run_nonce}:{strategy}:{logical_id}"))


async def run_strategy(base_url, strategy, sale_id, n, chaos_fraction,
                       stock, user_ids, admin_token, seed, use_idempotency=True,
                       run_nonce="", use_queue=True):
    run = StrategyRun(strategy=strategy, sale_id=sale_id, n=n,
                      chaos_fraction=chaos_fraction, starting_stock=stock,
                      use_idempotency=use_idempotency, use_queue=use_queue)
    rng = random.Random(seed)
    chaos = Chaos(chaos_fraction, rng)

    # A queued run holds a connection per waiting buyer for the whole drain,
    # and the timeout has to outlast the deepest queue: at 20 admissions/s,
    # buyer 1000 waits ~50s before its checkout is even sent.
    timeout = 240.0 if use_queue else 30.0
    limits = httpx.Limits(max_connections=n + 50, max_keepalive_connections=n + 50)
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout, limits=limits) as client:
        try:
            await reset_sale(client, sale_id, stock, admin_token)
            await reset_queue(client, sale_id)
        except Exception as exc:                               # noqa: BLE001
            run.error = f"could not reset sale: {exc}"
            return run

        # Baseline: everything above this count belongs to this run. Clock
        # agreement between host and database is deliberately not required.
        run.baseline_orders = count_confirmed_orders(sale_id)
        await asyncio.sleep(0.05)

        results = []
        barrier = asyncio.Event()
        tasks = []
        for i in range(n):
            plan = chaos.roll()
            uid = user_ids[i % len(user_ids)]
            # One key per logical purchase INTENT -- see make_idem_key().
            idem_key = make_idem_key(run_nonce, strategy, i) if use_idempotency else None
            # The SAME logical buyer retrying -- same key, so with
            # idempotency on this is one purchase asked for twice, and with
            # it off it is two independent purchases (the old behaviour,
            # kept for the before/after comparison).
            dup_plan = ({"delay": rng.uniform(0.0, 0.05),
                         "abort": False, "duplicate": False}
                        if plan["duplicate"] else None)
            tasks.append(one_buyer(client, strategy, sale_id, uid, i, plan,
                                   barrier, results, idem_key=idem_key,
                                   use_queue=use_queue, dup_plan=dup_plan))

        gathered = asyncio.gather(*tasks)
        t0 = time.perf_counter()
        barrier.set()                       # release the herd
        await gathered
        run.wall_time_s = time.perf_counter() - t0

        run.attempts = results
        # Let any in-flight commit from an aborted connection land.
        await asyncio.sleep(0.75)
        run.confirmed_in_db = count_confirmed_orders(sale_id) - run.baseline_orders

        try:
            body = (await client.get(f"/sales/{sale_id}")).json()
            run.final_reported = body.get("available")
            run.final_sql = body.get("sql_available")
            run.final_redis = body.get("redis_available")
        except Exception:                                      # noqa: BLE001
            pass

    return run


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def fmt(v, suffix=""):
    return "--" if v is None else f"{v:.1f}{suffix}"


def build_report(runs, args, sale_names):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    any_violation = any(not r.invariant_holds for r in runs if not r.error)
    any_dupes = any(r.duplicates_both_confirmed for r in runs if not r.error)

    L = []
    L.append("# Chaos Verification Report\n")
    L.append(f"Generated: {now}\n")
    L.append(
        f"Black-box run against the live HTTP API at `{args.base_url}`. "
        f"No checkout code, schema, or configuration was modified; stock was "
        f"prepared through the existing `POST /admin/reset-sale` endpoint and "
        f"verified with read-only `SELECT`s.\n"
    )
    L.append("## Configuration\n")
    L.append(f"- Concurrent buyers (N): **{args.n}** per strategy")
    L.append(f"- Chaos fraction: **{args.chaos:.0%}** of requests")
    L.append(f"- Starting stock: **{args.stock}** units per sale")
    L.append(f"- Strategies: {', '.join(r.strategy for r in runs)}")
    L.append(f"- RNG seed: {args.seed} (re-run with the same seed to reproduce)")
    if getattr(args, "queue", True):
        cfg = getattr(args, "_queue_config", None) or {}
        rate = cfg.get("admission_rate_per_s", "?")
        L.append(f"- Front door: **waiting room ON** — buyers join "
                 f"`/queue/join/{{sale_id}}` and are admitted "
                 f"{cfg.get('admit_batch', '?')} every "
                 f"{cfg.get('admit_interval_s', '?')}s (**{rate}/s**)\n")
    else:
        L.append("- Front door: **waiting room BYPASSED** (`--no-queue`) — the raw "
                 "pre-queue herd, requires `ADMISSION_ENFORCED=false`\n")

    L.append("## The invariant\n")
    L.append("> **Confirmed orders created during a run must never exceed the "
             "stock the sale started with.**\n")
    L.append(
        "Counted from the `orders` table rather than from this harness's own "
        "responses. A client that aborts mid-flight can leave behind a "
        "committed order it never saw confirmed, so a client-side tally would "
        "under-count exactly the case chaos testing exists to surface. It is "
        "also not read from `GET /sales/{id}`: the Redis fast path never "
        "decrements `inventory.reserved_stock`, so SQL availability would "
        "under-report Redis purchases.\n"
    )

    L.append("## Summary\n")
    L.append("| Strategy | Idem | N | Stock | Confirmed (DB) | Oversold | Invariant | "
             "Sold out | Aborted | Failed | Dup pairs | Bought twice | Both said OK | "
             "Replayed | p50 | p95 | p99 | Wall |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in runs:
        idem = "on" if r.use_idempotency else "off"
        if r.error:
            L.append(f"| {r.strategy} | {idem} | {r.n} | {r.starting_stock} | ERROR |"
                     + " -- |" * 13)
            continue
        L.append(
            f"| `{r.strategy}` | {idem} | {r.n} | {r.starting_stock} | "
            f"**{r.confirmed_in_db}** | {r.oversold} | "
            f"{'✅ HOLDS' if r.invariant_holds else '❌ VIOLATED'} | "
            f"{r.sold_out} | {r.aborted} | {r.failed} | {len(r.duplicate_pairs)} | "
            f"**{r.duplicates_distinct_orders}** | {r.duplicates_both_confirmed} | "
            f"{r.replayed} | "
            f"{fmt(r.percentile(50), 'ms')} | {fmt(r.percentile(95), 'ms')} | "
            f"{fmt(r.percentile(99), 'ms')} | {r.wall_time_s:.2f}s |"
        )
    L.append("")
    L.append(
        "**Bought twice** = duplicate pairs that produced two different `order_id`s "
        "(the real defect). **Both said OK** = pairs where both responses read "
        "`confirmed`, which after idempotency includes replays of the *same* order "
        "and is therefore expected to stay high. Read the first column.\n"
    )

    # ---- before/after, only when both arms were run at the same seed ----
    paired = {}
    for r in runs:
        if not r.error:
            paired.setdefault(r.strategy, {})[r.use_idempotency] = r
    both = {k: v for k, v in paired.items() if False in v and True in v}
    if both:
        L.append("## Before / after: Idempotency-Key\n")
        L.append(
            f"Each strategy run twice at seed **{args.seed}** with identical "
            f"parameters. The chaos plan is seeded, so both arms send the same "
            f"requests in the same order, and both arms share one "
            f"per-invocation key namespace, so a retry in either arm carries "
            f"the same key its original would have. The only difference is "
            f"whether that header is sent at all. Same experiment, one "
            f"variable.\n"
        )
        L.append(
            "(Keys are derived from a nonce generated fresh each invocation "
            "rather than from the seed. Seeded keys would be reproducible "
            "across *runs*, which sounds desirable but means a re-run replays "
            "the previous run's 24h cache and creates zero orders -- and with "
            "`--strategy all`, every strategy would share one key space and "
            "strategies 2 and 3 would replay strategy 1.)\n"
        )
        L.append("| Strategy | Dup pairs | **Bought twice BEFORE** | **Bought twice AFTER** | "
                 "Retries served from cache | Confirmed (DB) before → after | Invariant |")
        L.append("|---|---|---|---|---|---|---|")
        for st, arm in both.items():
            before, after = arm[False], arm[True]
            ok = "✅" if (after.invariant_holds and before.invariant_holds) else "❌"
            L.append(
                f"| `{st}` | {len(before.duplicate_pairs)} | "
                f"**{before.duplicates_distinct_orders}** | "
                f"**{after.duplicates_distinct_orders}** | {after.replayed} | "
                f"{before.confirmed_in_db} → {after.confirmed_in_db} | {ok} |"
            )
        L.append("")
        L.append(
            '> "Bought twice" counts duplicate pairs that produced **two different '
            '`order_id`s** — one intent that really did purchase twice. That is the '
            "defect, and it is deliberately not the same as *both responses said "
            '"confirmed"*: once idempotency is on, a replayed response is also '
            "`confirmed`, because it is the original result being handed back. Both "
            "counts appear in the summary table above; only this one tracks the "
            "bug.\n"
        )

        fixed = [s for s, a in both.items()
                 if a[False].duplicates_distinct_orders > 0
                 and a[True].duplicates_distinct_orders == 0]
        none_before = all(a[False].duplicates_distinct_orders == 0 for a in both.values())
        if fixed:
            L.append(
                f"**{', '.join('`'+s+'`' for s in fixed)}: double-confirms went to zero.** "
                f"The retry now hits `idempotency:{{key}}` and the cached response is "
                f"replayed verbatim -- same `order_id`, same status -- without the "
                f"checkout handler running a second time.\n"
            )
        elif none_before:
            L.append(
                "**No double-confirms in either arm at this configuration.** That is "
                "not evidence idempotency worked -- at this contention level the "
                "retry loses the race for the last unit and is rejected as sold out "
                "regardless. Scarcity, not deduplication, is doing the work. Use the "
                "low-contention control below, where stock is not the limiting "
                "factor, to see the difference.\n"
            )
        L.append(
            "The `Replayed` column counts responses the server served from cache "
            "(the `Idempotent-Replay: true` header). It is the direct evidence that "
            "the mechanism engaged, as opposed to inferring it from an absence.\n"
        )

    # ---- waiting room ----
    L.append("## Waiting room\n")
    L.append("| Strategy | Queue | Buyers | **Max concurrent checkouts** | "
             "Queue wait p50 / p95 / max | Refused at door (403) |")
    L.append("|---|---|---|---|---|---|")
    for r in runs:
        if r.error:
            continue
        waits = r.queue_waits_s
        wtxt = "--" if not waits else (
            f"{r.queue_wait_percentile(50):.1f}s / "
            f"{r.queue_wait_percentile(95):.1f}s / {max(waits):.1f}s")
        L.append(f"| `{r.strategy}` | {'on' if r.use_queue else '**off**'} | {r.n} | "
                 f"**{r.max_checkout_concurrency}** | {wtxt} | {r.admission_refused} |")
    L.append("")
    L.append(
        "**Max concurrent checkouts** is the peak number of checkout requests in "
        "flight at the same instant, computed by sweep line over every request's "
        "start and end time. It is the number the waiting room exists to bound: "
        "with the queue off it should approach the buyer count, and with the queue "
        "on it should stay near the admission batch size no matter how many people "
        "showed up.\n"
    )
    L.append(
        "It is measured client-side on purpose. The server also publishes "
        "`checkout_in_flight`, but Prometheus samples that every 2 seconds and can "
        "step over a peak between scrapes; this sees every interval. Two "
        "independent measurements of the same quantity is the point.\n"
    )

    verdict = "❌ **AT LEAST ONE INVARIANT VIOLATION**" if any_violation \
        else "✅ **All invariants held.**"
    L.append(f"### Verdict: {verdict}\n")
    if not any_violation:
        L.append(
            "Under this load, with chaos injected, no strategy sold more units "
            "than it had. Note that this is evidence, not proof: concurrency "
            "bugs are probabilistic, and a passing run bounds how easily a "
            "defect reproduces rather than showing there is none. Re-run at "
            "higher N and higher chaos before treating it as settled.\n"
        )

    # ---- per-strategy detail ----
    L.append("## Per-strategy detail\n")
    for r in runs:
        L.append(f"### `{r.strategy}` — sale `{r.sale_id}`")
        L.append(f"*{sale_names.get(r.sale_id, 'unknown product')}*\n")
        if r.error:
            L.append(f"**Run failed:** {r.error}\n")
            continue

        L.append(f"- Requests sent: **{len(r.attempts)}** "
                 f"({r.n} logical buyers + {len(r.attempts) - r.n} chaos duplicates)")
        L.append(f"- Confirmed in database: **{r.confirmed_in_db}** / {r.starting_stock} stock")
        L.append(f"- Confirmed seen by clients: {r.client_confirmed}")
        gap = r.confirmed_in_db - r.client_confirmed
        if gap > 0:
            L.append(f"  - **{gap} order(s) committed that no client saw confirmed** — "
                     f"aborted connections whose transaction still landed. Invisible "
                     f"to the buyer, real in the database.")
        elif gap < 0:
            L.append(f"  - **{-gap} more client(s) were told 'confirmed' than there are "
                     f"orders** — and that is the idempotency layer working, not a "
                     f"discrepancy. A replayed retry legitimately reports the "
                     f"confirmation of the order its first attempt already created, "
                     f"so N confirmations can correspond to fewer than N orders. "
                     f"Cross-check: {r.replayed} response(s) were served from cache.")
        L.append(f"- Rejected as sold out: {r.sold_out}")
        L.append(f"- Aborted mid-flight (chaos): {r.aborted}")
        L.append(f"- Failed / errored: {r.failed}")
        L.append(f"- Final availability — reported `{r.final_reported}` "
                 f"(SQL `{r.final_sql}`, Redis `{r.final_redis}`)")
        if (r.final_sql is not None and r.final_redis is not None
                and r.final_sql != r.final_redis):
            owner = "Redis" if r.strategy == "redis" else "PostgreSQL"
            other = "PostgreSQL" if r.strategy == "redis" else "Redis"
            L.append(f"  - The two disagree, which is expected here rather than "
                     f"alarming: `{r.strategy}` decrements **{owner}** only, so "
                     f"**{other}** still shows its pre-run figure. Each strategy "
                     f"owns one counter; nothing reconciles them, by design (see "
                     f"the CAP note on `/checkout/redis`). It is also why the "
                     f"invariant above is counted from the `orders` table, which "
                     f"every strategy writes to, instead of from either counter.")
        L.append(f"- Latency p50 / p95 / p99: {fmt(r.percentile(50), 'ms')} / "
                 f"{fmt(r.percentile(95), 'ms')} / {fmt(r.percentile(99), 'ms')}")
        L.append(f"- Wall time: {r.wall_time_s:.2f}s "
                 f"({len(r.attempts) / max(r.wall_time_s, 1e-9):.0f} req/s)\n")

        if not r.invariant_holds:
            L.append(f"> ❌ **INVARIANT VIOLATED — oversold by {r.oversold}.**")
            L.append(f"> `{r.strategy}` confirmed {r.confirmed_in_db} orders against "
                     f"{r.starting_stock} units of stock. Every extra order is a unit "
                     f"promised to a customer that does not exist. This is a real "
                     f"finding and is reported as-is; the underlying implementation "
                     f"was deliberately not changed to make this pass.\n")

        dp = r.duplicate_pairs
        if dp:
            both = r.duplicates_both_confirmed
            L.append(f"**Duplicate requests:** {len(dp)} logical buyer(s) sent the same "
                     f"purchase twice (simulated retry-after-timeout).")
            if both:
                L.append(f"{both} of those had **both requests confirmed** — the same "
                         f"buyer got two units from one intent.\n")
                L.append("> Expected for now: the API has no idempotency key, so two "
                         "independent POSTs are two independent purchases and the "
                         "server has no way to know they were one intent. This does "
                         "**not** violate the stock invariant — the total is still "
                         "capped — but it is the exact behaviour idempotency is meant "
                         "to remove, recorded here as a before-picture for that work.\n")
            else:
                L.append("None of them had both requests confirmed — in this run the "
                         "duplicates lost the race for remaining stock rather than "
                         "being deduplicated. That is scarcity doing the work, not "
                         "idempotency; at higher stock levels both would be expected "
                         "to succeed.\n")

    # ---- closing notes ----
    L.append("## Reading these numbers\n")
    L.append("- **Aborted** requests have no latency recorded — they were cancelled "
             "before a response arrived, so percentiles cover completed requests only.")
    L.append("- **Failed** includes optimistic-strategy version conflicts, which are "
             "the OCC design working (a losing writer is told to retry), not an error.")
    L.append("- A `confirmed (DB)` count *lower* than stock is normal when chaos "
             "aborts requests before they reach the server.")
    if any_dupes:
        L.append("- **Duplicates both succeeding is currently expected** and is "
                 "tracked separately from the stock invariant.")
    L.append("")
    L.append("Re-run at other scales:\n")
    L.append("```bash")
    L.append("python -m scripts.chaos_verification --n 1000 --chaos 0.5")
    L.append("python -m scripts.chaos_verification --strategy optimistic --n 500 --stock 25")
    L.append("```")
    return "\n".join(L)


# --------------------------------------------------------------------------
async def main_async(args):
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as client:
        try:
            sales = await discover_sales(client)
        except Exception as exc:                               # noqa: BLE001
            print(f"ERROR: cannot reach the API at {args.base_url} ({exc}).")
            print("Start it with:  python -m app.main")
            return 1

    if not sales:
        print("ERROR: no active sales found.")
        print("Seed some with:  python -m scripts.seed_storefront")
        return 1

    targets = STRATEGIES if args.strategy == "all" else [args.strategy]

    # A distinct sale per strategy where possible, so runs cannot contaminate
    # each other. Falls back to reusing one -- the per-run cutoff timestamp
    # keeps the counting correct either way.
    sale_names = {s["sale_id"]: s["product"] for s in sales}
    assigned = {st: sales[i % len(sales)]["sale_id"] for i, st in enumerate(targets)}
    if len(sales) < len(targets):
        print(f"NOTE: only {len(sales)} sale(s) available for {len(targets)} strategies; "
              f"reusing sales. Counting stays correct (per-run cutoff).")

    user_ids = fetch_user_ids(max(args.n, 30))
    if not user_ids:
        print("ERROR: no users in the database. Run:  python -m scripts.seed")
        return 1

    print(f"Chaos verification — N={args.n}, chaos={args.chaos:.0%}, stock={args.stock}")
    print(f"API: {args.base_url}   users available: {len(user_ids)}")

    args._queue_config = None
    if args.queue:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=10.0) as c:
            args._queue_config = await queue_config(c)
        cfg = args._queue_config or {}
        print(f"Waiting room: ON — admitting {cfg.get('admit_batch','?')} every "
              f"{cfg.get('admit_interval_s','?')}s "
              f"({cfg.get('admission_rate_per_s','?')}/s), "
              f"token TTL {cfg.get('admission_ttl_s','?')}s")
        print(f"  ~{args.n / max(1e-9, cfg.get('admission_rate_per_s') or 1):.0f}s "
              f"to drain {args.n} buyers per run")
    else:
        print("Waiting room: BYPASSED (--no-queue) — raw herd straight at /checkout/*")
    print()

    # --compare runs each strategy twice at the SAME seed: identical chaos
    # plan, identical keys, the only difference being whether the retry
    # carries the Idempotency-Key header. That is what makes the before/after
    # a controlled comparison rather than two unrelated runs.
    arms = [False, True] if args.compare else [args.idempotency]

    # Shared by both arms (so the comparison is controlled) but new every
    # invocation (so a re-run never replays the last run's cache).
    run_nonce = uuid.uuid4().hex

    runs = []
    for strategy in targets:
        sale_id = assigned[strategy]
        for use_idem in arms:
            tag = "with idempotency" if use_idem else "no idempotency"
            print(f"[{strategy}] ({tag}) sale {sale_id[:8]}… resetting to "
                  f"{args.stock} units, firing {args.n} buyers…", flush=True)
            run = await run_strategy(args.base_url, strategy, sale_id, args.n,
                                     args.chaos, args.stock, user_ids,
                                     args.admin_token, args.seed,
                                     use_idempotency=use_idem,
                                     run_nonce=run_nonce,
                                     use_queue=args.queue)
            runs.append(run)
            if run.error:
                print(f"  ERROR: {run.error}\n")
                continue
            flag = "HOLDS" if run.invariant_holds else "VIOLATED"
            qbits = ""
            if run.use_queue:
                qbits = (f"queue_wait_p50={fmt(run.queue_wait_percentile(50), 's')} "
                         f"p95={fmt(run.queue_wait_percentile(95), 's')}  ")
            print(f"  confirmed(DB)={run.confirmed_in_db}/{run.starting_stock}  "
                  f"sold_out={run.sold_out}  aborted={run.aborted}  "
                  f"dup_pairs={len(run.duplicate_pairs)}  "
                  f"bought_twice={run.duplicates_distinct_orders}  "
                  f"replayed={run.replayed}  refused_403={run.admission_refused}  "
                  f"max_concurrent_checkouts={run.max_checkout_concurrency}  "
                  f"{qbits}"
                  f"wall={run.wall_time_s:.2f}s  INVARIANT {flag}\n", flush=True)

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(build_report(runs, args, sale_names))

    print(f"Report written to {out_path}")
    violated = [r.strategy for r in runs if not r.error and not r.invariant_holds]
    if violated:
        print(f"\n*** INVARIANT VIOLATED: {', '.join(violated)} ***")
        return 2
    print("\nAll invariants held.")
    return 0


def main():
    p = argparse.ArgumentParser(
        description="Black-box chaos verification of the checkout strategies.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--n", type=int, default=200, help="concurrent buyers per strategy")
    p.add_argument("--chaos", type=float, default=0.3,
                   help="fraction of requests given chaos (0.0-1.0)")
    p.add_argument("--strategy", choices=STRATEGIES + ["all"], default="all",
                   help="which strategy to exercise")
    p.add_argument("--stock", type=int, default=5, help="starting stock per sale")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="running API base URL")
    p.add_argument("--admin-token", default=os.getenv("ADMIN_TOKEN", ""),
                   help="value for ?token= if the server sets ADMIN_TOKEN")
    p.add_argument("--seed", type=int, default=1337, help="RNG seed for reproducible chaos")
    p.add_argument("--out", default="results/chaos_verification_report.md",
                   help="report output path")
    p.add_argument("--no-idempotency", dest="idempotency", action="store_false",
                   help="send retries WITHOUT an Idempotency-Key (pre-idempotency behaviour)")
    p.add_argument("--compare", action="store_true",
                   help="run each strategy twice at the same seed -- once without "
                        "and once with Idempotency-Key -- for a controlled before/after")
    p.add_argument("--no-queue", dest="queue", action="store_false",
                   help="skip the waiting room and hit /checkout/* directly "
                        "(the pre-queue herd). Requires the server to be running "
                        "with ADMISSION_ENFORCED=false, otherwise every request "
                        "is refused at the door with a 403")
    p.set_defaults(idempotency=True, queue=True)
    args = p.parse_args()

    if not 0.0 <= args.chaos <= 1.0:
        p.error("--chaos must be between 0.0 and 1.0")
    if args.n < 1:
        p.error("--n must be >= 1")

    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
