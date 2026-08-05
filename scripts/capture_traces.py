"""
Reproduce, find and screenshot the three traces that tell the concurrency
story side by side in Jaeger.

    python -m scripts.capture_traces                  # all three
    python -m scripts.capture_traces --only lock      # one of lock|retry|cache
    python -m scripts.capture_traces --no-screenshot  # numbers only

Writes PNGs plus a machine-readable breakdown to results/traces/.

THE THREE SCENARIOS
    lock    a pessimistic request that queues on the row lock, so its
            `SELECT ... FOR UPDATE` span is most of the trace.
    retry   an optimistic request that loses on version, retries, and wins --
            both attempts in ONE trace.
    cache   a redis request answered from the idempotency cache, which never
            reaches a checkout handler at all.

BLACK BOX, LIKE THE CHAOS HARNESS
    Every request goes over the real HTTP API. Nothing here imports checkout
    code, and the only database contact is a read-only SELECT for user ids
    (orders.user_id is a foreign key, so the ids have to be real).

WHY THE HARNESS OWNS THE PARENT SPAN
    /checkout/optimistic does not retry. It detects the version conflict,
    returns 409, and the CALLER decides whether to try again -- so the retry
    is a second, independent HTTP request, and by default a second,
    independent trace. Nothing on the server could stitch them together
    without a retry loop being added to the handler, which this phase is not
    allowed to do (and which would change the system's behaviour just to make
    it easier to observe).

    So the harness opens a span for the logical purchase intent and
    propagates W3C `traceparent` on both attempts. Both server spans then
    land under one root and Jaeger draws them as siblings. This is ordinary
    distributed tracing -- the client is genuinely the parent here, because
    the client is genuinely what does the retrying. It is worth being clear
    about when reading the picture: the root span is the harness, not the
    server.

    The same mechanism is what makes the trace ID knowable in advance, so
    there is no searching or guessing about which trace to screenshot.
"""
import argparse
import asyncio
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from sqlalchemy import text

from app.database import SessionLocal

BASE_URL = os.getenv("CHAOS_BASE_URL", "http://127.0.0.1:8010")
JAEGER = os.getenv("JAEGER_URL", "http://127.0.0.1:16686")
PROMETHEUS = os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
OTLP = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "results", "traces")

SERVICE_NAME = "flashsale-capture-harness"

_PROVIDER = None          # set by init_tracer(); flushed before every lookup


# --------------------------------------------------------------------------
# Tracing setup for the harness itself
# --------------------------------------------------------------------------
def init_tracer():
    """
    A tracer provider for this script, exporting to the same Jaeger as the
    app.

    BatchSpanProcessor, NOT Simple, and the reason is a measurement bug worth
    naming. SimpleSpanProcessor exports each span synchronously the instant
    it ends -- so a child span's ~45ms HTTP export to Jaeger happens while
    its PARENT is still open, and gets counted as parent duration. The first
    cache-hit capture read 70ms at the root around a request the server
    handled in 12ms; nearly all of the difference was the tracer measuring
    itself. Batching moves the export off that path, and force_flush() at the
    end of each scenario keeps the trace immediately queryable.
    """
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    global _PROVIDER

    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{OTLP.rstrip('/')}/v1/traces"),
            schedule_delay_millis=500,
        )
    )
    trace.set_tracer_provider(provider)
    _PROVIDER = provider

    # Gives every outbound request a client span AND injects `traceparent`,
    # which is what puts the server spans under our root.
    HTTPXClientInstrumentor().instrument()

    return trace.get_tracer("capture_traces"), provider


def trace_id_hex(span):
    return format(span.get_span_context().trace_id, "032x")


# --------------------------------------------------------------------------
# Setup helpers (HTTP only, except one read-only SELECT)
# --------------------------------------------------------------------------
def fetch_user_ids(limit=80):
    """Real user ids for the orders.user_id foreign key. SELECT only."""
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT id FROM users ORDER BY created_at NULLS LAST LIMIT :lim"),
            {"lim": limit},
        ).fetchall()
        return [str(r[0]) for r in rows]
    finally:
        db.close()


async def pick_sale(client, index=0):
    r = await client.get("/sales")
    r.raise_for_status()
    sales = r.json().get("sales", [])
    if not sales:
        raise SystemExit("No active sales. Run:  python -m scripts.seed_storefront")
    return sales[index % len(sales)]


async def reset(client, sale_id, stock):
    r = await client.post(f"/admin/reset-sale/{sale_id}", params={"stock": stock})
    r.raise_for_status()
    return r.json()


async def checkout(client, strategy, sale_id, user_id, idem_key=None):
    headers = {"Idempotency-Key": idem_key} if idem_key else None
    resp = await client.post(f"/checkout/{strategy}",
                             params={"sale_id": sale_id, "user_id": user_id},
                             headers=headers)
    try:
        body = resp.json()
    except Exception:                                          # noqa: BLE001
        body = {}
    return resp, body


# --------------------------------------------------------------------------
# Scenario A -- pessimistic, dominated by the row-lock wait
# --------------------------------------------------------------------------
# (herd size, how long the subject waits before joining the stampede).
#
# BIASED SMALL, AND THAT IS THE COUNTER-INTUITIVE PART. The obvious way to
# deepen a lock queue is to throw more buyers at it, and it does not work:
# app/database.py's pool tops out at 30 connections (pool_size=10 +
# max_overflow=20) and FastAPI runs these sync handlers on a bounded thread
# pool. Past that ceiling the extra load never reaches PostgreSQL at all --
# it queues for a connection or a worker thread, both of which happen OUTSIDE
# any span and land in the unattributed residual instead of in the lock wait.
#
# Measured, not guessed: at herd=96 the SELECT ... FOR UPDATE was 32ms of a
# 359ms request (9%) with 316ms residual, while at herd=16 it was 230ms of
# 329ms (70%). Keeping the herd near the pool ceiling puts the contention
# where it can be seen.
# The effective ceiling is lower than the pool's 30, because every checkout
# briefly needs TWO connections: get_db() still holds one when
# log_checkout_attempt() opens its own (deliberately separate, so a rolled-back
# checkout does not erase its own behaviour log). So ~15 concurrent buyers is
# where PostgreSQL saturates, and the ladder sits around there.
#
# The delay is how long the subject waits before joining, and it matters as
# much as the size: too early and the herd has not finished queueing yet, too
# late and it has already drained.
LOCK_LADDER = [(16, 0.05), (16, 0.02), (14, 0.04), (18, 0.06),
               (16, 0.03), (20, 0.05), (14, 0.02), (18, 0.03)]

LOCK_DOMINANCE = 0.50          # lock span must be >= this share of the request


async def _one_lock_round(tracer, users, herd, delay):
    limits = httpx.Limits(max_connections=herd + 20, max_keepalive_connections=herd + 20)
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0, limits=limits) as client:
        sale = await pick_sale(client, 0)
        sale_id = sale["sale_id"]
        # Stock well above the herd on purpose. If it ran out, the losers
        # would find `total - reserved <= 0`, roll back and release the lock
        # in microseconds -- there would be no queue left to wait behind and
        # the trace would show a fast sold-out instead of a lock wait.
        await reset(client, sale_id, stock=herd * 3)

        gate = asyncio.Event()

        async def background(i):
            await gate.wait()
            try:
                await checkout(client, "pessimistic", sale_id, users[i % len(users)])
            except Exception:                                  # noqa: BLE001
                pass

        async def subject():
            await gate.wait()
            # A beat behind the herd, so it queues behind an established
            # backlog instead of racing to be first.
            await asyncio.sleep(delay)
            with tracer.start_as_current_span("purchase_intent pessimistic") as span:
                span.set_attribute("scenario", "lock_wait")
                span.set_attribute("herd_size", herd)
                resp, body = await checkout(client, "pessimistic", sale_id, users[-1])
                span.set_attribute("outcome", body.get("status", "?"))
                return trace_id_hex(span), body

        tasks = [asyncio.create_task(background(i)) for i in range(herd)]
        subj = asyncio.create_task(subject())
        gate.set()
        tid, body = await subj
        await asyncio.gather(*tasks, return_exceptions=True)

    return tid, body


async def scenario_lock(tracer, users, herd=None, rounds=16):
    """
    Real contention, not a simulated stall: a herd of buyers hits
    /checkout/pessimistic at once and the subject joins the same stampede.
    All of them take `SELECT ... FOR UPDATE` on the same inventory row, so
    they serialise, and a request arriving mid-queue blocks inside that
    statement until everyone ahead of it commits.

    Which position the subject lands in is genuinely random, so the lock
    share swings hard between runs at the same settings (3% and 70% both
    occurred at herd=24). Rather than reporting whatever the first run gave,
    this walks LOCK_LADDER until the lock really is at least
    LOCK_DOMINANCE of the request, keeping the best result seen. If nothing
    clears the bar, the best attempt is returned WITH its actual share so the
    write-up can say so plainly instead of overselling the picture.
    """
    ladder = [(herd, 0.03)] + LOCK_LADDER if herd else LOCK_LADDER
    best = None

    for i in range(rounds):
        h, delay = ladder[i % len(ladder)]
        tid, body = await _one_lock_round(tracer, users, h, delay)
        info = await wait_for_trace(tid)
        if not info:
            continue
        share = lock_share(info)
        if best is None or share > best[3]:
            best = (tid, body, info, share)
        if share >= LOCK_DOMINANCE:
            print(f"  captured on round {i + 1} (herd={h}, delay={delay}s): "
                  f"lock is {share:.0%} of the request")
            return tid, body, info
        print(f"  round {i + 1} (herd={h}): lock only {share:.0%} of the "
              f"request, retrying…")

    if best is None:
        # Every round drove real traffic but no trace ever came back --
        # Jaeger is down or not receiving. Say that, rather than dying on an
        # unpack three lines later.
        raise SystemExit(f"No trace reached Jaeger in {rounds} rounds. "
                         f"Is it up?  docker compose up -d jaeger   ({JAEGER})")

    tid, body, info, share = best
    print(f"  best of {rounds} rounds: lock is {share:.0%} of the request "
          f"(target was {LOCK_DOMINANCE:.0%})")
    return tid, body, info


def lock_span(info):
    """The `SELECT ... FOR UPDATE` child span, which is where the row-lock
    wait actually accumulates -- the statement blocks in PostgreSQL until the
    lock is granted."""
    for c in info["children"]:
        if "FOR UPDATE" in (c.get("statement") or ""):
            return c
    return None


def lock_share(info):
    """Lock wait as a fraction of the SERVER span -- not of the trace root,
    which also contains the harness's own client-side time."""
    sel = lock_span(info)
    if not sel or not info["server_total_ms"]:
        return 0.0
    return sel["duration_ms"] / info["server_total_ms"]


# --------------------------------------------------------------------------
# Scenario B -- optimistic version conflict, then a successful retry
# --------------------------------------------------------------------------
async def scenario_retry(tracer, users, herd=40, attempts=6):
    """
    The subject fires into a stampede of concurrent optimistic buyers. Its
    UPDATE ... WHERE version = :v matches zero rows because someone else
    committed against the same version first, so it gets a 409 "version
    conflict" -- and then, exactly as a real client would, it sends the whole
    request again under the same parent span.

    Both attempts are children of one `purchase_intent` span, so Jaeger draws
    them as two distinct sibling subtrees instead of one opaque box. The
    scenario is only accepted when the first attempt really did conflict and
    the second really did confirm.
    """
    for attempt in range(1, attempts + 1):
        limits = httpx.Limits(max_connections=herd + 20, max_keepalive_connections=herd + 20)
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0, limits=limits) as client:
            sale = await pick_sale(client, 1)
            sale_id = sale["sale_id"]
            await reset(client, sale_id, stock=herd * 3)

            gate = asyncio.Event()
            captured = {}

            async def background(i):
                await gate.wait()
                for _ in range(3):
                    try:
                        await checkout(client, "optimistic", sale_id, users[i % len(users)])
                    except Exception:                          # noqa: BLE001
                        pass

            async def subject():
                await gate.wait()
                with tracer.start_as_current_span("purchase_intent optimistic") as span:
                    span.set_attribute("scenario", "occ_retry")
                    span.set_attribute("herd_size", herd)
                    tries = []
                    for n in range(1, 6):
                        resp, body = await checkout(client, "optimistic",
                                                    sale_id, users[-1])
                        msg = (body.get("message") or "").lower()
                        conflicted = "version conflict" in msg
                        tries.append({"attempt": n, "status": body.get("status"),
                                      "http": resp.status_code,
                                      "version_conflict": conflicted})
                        if body.get("status") == "confirmed":
                            break
                        if not conflicted:
                            break        # sold out or an error -- not our case
                    span.set_attribute("attempts", len(tries))
                    span.set_attribute("outcome", tries[-1]["status"] or "?")
                    captured["tries"] = tries
                    return trace_id_hex(span), tries

            tasks = [asyncio.create_task(background(i)) for i in range(herd)]
            subj = asyncio.create_task(subject())
            gate.set()
            tid, tries = await subj
            await asyncio.gather(*tasks, return_exceptions=True)

        good = (len(tries) >= 2
                and tries[0]["version_conflict"]
                and tries[-1]["status"] == "confirmed")
        if good:
            info = await wait_for_trace(tid)
            print(f"  captured on attempt {attempt}: "
                  f"{' -> '.join(t['status'] for t in tries)}")
            return tid, tries, info
        print(f"  attempt {attempt}: got {[t['status'] for t in tries]}, retrying…")
        # Grow in small steps, not multiplicatively. A version conflict needs
        # concurrent writers, not a huge number of them -- two are enough in
        # principle -- and overshooting the connection-pool ceiling buries the
        # attempt spans under hundreds of milliseconds of queueing that has
        # nothing to do with OCC. See LOCK_LADDER.
        herd += 8

    return tid, tries, await wait_for_trace(tid)


# --------------------------------------------------------------------------
# Scenario C -- redis strategy served entirely from the idempotency cache
# --------------------------------------------------------------------------
async def scenario_cache(tracer, users):
    """
    Two requests carrying the same Idempotency-Key.

    The first is a normal /checkout/redis: it executes, decrements the Redis
    counter, writes an order, and its result is cached. The second is the
    retry a real client sends after a timeout -- same key, so the idempotency
    middleware returns the stored response and the checkout handler is never
    called.

    Only the SECOND trace is captured. That trace is the demonstration: a
    server span containing a single Redis GET and nothing else. No Postgres,
    no DECR, no order insert. The short-circuit is visible as an absence.

    Deterministic -- no herd and no retry loop, because there is no race to
    win here.
    """
    key = str(uuid.uuid4())
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0) as client:
        sale = await pick_sale(client, 2)
        sale_id = sale["sale_id"]
        await reset(client, sale_id, stock=50)

        # Priming request -- traced too (it is the "before" picture), but not
        # the one screenshotted.
        with tracer.start_as_current_span("purchase_intent redis (first send)") as span:
            span.set_attribute("scenario", "cache_prime")
            resp1, body1 = await checkout(client, "redis", sale_id, users[0], idem_key=key)
            span.set_attribute("outcome", body1.get("status", "?"))
            prime_tid = trace_id_hex(span)
        replay1 = resp1.headers.get("idempotent-replay")

        await asyncio.sleep(0.2)

        with tracer.start_as_current_span("purchase_intent redis (retry, same key)") as span:
            span.set_attribute("scenario", "cache_hit")
            resp2, body2 = await checkout(client, "redis", sale_id, users[0], idem_key=key)
            span.set_attribute("outcome", body2.get("status", "?"))
            span.set_attribute("idempotent_replay",
                               resp2.headers.get("idempotent-replay", "?"))
            tid = trace_id_hex(span)

    replay2 = resp2.headers.get("idempotent-replay")
    same_order = body1.get("order_id") == body2.get("order_id")
    print(f"  first send: {body1.get('status')} replay={replay1} "
          f"order={str(body1.get('order_id'))[:8]}…")
    print(f"  retry     : {body2.get('status')} replay={replay2} "
          f"order={str(body2.get('order_id'))[:8]}…  same_order={same_order}")

    if replay2 != "true":
        print("  WARNING: the retry was NOT served from cache.")

    info = await wait_for_trace(tid)
    prime_info = await wait_for_trace(prime_tid, quiet=True)
    return tid, {"replayed": replay2 == "true", "same_order_id": same_order,
                 "prime_trace_id": prime_tid,
                 "prime_total_ms": (prime_info or {}).get("total_ms")}, info


# --------------------------------------------------------------------------
# Jaeger lookup + breakdown
# --------------------------------------------------------------------------
async def wait_for_trace(trace_id, timeout_s=25.0, quiet=False, settle_s=1.2):
    """
    Poll Jaeger until the trace is queryable, then summarise it.

    Two things have to land before a trace is complete, and they race:

      * OUR spans (the purchase_intent root and the httpx client spans), which
        sit in this process's batch queue. force_flush() below pushes them out
        first -- without it the lookup can win the race and summarise a trace
        that is missing its own root. That is not hypothetical; it produced a
        two-span "cache hit" trace whose root was the server span.
      * The APP's spans, buffered on its side and indexed asynchronously by
        Jaeger. Those need polling.

    Even after the server span appears, siblings can still be in flight, so a
    short settle-and-refetch follows rather than summarising the first
    complete-looking answer.
    """
    if _PROVIDER is not None:
        _PROVIDER.force_flush(5000)

    deadline = time.time() + timeout_s
    async with httpx.AsyncClient(timeout=10.0) as client:

        async def fetch():
            r = await client.get(f"{JAEGER}/api/traces/{trace_id}")
            if r.status_code != 200:
                return None
            data = r.json().get("data") or []
            return data[0] if data and data[0].get("spans") else None

        while time.time() < deadline:
            try:
                raw = await fetch()
                if raw and summarise(raw)["server_spans"]:
                    await asyncio.sleep(settle_s)
                    return summarise(await fetch() or raw)
            except Exception:                                  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
    if not quiet:
        print(f"  WARNING: trace {trace_id} did not appear in Jaeger within "
              f"{timeout_s:.0f}s")
    return None


def _tags(span):
    return {t["key"]: t.get("value") for t in span.get("tags", [])}


def summarise(trace):
    """
    Flatten a Jaeger trace into the numbers the write-up needs: total
    duration, each child span, and the residual the children do not explain.

    The residual is reported rather than hidden. It is real time -- FastAPI
    routing and validation, the two response-buffering middlewares, JSON
    serialisation, and `COMMIT`, which SQLAlchemy's instrumentation does not
    span because it is a connection-level call and not a cursor execute.
    """
    spans = trace["spans"]
    by_id = {s["spanID"]: s for s in spans}
    procs = trace.get("processes", {})

    def service(s):
        return procs.get(s.get("processID"), {}).get("serviceName", "?")

    roots = [s for s in spans if not s.get("references")]
    root = min(roots, key=lambda s: s["startTime"]) if roots else \
        min(spans, key=lambda s: s["startTime"])

    server_spans = [s for s in spans
                    if _tags(s).get("span.kind") == "server"]

    def children_of(span_id):
        out = []
        for s in spans:
            for ref in s.get("references", []):
                if ref.get("refType") == "CHILD_OF" and ref.get("spanID") == span_id:
                    out.append(s)
        return sorted(out, key=lambda s: s["startTime"])

    def describe(s):
        tg = _tags(s)
        return {
            "name": s["operationName"],
            "service": service(s),
            "duration_ms": round(s["duration"] / 1000, 2),
            "start_offset_ms": round((s["startTime"] - root["startTime"]) / 1000, 2),
            "statement": (tg.get("db.statement") or "").strip().replace("\n", " ")[:220],
            "system": tg.get("db.system", ""),
            "http_status": tg.get("http.status_code") or tg.get("http.response.status_code"),
            "span_id": s["spanID"],
        }

    # For the breakdown, the interesting layer is the children of each server
    # span (the SQL and Redis calls). Client spans from the harness are
    # scaffolding.
    children = []
    for srv in sorted(server_spans, key=lambda s: s["startTime"]):
        for c in children_of(srv["spanID"]):
            d = describe(c)
            d["under"] = srv["operationName"]
            children.append(d)

    total_ms = round(root["duration"] / 1000, 2)
    accounted = sum(c["duration_ms"] for c in children)
    server_total = sum(round(s["duration"] / 1000, 2) for s in server_spans)

    return {
        "trace_id": trace["traceID"],
        "root": root["operationName"],
        "root_service": service(root),
        "total_ms": total_ms,
        "span_count": len(spans),
        "server_spans": [describe(s) for s in
                         sorted(server_spans, key=lambda s: s["startTime"])],
        "server_total_ms": round(server_total, 2),
        "children": children,
        "accounted_ms": round(accounted, 2),
        "residual_ms": round(server_total - accounted, 2),
    }


def print_breakdown(title, info):
    if not info:
        print(f"  {title}: no trace")
        return
    print(f"\n  {title}")
    print(f"  trace {info['trace_id']}  root={info['root']} "
          f"({info['root_service']})  total={info['total_ms']}ms  "
          f"spans={info['span_count']}")
    for s in info["server_spans"]:
        print(f"    [server] {s['duration_ms']:8.2f}ms  {s['name']}"
              f"  http={s['http_status']}")
    for c in info["children"]:
        stmt = c["statement"][:70]
        print(f"        {c['duration_ms']:8.2f}ms  {c['name']:<20} {stmt}")
    print(f"    residual (framework + COMMIT, not spanned): "
          f"{info['residual_ms']:.2f}ms")


# --------------------------------------------------------------------------
# Prometheus cross-check -- is the captured example typical?
# --------------------------------------------------------------------------
async def prometheus_quantiles(strategy, window="10m"):
    """
    p50/p95/p99 for this strategy from the metrics laid down in the previous
    phase, over the window this capture ran in.

    The point is to be able to say whether the trace we screenshotted is a
    representative request or a freak, instead of presenting one arbitrary
    example as if it were typical.
    """
    out = {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        for q, label in ((0.50, "p50"), (0.95, "p95"), (0.99, "p99")):
            expr = (f'histogram_quantile({q}, sum by (le) '
                    f'(rate(checkout_latency_seconds_bucket'
                    f'{{strategy="{strategy}"}}[{window}])))')
            try:
                r = await client.get(f"{PROMETHEUS}/api/v1/query", params={"query": expr})
                res = r.json().get("data", {}).get("result", [])
                val = float(res[0]["value"][1]) if res else float("nan")
                out[label] = None if val != val else round(val * 1000, 1)
            except Exception:                                  # noqa: BLE001
                out[label] = None
    return out


# --------------------------------------------------------------------------
# Screenshot
# --------------------------------------------------------------------------
# Waited on before measuring. NOT the row class: `VirtualizedTraceView--row`
# also prefix-matches the zero-height `--rowsWrapper` container, which never
# becomes "visible" and just times the wait out.
TIMELINE_SELECTOR = "[class*='TraceTimelineViewer']"

CONTENT_HEIGHT_JS = """() => {
  let max = 0;
  document.querySelectorAll('div').forEach(e => {
    const c = e.className;
    if (typeof c === 'string' && c.includes('VirtualizedTraceView--row')) {
      const b = e.getBoundingClientRect().bottom + window.scrollY;
      if (b > max) max = b;
    }
  });
  return max;
}"""


def screenshot_trace(trace_id, out_path, viewport_h=1600, settle_s=4.0):
    """
    Headless Chromium against the Jaeger UI, same approach as
    scripts/capture_dashboard.py (which screenshots Grafana).

    `uiEmbed=v0` strips Jaeger's top nav so the capture is the trace itself
    rather than a browser tour.

    The viewport is deliberately taller than any of these traces need -- the
    waterfall is a VIRTUALISED list, so rows scrolled out of view are not in
    the DOM and would simply be missing from the capture. It is then cropped
    to the measured bottom of the last row, because a 4-span trace in a
    1600px window is mostly empty white.
    """
    from playwright.sync_api import sync_playwright

    url = f"{JAEGER}/trace/{trace_id}?uiEmbed=v0"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--force-device-scale-factor=1"])
        page = browser.new_page(viewport={"width": 1600, "height": viewport_h},
                                device_scale_factor=1.5)
        page.goto(url, wait_until="networkidle", timeout=60_000)
        page.wait_for_selector(TIMELINE_SELECTOR, timeout=30_000)
        time.sleep(settle_s)

        clip = None
        try:
            bottom = page.evaluate(CONTENT_HEIGHT_JS)
            if bottom and bottom > 100:
                clip = {"x": 0, "y": 0, "width": 1600,
                        "height": min(viewport_h, int(bottom) + 16)}
        except Exception:                                      # noqa: BLE001
            pass

        page.screenshot(path=out_path, clip=clip, full_page=clip is None)
        browser.close()
    return out_path


# --------------------------------------------------------------------------
async def main_async(args):
    tracer, provider = init_tracer()
    users = fetch_user_ids()
    if not users:
        raise SystemExit("No users in the database. Run:  python -m scripts.seed")

    os.makedirs(OUT_DIR, exist_ok=True)
    want = {"lock", "retry", "cache"} if args.only == "all" else {args.only}
    results = {}

    if "lock" in want:
        print("\n[1/3] pessimistic — waiting on the row lock")
        tid, body, info = await scenario_lock(tracer, users, herd=args.herd)
        provider.force_flush()
        print_breakdown("lock wait", info)
        results["lock"] = {"trace_id": tid, "outcome": body, "breakdown": info,
                           "lock_share": round(lock_share(info), 4) if info else None,
                           "prometheus": await prometheus_quantiles("pessimistic")}

    if "retry" in want:
        print("\n[2/3] optimistic — version conflict, then retry")
        tid, tries, info = await scenario_retry(tracer, users, herd=args.retry_herd)
        provider.force_flush()
        print_breakdown("occ retry", info)
        results["retry"] = {"trace_id": tid, "attempts": tries, "breakdown": info,
                            "prometheus": await prometheus_quantiles("optimistic")}

    if "cache" in want:
        print("\n[3/3] redis — served from the idempotency cache")
        tid, meta, info = await scenario_cache(tracer, users)
        provider.force_flush()
        print_breakdown("cache hit", info)
        results["cache"] = {"trace_id": tid, "meta": meta, "breakdown": info,
                            "prometheus": await prometheus_quantiles("redis")}

    # Merge rather than overwrite, so `--only cache` refreshes one scenario
    # instead of silently deleting the other two from the summary the
    # write-up cites.
    summary_path = os.path.join(OUT_DIR, "trace_summary.json")
    merged = {}
    if os.path.exists(summary_path):
        try:
            with open(summary_path, encoding="utf-8") as f:
                merged = json.load(f)
        except Exception:                                      # noqa: BLE001
            merged = {}
    merged.update(results)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
    print(f"\nBreakdowns written to {summary_path}")
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", choices=["all", "lock", "retry", "cache"], default="all")
    p.add_argument("--herd", type=int, default=None,
                   help="try this herd size first for the lock scenario; "
                        "otherwise walk the built-in ladder (see LOCK_LADDER -- "
                        "bigger is NOT better)")
    p.add_argument("--retry-herd", type=int, default=20,
                   help="concurrent optimistic buyers used to force a version conflict")
    p.add_argument("--no-screenshot", action="store_true",
                   help="reproduce and measure, but skip Playwright")
    args = p.parse_args()

    try:
        results = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130

    # Screenshots happen AFTER the event loop has closed, not inside it:
    # Playwright's sync API refuses to run on a live asyncio loop. Driving the
    # scenarios is naturally async and driving a browser is naturally
    # sequential, so they are simply kept apart.
    if not args.no_screenshot:
        print("\nScreenshotting…")
        for name, res in results.items():
            out = os.path.join(OUT_DIR, f"trace_{name}.png")
            try:
                screenshot_trace(res["trace_id"], out)
                print(f"  {out}  ({os.path.getsize(out):,} bytes)")
            except Exception as exc:                           # noqa: BLE001
                print(f"  {name}: screenshot failed ({type(exc).__name__}: {exc})")

    print("\nJaeger UI: " + JAEGER)
    for name, res in results.items():
        print(f"  {name:<6} {JAEGER}/trace/{res['trace_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
