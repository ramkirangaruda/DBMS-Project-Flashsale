"""
How many units of each sale are still free -- in Redis, so the admission
worker never has to ask PostgreSQL on its own clock.

WHY A SEPARATE ENGINE
    app/queue.py records what happened when the admission worker shared the
    checkout connection pool: once ~30 checkouts were in flight the pool was
    empty, the worker blocked on it, and admission stopped. The claim window
    needs free units every tick, so this module reads them on a background
    thread through its OWN one-connection engine, built from the same URL as
    app/database.py's engine (which is not modified). A stalled read stalls
    this thread only; the claim window keeps using the last value.

WHAT "FREE" MEANS
    SQL strategies (pessimistic, optimistic): total_stock - reserved_stock.
    Redis strategy: the stock:{sale_id} counter, which owns stock on that
    path (see the CAP note on /checkout/redis). When both exist the smaller
    one is used, which is right for either strategy.

    Between reads the door decrements fa:free:{sale_id} on every confirmed
    checkout it sees (claim_window.on_outcome), so the window reacts within a
    request, not a refresh. Every refresh re-anchors the mirror to the
    database, which also picks up units returned by declined payments.

    Off unless the claim window is on (CLAIM_WINDOW != off).
"""
import os
import threading
import time

from sqlalchemy import create_engine, text

# Only its URL is used; its connection pool is never touched here.
from app.database import engine as app_engine

FREE_KEY = "fa:free:{sale_id}"
REFRESH_S = float(os.getenv("FA_STOCK_REFRESH_S", "0.2"))
ENABLED = os.getenv("CLAIM_WINDOW", "off").strip().lower() not in ("", "off")

_started = False


def compute_free(sql_free, redis_counter):
    """Pure: the free-unit count the claim window should use."""
    vals = [v for v in (sql_free, redis_counter) if v is not None]
    if not vals:
        return None
    return max(0, min(int(v) for v in vals))


def read_free(r, sale_id):
    raw = r.get(FREE_KEY.format(sale_id=sale_id))
    return None if raw is None else max(0, int(raw))


def refresh_once(r, eng):
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT sale_id, total_stock - reserved_stock FROM inventory")).fetchall()
    pipe = r.pipeline()
    for sale_id, sql_free in rows:
        sid = str(sale_id)
        counter = r.get(f"stock:{sid}")
        free = compute_free(sql_free, None if counter is None else int(counter))
        pipe.set(FREE_KEY.format(sale_id=sid), free, ex=3600)
    pipe.execute()
    return len(rows)


def start(r):
    """Start the refresher thread once per process. No-op when disabled."""
    global _started
    if not ENABLED or _started:
        return None
    _started = True
    eng = create_engine(app_engine.url, pool_size=1, max_overflow=0, pool_pre_ping=True)

    def loop():
        while True:
            try:
                refresh_once(r, eng)
            except Exception:                                  # noqa: BLE001, S110
                pass                                           # keep the last value; try again
            time.sleep(REFRESH_S)

    t = threading.Thread(target=loop, daemon=True, name="fa-stock-view")
    t.start()
    return t
