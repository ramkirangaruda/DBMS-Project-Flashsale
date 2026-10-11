"""
Server-side enforcement of a flash sale's start and end time.

WHY THIS EXISTS
    Until now the sale window was decoration. `start_time` / `end_time` were
    returned by the API and the storefront drew a countdown from them, but no
    checkout endpoint ever looked at them -- verified by running it: with the
    window set to an hour ago, and again with it set to an hour from now,
    POST /checkout/pessimistic and /checkout/optimistic both returned
    "confirmed". Anyone with curl could buy before the drop opened, and the
    waiting room (which starts admitting immediately) made that easy.

HOW
    ASGI middleware, so the three checkout handlers stay untouched, exactly
    like app/admission.py and app/idempotency.py. It is registered OUTERMOST
    (after the admission door) so an out-of-window request is refused before
    it can consume an admission claim or reach anything durable.

    The request path never touches PostgreSQL: windows are cached in memory
    and refreshed by a background thread (or lazily, on serverless). The cost
    is staleness of up to SALE_WINDOW_REFRESH_S (default 2 s) after someone
    edits a sale's times. Unknown sale ids fail OPEN -- the handler's own 404
    is the right answer for those, not this layer's.

CONFIG
    SALE_WINDOW_ENFORCED=false   switch off (benchmarks against an old seed)
    SALE_WINDOW_REFRESH_S=2

    NOTE for the load harnesses: scripts/seed.py gives its sale a 10-minute
    window. A long benchmark against an old seed needs `python -m
    scripts.seed` again (it extends end_time) or SALE_WINDOW_ENFORCED=false.
"""
import asyncio
import json
import os
import threading
import time

from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

ENFORCED = os.getenv("SALE_WINDOW_ENFORCED", "true").lower() not in ("false", "0", "no")
REFRESH_S = float(os.getenv("SALE_WINDOW_REFRESH_S", "2"))
IS_SERVERLESS = bool(os.getenv("VERCEL"))

DEFAULT_PATH_PREFIXES = ("/checkout/",)


class SaleWindows:
    """In-memory {sale_id: (start_epoch, end_epoch)} with a refresh hook."""

    def __init__(self, session_factory=None, refresh_s=REFRESH_S, clock=time.time):
        self._session_factory = session_factory
        self._refresh_s = refresh_s
        self._clock = clock
        self._windows = {}
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def set_windows(self, windows):
        with self._lock:
            self._windows = dict(windows)
            self._loaded_at = self._clock()

    def refresh(self):
        db = self._session_factory()
        try:
            rows = db.execute(text(
                "SELECT id, EXTRACT(EPOCH FROM start_time), EXTRACT(EPOCH FROM end_time) "
                "FROM flash_sale_events"
            )).fetchall()
        finally:
            db.close()
        self.set_windows({str(r[0]): (float(r[1]), float(r[2])) for r in rows})

    def is_stale(self):
        return self._clock() - self._loaded_at > self._refresh_s

    def state(self, sale_id, now=None):
        """('open' | 'not_started' | 'ended' | 'unknown', seconds_until_open)."""
        w = self._windows.get(str(sale_id))
        if w is None:
            return "unknown", 0.0
        now = self._clock() if now is None else now
        start, end = w
        if now < start:
            return "not_started", start - now
        if now >= end:
            return "ended", 0.0
        return "open", 0.0

    def start_thread(self):
        def loop():
            while True:
                try:
                    self.refresh()
                except Exception:                              # noqa: BLE001
                    pass
                time.sleep(self._refresh_s)
        threading.Thread(target=loop, daemon=True, name="sale-window-refresher").start()


class SaleWindowMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, windows=None, path_prefixes=DEFAULT_PATH_PREFIXES,
                 enforced=None, lazy_refresh=False):
        super().__init__(app)
        self.windows = windows
        self.path_prefixes = tuple(path_prefixes)
        self.enforced = ENFORCED if enforced is None else enforced
        self.lazy_refresh = lazy_refresh

    async def dispatch(self, request, call_next):
        if (not self.enforced or self.windows is None
                or request.method.upper() != "POST"
                or not request.url.path.startswith(self.path_prefixes)):
            return await call_next(request)

        if self.lazy_refresh and self.windows.is_stale():
            try:
                await asyncio.to_thread(self.windows.refresh)
            except Exception:                                  # noqa: BLE001
                pass

        sale_id = request.query_params.get("sale_id")
        state, opens_in = self.windows.state(sale_id) if sale_id else ("unknown", 0.0)
        if state in ("not_started", "ended"):
            strategy = request.url.path.rsplit("/", 1)[-1] or "unknown"
            if state == "not_started":
                msg = f"This sale has not started yet. It opens in {opens_in:.0f}s."
            else:
                msg = "This sale has ended."
            return Response(
                content=json.dumps({"status": "failed", "order_id": None,
                                    "message": msg, "strategy": strategy}).encode("utf-8"),
                status_code=403,
                media_type="application/json",
                headers={"X-Sale-Window": state},
            )
        return await call_next(request)


def install(app, session_factory):
    """One-liner used by app/main.py."""
    windows = SaleWindows(session_factory)
    try:
        windows.refresh()                     # best effort: tables may not exist yet
    except Exception:                                          # noqa: BLE001
        pass
    if not IS_SERVERLESS:
        windows.start_thread()
    app.add_middleware(SaleWindowMiddleware, windows=windows, lazy_refresh=IS_SERVERLESS)
    return windows
