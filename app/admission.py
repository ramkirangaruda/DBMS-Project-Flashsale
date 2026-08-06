"""
The door. Checkout is unreachable without a valid admission token.

Implemented as ASGI middleware for the same reason app/idempotency.py is: the
three checkout handlers are not touched at all. Their locking, versioning and
Redis logic is exactly what it was -- this layer only decides whether a
request is allowed to reach them.

    No X-Admission-Token          -> 403, "join the queue"
    Token unknown or expired      -> 403, "join the queue"
    Token valid for this sale     -> passes through unchanged

WHERE THIS SITS IN THE MIDDLEWARE STACK, AND WHY IT MATTERS
    Registered AFTER install_metrics(), which in Starlette means it wraps
    outside it. Two consequences, both intended:

    * OUTSIDE the metrics middleware, so a rejected request is not counted in
      checkout_requests_total or the latency histogram. A buyer turned away at
      the door never attempted a checkout, and folding thousands of 403s into
      the checkout metrics would wreck exactly the numbers this phase exists
      to compare. Rejections get their own counter instead.

    * OUTSIDE the idempotency middleware, and this one is a genuine bug if you
      get it backwards. Idempotency caches whatever response it sees under the
      Idempotency-Key for 24 hours. If the door were inside it, a request that
      arrived a moment too early would have its 403 cached, and the same
      buyer's legitimate retry -- correct token, properly admitted -- would
      replay that 403 for the rest of the day. The door has to reject BEFORE
      anything durable is written.
"""
import json
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.metrics import ADMISSION_REJECTED, CHECKOUT_IN_FLIGHT
from app.queue import async_redis, validate

DEFAULT_PATH_PREFIXES = ("/checkout/",)
HEADER = "x-admission-token"

# Set ADMISSION_ENFORCED=false to reopen the pre-queue front door.
#
# This exists for ONE reason: results/queue_notes.md compares checkout under a
# raw 1000-buyer herd against the same load through the queue, and that
# comparison is only worth anything if the two arms differ in one variable.
# Flipping a flag on the same running binary does that; checking out an older
# commit does not, because then the build differs too.
#
# It is not a feature. In anything real the door does not have an off switch.
ENFORCED = os.getenv("ADMISSION_ENFORCED", "true").lower() not in ("false", "0", "no")


def _refused(strategy, message):
    return Response(
        content=json.dumps({
            "status": "failed",
            "order_id": None,
            "message": message,
            "strategy": strategy,
        }).encode("utf-8"),
        status_code=403,
        media_type="application/json",
        headers={"X-Admission-Required": "true"},
    )


class AdmissionMiddleware(BaseHTTPMiddleware):
    """
    Args:
        redis_client: the app's existing client, passed in so there is one
            connection pool (same reasoning as idempotency.py).
        enforced: when False, every request passes through. The in-flight
            gauge is still recorded, which is what makes the before/after
            concurrency comparison possible at all.
    """

    def __init__(self, app, redis_client=None, path_prefixes=DEFAULT_PATH_PREFIXES,
                 enforced=None):
        super().__init__(app)
        self.redis = redis_client
        self.path_prefixes = tuple(path_prefixes)
        self.enforced = ENFORCED if enforced is None else enforced

    def _applies(self, request):
        return (request.method.upper() == "POST"
                and request.url.path.startswith(self.path_prefixes))

    async def dispatch(self, request, call_next):
        if not self._applies(request):
            return await call_next(request)

        strategy = request.url.path.rsplit("/", 1)[-1] or "unknown"

        if self.enforced and self.redis is not None:
            token = request.headers.get(HEADER)
            sale_id = request.query_params.get("sale_id")

            if not token:
                ADMISSION_REJECTED.labels(reason="no_token").inc()
                return _refused(strategy,
                                "Not yet admitted. Join the queue at "
                                f"POST /queue/join/{sale_id or '{sale_id}'}, poll "
                                "GET /queue/status until admitted, then resend this "
                                "request with the X-Admission-Token header.")

            try:
                grant = await validate(async_redis(), token, sale_id)
            except Exception:                                  # noqa: BLE001
                # Redis unreachable -- fail OPEN, matching idempotency.py.
                #
                # This is a load-shedding door, not an authorisation one. If
                # Redis is down nobody can join the queue either, so failing
                # closed converts a degraded dependency into a total outage
                # and sells nothing at all. Failing open drops the protection
                # and keeps the shop working, which is the better of two bad
                # afternoons. A real system with a security boundary here
                # would make the opposite choice.
                grant = {"degraded": True}

            if grant is None:
                ADMISSION_REJECTED.labels(reason="invalid_or_expired").inc()
                return _refused(strategy,
                                "Admission token is unknown, expired, or issued for a "
                                "different sale. Rejoin the queue at "
                                f"POST /queue/join/{sale_id or '{sale_id}'}.")

        # Admitted (or the door is open). The gauge is the evidence for the
        # claim in results/queue_notes.md that checkout never sees more than
        # ~K concurrent requests -- measured here rather than asserted,
        # because this is the only point that sees every admitted request
        # enter and leave.
        CHECKOUT_IN_FLIGHT.labels(strategy=strategy).inc()
        try:
            return await call_next(request)
        finally:
            CHECKOUT_IN_FLIGHT.labels(strategy=strategy).dec()


def install(app, redis_client):
    """One-liner used by app/main.py. Keeps the wiring in one place."""
    app.add_middleware(AdmissionMiddleware, redis_client=redis_client)
    return app
