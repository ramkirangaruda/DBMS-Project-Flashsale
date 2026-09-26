"""
Prometheus instrumentation -- purely additive.

Nothing here edits a checkout handler or the idempotency middleware. All
checkout metrics are derived by OBSERVING the HTTP exchange from outside:
the strategy comes from the request path, the outcome from the response
body's `status` field, and the idempotency signals from the
`Idempotent-Replay` header that app/idempotency.py already sets. That is
enough to reconstruct everything below without reaching inside anything.

Exposed at /metrics.

    checkout_requests_total{strategy,outcome}      counter
    checkout_latency_seconds{strategy}             histogram
    checkout_retries_total{strategy}               counter  (OCC conflicts)
    idempotency_cache_hits_total                   counter
    idempotency_cache_misses_total                 counter
    idempotency_reservation_conflicts_total        counter  (see caveat)
    flashsale_sale_available{sale_id,product,source}  gauge  (sql | redis)
    flashsale_sale_divergence{sale_id,product}     gauge  (sql - redis)
"""
import os
import threading
import time

from prometheus_client import Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# --------------------------------------------------------------------------
# Metric definitions
# --------------------------------------------------------------------------
CHECKOUT_REQUESTS = Counter(
    "checkout_requests_total",
    "Checkout attempts by strategy and outcome.",
    ["strategy", "outcome"],
)

CHECKOUT_LATENCY = Histogram(
    "checkout_latency_seconds",
    "End-to-end checkout latency as seen by the server, by strategy.",
    ["strategy"],
    # Tuned for this system: sub-millisecond Redis hits at one end, multi-
    # second lock queues under heavy contention at the other.
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5,
             1.0, 2.5, 5.0, 10.0, float("inf")),
)

CHECKOUT_RETRIES = Counter(
    "checkout_retries_total",
    "Optimistic-concurrency version conflicts -- a losing writer told to retry. "
    "Expected under contention; it is OCC working, not an error.",
    ["strategy"],
)

IDEMPOTENCY_HITS = Counter(
    "idempotency_cache_hits_total",
    "Requests answered from the idempotency cache without re-executing checkout.",
)

IDEMPOTENCY_MISSES = Counter(
    "idempotency_cache_misses_total",
    "Requests carrying an Idempotency-Key that had no cached result and executed.",
)

# CAVEAT, stated plainly because the number is easy to over-read:
# this counts only reservation conflicts the SERVER REPORTS -- the 409
# "still in flight" response. A request that loses the SET NX race but then
# successfully waits for the winner's result is, from outside, indistinguishable
# from an ordinary cache hit: both return the same body with
# Idempotent-Replay: true. Counting those would mean incrementing a counter
# inside app/idempotency.py's contention branch, which this task is explicitly
# not allowed to modify. So this is a LOWER BOUND on races, not a total.
IDEMPOTENCY_RESERVATION_CONFLICTS = Counter(
    "redis_reservation_races_total",
    "Idempotency reservation races observable from outside: requests rejected "
    "because another request holding the same key was still in flight. "
    "LOWER BOUND -- races that resolve successfully look identical to cache hits.",
)

SALE_AVAILABLE = Gauge(
    "flashsale_sale_available",
    "Units the given store believes are available for a sale.",
    ["sale_id", "product", "source"],
)

SALE_DIVERGENCE = Gauge(
    "flashsale_sale_divergence",
    "sql_available minus redis_available. Non-zero is the CAP trade-off "
    "visible in real time, not an error: each strategy decrements one store.",
    ["sale_id", "product"],
)

# --------------------------------------------------------------------------
# Waiting-room metrics.
#
# DEFINITIONS ONLY -- nothing in this file records them. app/queue.py sets the
# queue gauges from its admission worker and app/admission.py moves the
# in-flight gauge, so the observation code lives next to the thing being
# observed and the middleware above is untouched.
# --------------------------------------------------------------------------
QUEUE_JOINS = Counter(
    "queue_joins_total",
    "Buyers who took a place in the waiting room.",
    ["sale_id"],
)

QUEUE_ADMITS = Counter(
    "queue_admits_total",
    "Buyers released from the waiting room to checkout. Rises in steps of "
    "the admission batch size, which is the point.",
    ["sale_id"],
)

QUEUE_WAIT = Histogram(
    "queue_wait_seconds",
    "How long an admitted buyer spent in the waiting room.",
    # A 1000-deep queue at 20 admissions/s takes ~50s to drain, so the useful
    # range runs from sub-second to about a minute.
    buckets=(0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 45.0, 60.0, 120.0,
             float("inf")),
)

QUEUE_DEPTH = Gauge(
    "current_queue_depth",
    "Buyers still waiting for a sale right now.",
    ["sale_id"],
)

CHECKOUT_IN_FLIGHT = Gauge(
    "checkout_in_flight",
    "Checkout requests executing at this instant. This is the number the "
    "waiting room is designed to bound: without it 'the herd never reaches "
    "checkout' would be an assertion rather than a measurement.",
    ["strategy"],
)

ADMISSION_REJECTED = Counter(
    "admission_rejected_total",
    "Checkout attempts turned away at the door for lack of a valid admission "
    "token. Counted separately from checkout_requests_total on purpose -- a "
    "request that never reached a handler is not a checkout attempt.",
    ["reason"],
)

QUEUE_ADMISSION_LEADER = Gauge(
    "queue_admission_leader",
    "1 on the single replica currently holding the admission-worker lease, "
    "0 on every other replica. Under horizontal scaling, each API process "
    "runs its own admission-worker thread (see app/queue.py); without this "
    "lease exactly-one-active guarantee, N replicas silently admit at N "
    "times the configured rate -- discovered during the scaling phase and "
    "fixed by gating admit() behind this lease. Summed across replicas this "
    "should read exactly 1 at all times (0 only in the brief window during "
    "leader failover); anything else means the fix has regressed.",
)


# --------------------------------------------------------------------------
# Checkout observation middleware
# --------------------------------------------------------------------------
class CheckoutMetricsMiddleware(BaseHTTPMiddleware):
    """
    Buffers and inspects /checkout/* responses only.

    Everything else -- the SPA static mount, /dashboard's FileResponse, the
    forecast endpoint -- is passed straight through untouched, because
    buffering a streamed file response here would be both pointless and
    harmful.
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        if not path.startswith("/checkout/"):
            return await call_next(request)

        strategy = path.rsplit("/", 1)[-1] or "unknown"
        had_key = "idempotency-key" in request.headers

        started = time.perf_counter()
        response = await call_next(request)
        body = b"".join([chunk async for chunk in response.body_iterator])
        elapsed = time.perf_counter() - started

        # --- idempotency signals (from the header the other middleware sets)
        replay = response.headers.get("idempotent-replay", "").lower()
        if replay == "true":
            IDEMPOTENCY_HITS.inc()
        elif had_key:
            IDEMPOTENCY_MISSES.inc()

        # --- outcome, parsed from the uniform response body
        status = ""
        message = ""
        try:
            import json

            parsed = json.loads(body.decode("utf-8"))
            status = str(parsed.get("status", ""))
            message = str(parsed.get("message", ""))
        except Exception:                                      # noqa: BLE001
            pass

        low = message.lower()
        if status == "confirmed":
            outcome = "confirmed"
        elif status == "sold_out":
            outcome = "sold_out"
        else:
            outcome = "error"
            if "version conflict" in low:
                # OCC's losing writer. Counted separately from generic errors
                # so a retry spike is legible as its own signal.
                CHECKOUT_RETRIES.labels(strategy=strategy).inc()
            if "still in flight" in low:
                IDEMPOTENCY_RESERVATION_CONFLICTS.inc()

        CHECKOUT_REQUESTS.labels(strategy=strategy, outcome=outcome).inc()
        # Replayed responses are excluded from the latency histogram: they
        # never ran checkout, so folding them in would flatter the numbers.
        if replay != "true":
            CHECKOUT_LATENCY.labels(strategy=strategy).observe(elapsed)

        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=body, status_code=response.status_code,
                        headers=headers,
                        media_type=response.media_type or "application/json")


# --------------------------------------------------------------------------
# CAP divergence collector
# --------------------------------------------------------------------------
def _collect_sale_gauges(session_factory, redis_client):
    """
    One sampling pass: for each active sale, what does PostgreSQL think is
    available, and what does Redis think?

    They are allowed to disagree -- /checkout/redis never decrements
    inventory.reserved_stock, and the SQL strategies never touch the Redis
    counter. Graphing both is the point: the gap IS the CAP trade-off, drawn
    live rather than described.

    Read-only.
    """
    db = session_factory()
    try:
        rows = db.execute(text("""
            SELECT f.id, p.name, i.total_stock, i.reserved_stock
            FROM flash_sale_events f
            JOIN products p  ON p.id = f.product_id
            JOIN inventory i ON i.sale_id = f.id
            WHERE f.end_time > now()
        """)).fetchall()
    finally:
        db.close()

    for sale_id, product, total, reserved in rows:
        sid = str(sale_id)
        sql_avail = max(0, (total or 0) - (reserved or 0))
        SALE_AVAILABLE.labels(sale_id=sid, product=product, source="sql").set(sql_avail)

        redis_avail = None
        try:
            raw = redis_client.get(f"stock:{sid}")
            if raw is not None:
                redis_avail = max(0, int(raw))
        except Exception:                                      # noqa: BLE001
            redis_avail = None

        if redis_avail is None:
            # No Redis counter for this sale yet (nobody has used the fast
            # path). Mirror the SQL value so the divergence panel reads 0
            # rather than drawing a phantom gap.
            SALE_AVAILABLE.labels(sale_id=sid, product=product, source="redis").set(sql_avail)
            SALE_DIVERGENCE.labels(sale_id=sid, product=product).set(0)
        else:
            SALE_AVAILABLE.labels(sale_id=sid, product=product, source="redis").set(redis_avail)
            SALE_DIVERGENCE.labels(sale_id=sid, product=product).set(sql_avail - redis_avail)


def start_sale_collector(session_factory, redis_client, interval_s=None):
    interval_s = interval_s or float(os.getenv("METRICS_SALE_INTERVAL", "3"))

    def loop():
        while True:
            try:
                _collect_sale_gauges(session_factory, redis_client)
            except Exception:                                  # noqa: BLE001
                pass                                           # never kill the thread
            time.sleep(interval_s)

    t = threading.Thread(target=loop, daemon=True, name="sale-gauge-collector")
    t.start()
    return t


# --------------------------------------------------------------------------
def install(app, redis_client, session_factory):
    """
    One call from app/main.py. Adds /metrics, the default HTTP metrics, the
    checkout observation middleware, and the CAP gauge collector.
    """
    app.add_middleware(CheckoutMetricsMiddleware)

    Instrumentator(
        should_group_status_codes=False,
        excluded_handlers=["/metrics"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    start_sale_collector(session_factory, redis_client)
    return app
