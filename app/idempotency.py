"""
Optional Idempotency-Key support for the checkout endpoints.

Implemented as ASGI middleware so the checkout handlers are not touched at
all: their transaction/locking logic is exactly what it was, and this layer
sits in front deciding whether to let a request reach them.

CONTRACT
    No Idempotency-Key header      -> completely unchanged behaviour.
    Header present, key unseen     -> execute normally, cache the response.
    Header present, key seen       -> return the cached response verbatim
                                      (same status, same order_id) WITHOUT
                                      re-executing checkout.

Cached under `idempotency:{key}` for 24h.

WHY A RESERVATION AND NOT JUST GET-THEN-SET
    The interesting case is not a retry arriving a minute later -- it is two
    retries in flight at the same instant, which is precisely what a client
    that timed out and immediately re-sent produces. A naive
    "GET, miss, execute, SET" has a window where both requests miss and both
    execute, and the second confirmed order is exactly the bug idempotency is
    supposed to prevent.

    So the first request atomically RESERVES the key with SET NX. Whoever
    wins the reservation executes; anyone else waits briefly for the real
    response to be written and then replays it. The reservation carries a
    short TTL so a crashed request cannot wedge the key for 24 hours, and it
    is deleted outright if the handler raises.

    This is scoped to the checkout paths only -- it is a demo-grade
    implementation, not a general-purpose idempotency layer.
"""
import asyncio
import json
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

IDEMPOTENCY_TTL_S = 24 * 60 * 60      # how long a completed result is replayable
RESERVATION_TTL_S = 60                # bounds a crashed in-flight request
RESERVATION_MARKER = "__in_progress__"
WAIT_TIMEOUT_S = 15.0                 # how long a loser waits for the winner
WAIT_POLL_S = 0.01

DEFAULT_PATH_PREFIXES = ("/checkout/",)


def _key(raw: str) -> str:
    return f"idempotency:{raw}"


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """
    Args:
        redis_client: the app's existing Redis client (passed in rather than
            constructed here, so there is one connection pool and no import
            cycle with app.main).
        path_prefixes: which routes participate. Everything else is passed
            straight through untouched.
    """

    def __init__(self, app, redis_client=None, path_prefixes=DEFAULT_PATH_PREFIXES):
        super().__init__(app)
        self.redis = redis_client
        self.path_prefixes = tuple(path_prefixes)

    # -- helpers -----------------------------------------------------------
    def _applies(self, request):
        if self.redis is None:
            return False
        if request.method.upper() != "POST":
            return False
        return request.url.path.startswith(self.path_prefixes)

    @staticmethod
    def _replay(payload: dict, key: str) -> Response:
        return Response(
            content=payload["body"].encode("utf-8"),
            status_code=payload["status_code"],
            media_type="application/json",
            # Observable so a client (and the chaos harness) can tell a cached
            # replay from a fresh execution.
            headers={"Idempotent-Replay": "true", "Idempotency-Key": key},
        )

    async def _await_winner(self, redis_key, key):
        """Another request holds the reservation; wait for its result."""
        deadline = asyncio.get_event_loop().time() + WAIT_TIMEOUT_S
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(WAIT_POLL_S)
            raw = self.redis.get(redis_key)
            if raw is None:
                # Winner died and cleaned up; let this request execute instead.
                return None
            if raw != RESERVATION_MARKER:
                return self._replay(json.loads(raw), key)
        return Response(
            content=json.dumps({
                "status": "failed",
                "order_id": None,
                "message": "A request with this Idempotency-Key is still in flight.",
                "strategy": "",
            }).encode("utf-8"),
            status_code=409,
            media_type="application/json",
            headers={"Idempotency-Key": key},
        )

    # -- main --------------------------------------------------------------
    async def dispatch(self, request, call_next):
        key = request.headers.get("idempotency-key")
        if not key or not self._applies(request):
            return await call_next(request)          # unchanged path

        redis_key = _key(key)

        try:
            cached = self.redis.get(redis_key)
        except Exception:                                      # noqa: BLE001
            # Redis unreachable -- fail open. Losing idempotency is strictly
            # better than refusing to sell anything.
            return await call_next(request)

        if cached is not None and cached != RESERVATION_MARKER:
            return self._replay(json.loads(cached), key)

        if cached == RESERVATION_MARKER:
            replayed = await self._await_winner(redis_key, key)
            if replayed is not None:
                return replayed

        # Try to become the executor. NX makes this atomic, so exactly one
        # concurrent retry gets through to the checkout handler.
        try:
            won = self.redis.set(redis_key, RESERVATION_MARKER,
                                 nx=True, ex=RESERVATION_TTL_S)
        except Exception:                                      # noqa: BLE001
            return await call_next(request)

        if not won:
            replayed = await self._await_winner(redis_key, key)
            if replayed is not None:
                return replayed
            # Winner vanished; fall through and execute.

        try:
            response = await call_next(request)
            body = b"".join([chunk async for chunk in response.body_iterator])
        except Exception:
            # Never leave a reservation behind on failure -- a later retry
            # must be able to proceed.
            try:
                self.redis.delete(redis_key)
            except Exception:                                  # noqa: BLE001
                pass
            raise

        try:
            self.redis.set(
                redis_key,
                json.dumps({"status_code": response.status_code,
                            "body": body.decode("utf-8")}),
                ex=IDEMPOTENCY_TTL_S,
            )
        except Exception:                                      # noqa: BLE001
            pass

        headers = dict(response.headers)
        headers.pop("content-length", None)      # rebuilt from the buffered body
        headers["Idempotency-Key"] = key
        headers["Idempotent-Replay"] = "false"
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type or "application/json",
        )


def install(app, redis_client):
    """One-liner used by app/main.py. Keeps the wiring in one place."""
    app.add_middleware(IdempotencyMiddleware, redis_client=redis_client)
    return app


# Environment fallback, only used if someone wires this up standalone.
def default_redis():
    import redis as _redis

    return _redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6390")),
        decode_responses=True,
    )
