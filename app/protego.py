"""
Protego-style baseline: drop checkouts that wait too long for a lock.

FOR EVALUATION ONLY. This is one of the generic overload controllers the
futility-aware claim window is compared against (plan section 6.2). Protego
(NSDI '23) admits by measured performance and, crucially for a contended
row, drops requests whose wait in a lock queue exceeds a delay budget. Here:

    admission       the existing AIMD controller (ADMISSION_CONTROL=aimd),
                    which is performance-driven, as Protego's admission is
    lock budget     PostgreSQL `lock_timeout` = PROTEGO_LOCK_TIMEOUT_MS on
                    connections of the CHECKOUT engine (app/database.py's
                    engine, via a connect-event listener -- the module itself
                    is not modified). app/stock_view.py's own engine is not
                    touched.
    a drop          a lock timeout (SQLSTATE 55P03) inside a checkout handler.
                    It surfaces as an exception; the handler registered here
                    turns it into 503 {"status": "dropped"} with header
                    X-Protego-Drop, so it is counted as a Protego drop rather
                    than a server error, and the door releases the buyer's
                    claim so they may retry inside their window.

The handlers' bytes are unchanged; what changes at runtime is that a lock
wait can now end in an error. Decided by the project owner for this arm only
(plan section 10). Off unless PROTEGO_LOCK_TIMEOUT_MS is set.
"""
import os

from fastapi.responses import JSONResponse
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

LOCK_TIMEOUT_MS = int(os.getenv("PROTEGO_LOCK_TIMEOUT_MS", "0") or 0)
ENABLED = LOCK_TIMEOUT_MS > 0
LOCK_NOT_AVAILABLE = "55P03"

_installed = False


def _set_lock_timeout(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    try:
        cur.execute(f"SET lock_timeout = {int(LOCK_TIMEOUT_MS)}")
    finally:
        cur.close()


def is_lock_timeout(exc):
    return getattr(getattr(exc, "orig", None), "pgcode", None) == LOCK_NOT_AVAILABLE


async def _drop_handler(request, exc):
    if not is_lock_timeout(exc):
        raise exc
    strategy = request.url.path.rsplit("/", 1)[-1] or "unknown"
    return JSONResponse(status_code=503, headers={"X-Protego-Drop": "lock_timeout", "Retry-After": "0"},
                        content={"status": "dropped", "order_id": None, "strategy": strategy,
                                 "message": "Dropped: waited too long for the item's lock. Try again."})


def install(app, engine=None):
    """Attach the lock budget to the checkout engine and the drop handler to
    the app. No-op when disabled."""
    global _installed
    if not ENABLED or _installed:
        return
    _installed = True
    if engine is None:
        from app.database import engine as checkout_engine
        engine = checkout_engine
    event.listen(engine, "connect", _set_lock_timeout)
    engine.dispose()        # connections opened before the listener get the setting on reconnect
    app.add_exception_handler(OperationalError, _drop_handler)
