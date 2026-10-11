"""
The door. Checkout is unreachable without a valid admission token.

Implemented as ASGI middleware for the same reason app/idempotency.py is: the
three checkout handlers are not touched at all. Their locking, versioning and
Redis logic is exactly what it was -- this layer only decides whether a
request is allowed to reach them.

    No X-Admission-Token          -> 403, "join the queue"
    Token unknown or expired      -> 403, "join the queue"
    Token valid for this sale     -> claimed, then passed through unchanged

ONE ADMISSION = ONE BUYER = ONE PURCHASE  (added after an audit)
    The original door only asked "is this token alive?". A token was neither
    bound to a buyer nor consumed, and polling /queue/status renews its TTL,
    so a single queue slot could buy the entire stock through any number of
    accounts -- measured: one token, five different user_ids, 5 of 5 units.
    That defeats the point of a fair waiting room. Each token now carries a
    small state hash (admission:state:{token}), updated atomically by a Lua
    script:

      * bound to the first user_id that uses it; another user_id is refused
        (`other_buyer`)
      * at most one checkout in flight per token (`busy`) -- a double-click
        cannot buy twice
      * spent once an order is CONFIRMED (`spent`)
      * a failed attempt (sold out, OCC conflict) releases the claim, so the
        optimistic strategy's "try again" still works inside the window
      * a retry that carries the SAME Idempotency-Key as the claiming request
        is let through even when busy/spent, so the idempotency layer can
        replay the original order instead of the buyer seeing a 403 for a
        request that actually succeeded

    These refusals deliberately do NOT carry X-Admission-Required, so the
    storefront does not auto-rejoin the queue and try again on a refusal that
    is about ownership rather than expiry.

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

    Because it sees the final response, it is also the one place that can
    measure end-to-end checkout latency and outcome for the adaptive admission
    controller (app/admission_control.py).
"""
import json
import os
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app import admission_control as control
from app.metrics import ADMISSION_REJECTED, CHECKOUT_IN_FLIGHT
from app.queue import ADMISSION_TTL_S, async_redis, validate

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

STATE_KEY = "admission:state:{token}"
STATE_TTL_S = max(600, ADMISSION_TTL_S * 20)     # outlives any renewed grant
SPENT_TTL_S = 24 * 60 * 60

# Atomic claim. Returns one of: ok | replay | busy | spent | other_buyer
#   KEYS[1] state hash   ARGV[1] user_id   ARGV[2] ttl   ARGV[3] idempotency key
_CLAIM_LUA = """
local st = redis.call('HMGET', KEYS[1], 'user', 'inflight', 'spent', 'idem')
local user, inflight, spent, idem = st[1], st[2], st[3], st[4]
if user and user ~= ARGV[1] then return 'other_buyer' end
if spent == '1' or inflight == '1' then
    if idem and idem ~= '' and idem == ARGV[3] then return 'replay' end
    if spent == '1' then return 'spent' end
    return 'busy'
end
redis.call('HSET', KEYS[1], 'user', ARGV[1], 'inflight', '1', 'idem', ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 'ok'
"""

_REFUSALS = {
    "other_buyer": (403, "This admission was issued to a different buyer. "
                         "Join the queue yourself."),
    "spent": (403, "This admission has already been used for a purchase."),
    "busy": (409, "A checkout for this admission is already in progress."),
}


def _json_response(strategy, message, status_code, headers=None):
    return Response(
        content=json.dumps({
            "status": "failed",
            "order_id": None,
            "message": message,
            "strategy": strategy,
        }).encode("utf-8"),
        status_code=status_code,
        media_type="application/json",
        headers=headers or {},
    )


def _refused(strategy, message):
    return _json_response(strategy, message, 403, {"X-Admission-Required": "true"})


async def claim(ar, token, user_id, idem_key):
    return await ar.eval(_CLAIM_LUA, 1, STATE_KEY.format(token=token),
                         user_id or "", STATE_TTL_S, idem_key or "")


async def release(ar, token, confirmed):
    key = STATE_KEY.format(token=token)
    pipe = ar.pipeline()
    if confirmed:
        pipe.hset(key, mapping={"spent": "1", "inflight": "0"})
        pipe.expire(key, SPENT_TTL_S)
    else:
        pipe.hset(key, "inflight", "0")
    await pipe.execute()


def _body_status(body):
    try:
        data = json.loads(body)
        return data.get("status") if isinstance(data, dict) else None
    except Exception:                                          # noqa: BLE001
        return None


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
                 enforced=None, aredis_getter=None):
        super().__init__(app)
        self.redis = redis_client
        self.path_prefixes = tuple(path_prefixes)
        self.enforced = ENFORCED if enforced is None else enforced
        self._aredis = aredis_getter or async_redis

    def _applies(self, request):
        return (request.method.upper() == "POST"
                and request.url.path.startswith(self.path_prefixes))

    async def dispatch(self, request, call_next):
        if not self._applies(request):
            return await call_next(request)

        strategy = request.url.path.rsplit("/", 1)[-1] or "unknown"
        ar = self._aredis()
        token = None
        claimed = False

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
                grant = await validate(ar, token, sale_id)
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

            if not grant.get("degraded"):
                try:
                    verdict = await claim(
                        ar, token, request.query_params.get("user_id"),
                        request.headers.get("idempotency-key"))
                except Exception:                              # noqa: BLE001
                    verdict = "replay"                         # fail open, as above
                if verdict in _REFUSALS:
                    ADMISSION_REJECTED.labels(reason=verdict).inc()
                    code, msg = _REFUSALS[verdict]
                    return _json_response(strategy, msg, code,
                                          {"X-Admission-Refused": verdict})
                claimed = verdict == "ok"

        # Admitted (or the door is open). The gauge is the evidence for the
        # claim in results/queue_notes.md that checkout never sees more than
        # ~K concurrent requests -- measured here rather than asserted,
        # because this is the only point that sees every admitted request
        # enter and leave.
        CHECKOUT_IN_FLIGHT.labels(strategy=strategy).inc()
        measuring = control.ENABLED and ar is not None
        if measuring:
            await control.enter(ar, token if claimed else None)
        started = time.perf_counter()
        confirmed = False
        outcome = "error"
        try:
            response = await call_next(request)
            body = b"".join([chunk async for chunk in response.body_iterator])
            body_status = _body_status(body)
            confirmed = body_status == "confirmed"
            outcome = control.classify(response.status_code, body_status)
            headers = dict(response.headers)
            headers.pop("content-length", None)       # rebuilt from the buffered body
            return Response(content=body, status_code=response.status_code,
                            headers=headers,
                            media_type=response.media_type or "application/json")
        finally:
            CHECKOUT_IN_FLIGHT.labels(strategy=strategy).dec()
            if measuring:
                await control.leave(ar)
                control.record(ar, (time.perf_counter() - started) * 1000.0, outcome)
            if claimed:
                try:
                    await release(ar, token, confirmed)
                except Exception:                              # noqa: BLE001
                    pass


def install(app, redis_client):
    """One-liner used by app/main.py. Keeps the wiring in one place."""
    app.add_middleware(AdmissionMiddleware, redis_client=redis_client)
    return app
