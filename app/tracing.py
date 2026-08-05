"""
OpenTelemetry distributed tracing -- purely additive, and almost entirely
automatic.

WHY THERE ARE NO HAND-WRITTEN SPANS IN THE CHECKOUT CODE
    Everything below is auto-instrumentation. The FastAPI, SQLAlchemy and
    Redis integrations patch their libraries at the boundary, so every HTTP
    request, every SQL statement and every Redis command becomes a span
    without a single line changing inside /checkout/*. That is deliberate:
    the point of this phase was to see where checkout spends its time, and
    a measurement that requires rewriting the thing being measured is worth
    less than one that doesn't.

    It also means the spans are honest about what they are. The lock wait in
    the pessimistic strategy is not "a span someone named lock_wait" -- it is
    the actual `SELECT ... FOR UPDATE` statement span, which is wide because
    the statement genuinely blocked in PostgreSQL.

WHAT YOU GET, PER CHECKOUT REQUEST
    POST /checkout/{strategy}          server span (FastAPI/ASGI)
      +- BEGIN / SELECT / UPDATE / INSERT   one span per statement (SQLAlchemy)
      +- GET / SET / DECR ...               one span per command (Redis)

    ...and, because the idempotency middleware sits INSIDE this span, a
    request served from the idempotency cache shows up as a server span whose
    only child is a single Redis GET -- visibly, structurally different from
    one that ran checkout.

TRACE CONTEXT
    W3C `traceparent` is extracted from incoming requests by default, so a
    client that propagates context (see scripts/capture_traces.py) can put
    several HTTP requests under one parent span. That is the only way to see
    an optimistic version conflict and its retry in a single trace: the retry
    is client-driven -- /checkout/optimistic returns 409 and the CALLER
    decides to try again -- so there is no server-side loop to instrument.
    See the note in capture_traces.py.

CONFIG
    OTEL_EXPORTER_OTLP_ENDPOINT   default http://localhost:4318  (Jaeger OTLP)
    OTEL_SERVICE_NAME             default flashsale-engine
    OTEL_SDK_DISABLED=true        turn tracing off entirely
    OTEL_INSTRUMENT_PSYCOPG2=1    also trace at the DB-API layer (see below)
"""
import os

_ENABLED = os.getenv("OTEL_SDK_DISABLED", "").lower() not in ("true", "1", "yes")

# Prometheus scrapes /metrics every 2 seconds, forever. Tracing it would bury
# the interesting traces under thousands of identical ones.
EXCLUDED_URLS = os.getenv("OTEL_PYTHON_EXCLUDED_URLS", "metrics,health,favicon.ico")

# Recorded onto the server span as attributes. Both are config-only, which is
# what makes them usable here: they let a trace say "this request carried
# Idempotency-Key X and was answered from cache" without the idempotency
# middleware being modified to emit anything.
CAPTURE_REQUEST_HEADERS = ["Idempotency-Key"]
CAPTURE_RESPONSE_HEADERS = ["Idempotent-Replay", "Idempotency-Key"]

_provider = None
_libs_instrumented = False


def install(app, engine=None, service_name=None):
    """
    Called once from app/main.py. Sets up the tracer provider and turns on
    auto-instrumentation for FastAPI, SQLAlchemy and Redis.

    Args:
        app: the FastAPI application.
        engine: the SQLAlchemy Engine to trace (passed in rather than
            imported, to keep this module free of an app.database import).
        service_name: overrides OTEL_SERVICE_NAME.

    Safe to call when nothing is listening on the OTLP port: the exporter
    retries in a background thread and gives up quietly. A missing Jaeger
    slows nothing down and breaks nothing -- tracing is diagnostic, and it
    must never be able to take checkout with it.

    SAFE TO CALL TWICE, and it is called twice in practice. `python -m
    app.main` executes this module once as __main__ and then hands uvicorn
    the string "app.main:app", which imports it a second time under its real
    name -- so there are two FastAPI app objects, and only the second one
    ever serves a request. Creating a second TracerProvider would leave the
    live app exporting through the first one while force_flush() below
    flushed the second, which is a genuinely confusing failure: spans arrive,
    but never when you ask for them. So an existing SDK provider is adopted
    rather than replaced, and the library-level instrumentors (global, unlike
    the per-app FastAPI one) run only on the first pass.
    """
    global _provider, _libs_instrumented

    if not _ENABLED:
        return None

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        # Second pass: adopt whatever is already global and instrument only
        # this (new) app object.
        _provider = current
    else:
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        resource = Resource.create({
            "service.name": service_name or os.getenv("OTEL_SERVICE_NAME", "flashsale-engine"),
            "service.version": "1.0",
            "deployment.environment": os.getenv("OTEL_ENVIRONMENT", "local-demo"),
        })

        _provider = TracerProvider(resource=resource)
        _provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"),
                # Flushed roughly twice a second rather than the 5s default:
                # scripts/capture_traces.py drives a request and then
                # immediately goes looking for its trace, and waiting five
                # seconds per scenario for a batch to drain is dead time.
                schedule_delay_millis=int(os.getenv("OTEL_BSP_SCHEDULE_DELAY", "500")),
                max_export_batch_size=512,
            )
        )
        trace.set_tracer_provider(_provider)

    _instrument_fastapi(app)
    if not _libs_instrumented:
        _instrument_sqlalchemy(engine)
        _instrument_redis()
        _libs_instrumented = True

    return _provider


def _instrument_fastapi(app):
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls=EXCLUDED_URLS,
        http_capture_headers_server_request=CAPTURE_REQUEST_HEADERS,
        http_capture_headers_server_response=CAPTURE_RESPONSE_HEADERS,
        # Drop the per-message `http receive` / `http send` ASGI spans. They
        # are plumbing, not behaviour, and on a trace whose whole story is
        # "one child span is 95% of the duration" they are noise that makes
        # the story harder to read. Their time is not lost -- it shows up as
        # the part of the server span no child accounts for.
        exclude_spans=["receive", "send"],
    )


def _instrument_sqlalchemy(engine):
    """
    Traces at the SQLAlchemy layer: one span per statement, carrying the SQL
    in `db.statement`.

    This is the layer that matters for the pessimistic strategy, because
    `SELECT ... FOR UPDATE` blocks inside the driver call -- so the time
    spent queueing for the row lock lands squarely inside that statement's
    span instead of being smeared across the request.

    OTEL_INSTRUMENT_PSYCOPG2 additionally instruments the DB-API layer
    underneath. It is OFF by default on purpose: psycopg2 and SQLAlchemy
    spans nest around the same statement, so enabling both draws every query
    twice, once inside the other, at near-identical durations. That is
    misleading rather than more detailed. Turn it on only when the question
    is specifically about driver-level overhead.
    """
    if engine is None:
        return

    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(engine=engine, enable_commenter=False)

    if os.getenv("OTEL_INSTRUMENT_PSYCOPG2", "").lower() in ("1", "true", "yes"):
        from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor

        Psycopg2Instrumentor().instrument(skip_dep_check=True)


def _instrument_redis():
    """
    One span per Redis command. Covers both users of the client: the
    /checkout/redis fast path (EXISTS / DECR / INCR) and the idempotency
    middleware (GET / SET), which is what makes a cache-hit trace legible --
    it is a server span containing exactly one GET and nothing else.
    """
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    RedisInstrumentor().instrument()


def force_flush(timeout_millis=5000):
    """Push any buffered spans now. Used by scripts that exit immediately
    after driving a request, and by the shutdown hook."""
    if _provider is not None:
        try:
            return _provider.force_flush(timeout_millis)
        except Exception:                                      # noqa: BLE001
            return False
    return False
