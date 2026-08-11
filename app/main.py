"""
FastAPI application tying the project together into something demoable
live in a browser/Postman, not just via scripts.

Run with: venv/bin/uvicorn app.main:app --reload --port 8000
Then visit http://localhost:8000/docs for interactive API docs.
"""
import os
import random
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
import redis

from app.database import get_db, Base, engine, SessionLocal
from app import models
from app import migrate
from app.idempotency import install as install_idempotency
from app.metrics import install as install_metrics
from app.queue import install as install_queue
from app.admission import install as install_admission
from app.payments import install as install_payments
from app.tracing import install as install_tracing
from app.ml.demand_forecast import get_forecast_sample

app = FastAPI(
    title="Flash-Sale Inventory & Oversell Prevention Engine",
    description="DBMS course project -- concurrency control, indexing, recovery, and ML demand/bot detection.",
    version="1.0",
)

# CORS so the Vite dev server (and phones on the same LAN) can call this API
# from a different origin.
#
# Defaults to "*" DELIBERATELY: this is a local/LAN classroom demo where the
# whole point is that several people open the storefront on their own devices
# and race each other. Every endpoint here is unauthenticated demo data, so
# there is nothing for a permissive origin policy to leak.
#
# THIS WOULD NEED LOCKING DOWN FOR ANYTHING REAL: set CORS_ORIGINS to an
# explicit comma-separated allowlist (e.g. "https://shop.example.com") before
# exposing this beyond a trusted network, and add authentication before
# allow_credentials is ever turned on -- "*" plus credentials is exactly the
# combination browsers refuse, and for good reason.
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,   # keep False while allow_origins may be "*"
    allow_methods=["*"],
    allow_headers=["*"],
)

r = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", "6390")),  # matches docker-compose.yml (6390:6379)
    decode_responses=True,
)

# Optional Idempotency-Key support for /checkout/*. Purely additive: a request
# without the header behaves exactly as before, and the checkout handlers
# below are untouched -- this only decides whether a request reaches them.
# Registered here rather than beside the CORS middleware because it needs the
# Redis client above. See app/idempotency.py.
install_idempotency(app, r)

# Prometheus instrumentation at /metrics. Added AFTER the idempotency
# middleware so it sits outside it and can see the Idempotent-Replay header
# on the final response -- that header is how cache hits are counted without
# reaching into app/idempotency.py. See app/metrics.py.
install_metrics(app, r, SessionLocal)

# Virtual waiting room: /queue/* endpoints plus the background admission
# worker that releases buyers K at a time. Adds routes and a thread; changes
# nothing about how checkout behaves once a buyer gets there.
install_queue(app, r, SessionLocal)

# The door in front of /checkout/*. Registered AFTER install_metrics, which
# means it wraps OUTSIDE it -- so a buyer turned away is not counted as a
# checkout attempt, and (more importantly) is rejected before the idempotency
# middleware can cache a 403 under their key for 24 hours. See the stack note
# in app/admission.py. The checkout handlers themselves are untouched.
install_admission(app, r)

# Stripe test-mode payment step, layered on top of an already-confirmed
# order. New router only -- no middleware, so where this line sits relative
# to idempotency/admission/metrics does not matter the way it does for
# them. Money never touches Postgres: payment state lives in Redis only,
# the same pattern as idempotency.py and queue.py. See app/payments.py.
install_payments(app, r)

# OpenTelemetry tracing, exported to Jaeger. Registered LAST so its middleware
# is the outermost one: the server span then covers the whole exchange,
# including the idempotency short-circuit -- which is the entire point of the
# cache-hit trace, since a replayed request never reaches a checkout handler
# and would otherwise produce no span at all.
#
# Auto-instrumentation only; there is not one hand-written span inside any
# checkout handler. See app/tracing.py.
install_tracing(app, engine=engine)


def log_checkout_attempt(user_id, sale_id, order_id, page_load_time, checkout_time, ip_address):
    """
    Records a UserBehaviorLog row for this checkout attempt -- success or
    failure alike -- so the Section 10.2 bot-detection scoring pass
    (scripts/run_bot_scoring.py) has real session data to work from.

    Uses its OWN connection/transaction, independent of the checkout's own
    session. That matters: a rejected (sold out) or conflicted checkout
    calls db.rollback() on its own session, which would silently erase the
    log row too if it were part of the same transaction -- and a rejected
    attempt is itself a useful signal (bots retry fast after a rejection),
    not something to discard.
    """
    checkout_time = checkout_time or datetime.now(timezone.utc)
    page_load_time = page_load_time or (checkout_time - timedelta(milliseconds=random.randint(8_000, 60_000)))
    ip_address = ip_address or f"203.0.113.{random.randint(1, 254)}"  # TEST-NET-3, safe placeholder
    session_duration_ms = max(0, int((checkout_time - page_load_time).total_seconds() * 1000))

    log_db = SessionLocal()
    try:
        log_db.execute(
            text(
                "INSERT INTO user_behavior_logs "
                "(id, user_id, sale_id, order_id, page_load_time, checkout_time, ip_address, session_duration_ms) "
                "VALUES (gen_random_uuid(), :uid, :sid, :oid, :plt, :ct, :ip, :dur)"
            ),
            {
                "uid": user_id, "sid": sale_id, "oid": order_id,
                "plt": page_load_time, "ct": checkout_time, "ip": ip_address, "dur": session_duration_ms,
            },
        )
        log_db.commit()
    finally:
        log_db.close()


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    # create_all() above only creates tables that don't exist yet -- it
    # never alters an existing one, so orders.strategy (added for
    # app/payments.py's stock reconciliation) needs its own one-line,
    # idempotent ALTER. See app/migrate.py for why this isn't a real
    # Alembic migration.
    migrate.run(engine)
    # Warm the /demand-forecast cache in the background so the dashboard's
    # chart doesn't stall a live demo on the first request (training on the
    # real dataset takes well over a minute).
    threading.Thread(target=get_forecast_sample, daemon=True).start()


def redis_available(sale_id):
    """
    The Redis fast-path counter for a sale, or None if it has never been
    initialised (nobody has used /checkout/redis for this sale yet) or Redis
    is unreachable.

    Why the read endpoints consult this at all: /checkout/redis deliberately
    does NOT write inventory.reserved_stock -- Redis owns stock on the fast
    path (see the CAP note on that endpoint). So after three Redis purchases
    PostgreSQL still reports the original reserved_stock, and a storefront
    reading only SQL would show a stock counter frozen at its starting value
    while items were visibly selling out. Reporting the counter that actually
    governs whether the next tap succeeds is the honest number to show.

    Both numbers are returned side by side (`sql_available` / `redis_available`)
    rather than one silently replacing the other, so the divergence stays
    visible instead of being papered over.
    """
    try:
        raw = r.get(f"stock:{sale_id}")
        return max(0, int(raw)) if raw is not None else None
    except Exception:                                          # noqa: BLE001
        return None


def checkout_result(status: str, message: str, order_id=None, strategy: str = "", http_status: int = 200):
    """
    One response shape for all three checkout endpoints, so the storefront can
    render any outcome without special-casing which strategy produced it:

        {"status": "confirmed" | "sold_out" | "failed",
         "order_id": str | null, "message": str, "strategy": str}

    HTTP status codes are kept meaningful (200 / 409 / 404 / 500) -- the body
    is uniform, not the code. The frontend reads response.json() regardless of
    code and switches on `status`.

    Note this changes only how outcomes are REPORTED. The locking, versioning
    and Redis logic underneath is untouched.
    """
    body = {"status": status, "order_id": str(order_id) if order_id else None,
            "message": message, "strategy": strategy}
    if http_status == 200:
        return body
    return JSONResponse(status_code=http_status, content=body)


@app.get("/api")
def api_index():
    """
    Endpoint index. Lives at /api rather than / because / now serves the
    storefront SPA -- someone handed the demo link should land on a shop,
    not on JSON.
    """
    return {
        "message": "Flash-Sale Inventory Engine is running.",
        "docs": "/docs",
        "storefront": "/",
        "dashboard": "/dashboard",
        "endpoints": ["/sales", "/sales/{sale_id}", "/checkout/pessimistic", "/checkout/optimistic", "/checkout/redis", "/admin/reset-sale/{sale_id}", "/flagged-orders", "/demand-forecast", "/dashboard"],
    }


@app.get("/sales")
def list_sales(db: Session = Depends(get_db), include_ended: bool = False):
    """
    Every currently active or upcoming flash sale -- what the storefront
    homepage renders its hero slider and product grid from.

    `server_time` is returned alongside so clients can synchronise their
    countdowns against THIS clock instead of the device's own. Without it,
    two phones whose clocks differ by a few seconds would unlock "Buy Now" at
    visibly different moments and the whole point of the drop (everyone taps
    at once, and the race is real) would be lost.
    """
    rows = db.execute(
        text("""
            SELECT f.id, p.name, p.category, p.base_price, f.sale_price,
                   f.start_time, f.end_time,
                   i.total_stock, i.reserved_stock
            FROM flash_sale_events f
            JOIN products p  ON p.id = f.product_id
            JOIN inventory i ON i.sale_id = f.id
            WHERE (:include_ended OR f.end_time > now())
            ORDER BY f.start_time ASC
        """),
        {"include_ended": include_ended},
    ).fetchall()

    now = datetime.now(timezone.utc)
    sales = []
    for sid, name, category, base_price, sale_price, start, end, total, reserved in rows:
        base = float(base_price or 0)
        sale = float(sale_price or 0)
        discount = round((base - sale) / base * 100) if base > 0 else 0
        sql_avail = max(0, (total or 0) - (reserved or 0))
        rds_avail = redis_available(sid)
        available = rds_avail if rds_avail is not None else sql_avail
        sales.append({
            "sale_id": str(sid),
            "product": name,
            "category": category,
            "base_price": base,
            "sale_price": sale,
            "discount_pct": discount,
            "start_time": start.isoformat() if start else None,
            "end_time": end.isoformat() if end else None,
            "total_stock": total,
            "reserved_stock": reserved,
            "available": available,
            "sql_available": sql_avail,
            "redis_available": rds_avail,
            "sold_out": available <= 0,
            "has_started": bool(start and start <= now),
            "has_ended": bool(end and end <= now),
        })

    return {"server_time": now.isoformat(), "count": len(sales), "sales": sales}


@app.get("/sales/{sale_id}")
def get_sale(sale_id: str, db: Session = Depends(get_db)):
    row = db.execute(
        text("""
            SELECT p.name, p.category, p.base_price, f.sale_price,
                   f.start_time, f.end_time,
                   i.total_stock, i.reserved_stock, i.version
            FROM flash_sale_events f
            JOIN products p ON p.id = f.product_id
            JOIN inventory i ON i.sale_id = f.id
            WHERE f.id = :sid
        """),
        {"sid": sale_id},
    ).fetchone()
    if not row:
        raise HTTPException(404, "Sale not found")
    name, category, base_price, price, start, end, total, reserved, version = row
    now = datetime.now(timezone.utc)
    base = float(base_price or 0)
    sale = float(price)
    sql_avail = total - reserved
    rds_avail = redis_available(sale_id)
    available = rds_avail if rds_avail is not None else sql_avail
    return {
        "sale_id": sale_id,
        "product": name,
        "category": category,
        "base_price": base,
        "sale_price": sale,
        "discount_pct": round((base - sale) / base * 100) if base > 0 else 0,
        "start_time": start.isoformat() if start else None,
        "end_time": end.isoformat() if end else None,
        "total_stock": total,
        "reserved_stock": reserved,
        # `available` is the number that actually governs the next tap. When
        # the Redis fast path owns this sale that is the Redis counter; both
        # are exposed so the divergence stays visible. See redis_available().
        "available": available,
        "sql_available": sql_avail,
        "redis_available": rds_avail,
        "version": version,
        "sold_out": available <= 0,
        "has_started": bool(start and start <= now),
        "has_ended": bool(end and end <= now),
        # Poll-by-poll clock reference for the storefront countdown -- see
        # list_sales() for why the server's clock is the only one trusted.
        "server_time": now.isoformat(),
    }


@app.post("/checkout/pessimistic")
def checkout_pessimistic(
    sale_id: str, user_id: str, db: Session = Depends(get_db),
    page_load_time: Optional[datetime] = None,
    checkout_time: Optional[datetime] = None,
    ip_address: Optional[str] = None,
):
    """Locks the inventory row (SELECT ... FOR UPDATE) before checking stock."""
    order_id = None
    try:
        db.execute(text("BEGIN"))
        row = db.execute(
            text("SELECT id, total_stock, reserved_stock FROM inventory WHERE sale_id = :sid FOR UPDATE"),
            {"sid": sale_id},
        ).fetchone()
        if not row:
            db.rollback()
            return checkout_result("failed", "Sale not found.", strategy="pessimistic", http_status=404)
        inv_id, total, reserved = row
        if total - reserved <= 0:
            db.rollback()
            return checkout_result("sold_out", "Sold out -- someone beat you to it.",
                                   strategy="pessimistic", http_status=409)
        db.execute(text("UPDATE inventory SET reserved_stock = reserved_stock + 1 WHERE id = :id"), {"id": inv_id})
        order_id = db.execute(
            text(
                # `strategy` recorded here, added so app/payments.py can
                # release the right counter if a payment is later declined
                # -- see Order.strategy's docstring in app/models.py. Pure
                # bookkeeping: the lock, the UPDATE above, and everything
                # else in this function is exactly what it was.
                "INSERT INTO orders (id, user_id, sale_id, status, strategy, created_at) "
                "VALUES (gen_random_uuid(), :uid, :sid, 'confirmed', 'pessimistic', now()) RETURNING id"
            ),
            {"uid": user_id, "sid": sale_id},
        ).fetchone()[0]
        db.commit()
        return checkout_result("confirmed", "Order confirmed.", order_id, "pessimistic")
    finally:
        log_checkout_attempt(user_id, sale_id, order_id, page_load_time, checkout_time, ip_address)


@app.post("/checkout/optimistic")
def checkout_optimistic(
    sale_id: str, user_id: str, db: Session = Depends(get_db),
    page_load_time: Optional[datetime] = None,
    checkout_time: Optional[datetime] = None,
    ip_address: Optional[str] = None,
):
    """Version-based optimistic concurrency control with a single retry attempt exposed via the API."""
    order_id = None
    try:
        row = db.execute(
            text("SELECT id, total_stock, reserved_stock, version FROM inventory WHERE sale_id = :sid"),
            {"sid": sale_id},
        ).fetchone()
        if not row:
            return checkout_result("failed", "Sale not found.", strategy="optimistic", http_status=404)
        inv_id, total, reserved, version = row
        if total - reserved <= 0:
            return checkout_result("sold_out", "Sold out -- someone beat you to it.",
                                   strategy="optimistic", http_status=409)
        result = db.execute(
            text("UPDATE inventory SET reserved_stock = reserved_stock + 1, version = version + 1 WHERE id = :id AND version = :v"),
            {"id": inv_id, "v": version},
        )
        if result.rowcount == 0:
            db.rollback()
            # Not "sold out" -- someone else committed against the same version
            # first. Distinct status so the UI can invite a retry.
            return checkout_result("failed", "Version conflict -- another buyer got there first. Try again.",
                                   strategy="optimistic", http_status=409)
        order_id = db.execute(
            text(
                # See the pessimistic handler's identical note -- `strategy`
                # is bookkeeping for app/payments.py's stock reconciliation,
                # not a change to this function's own OCC logic.
                "INSERT INTO orders (id, user_id, sale_id, status, strategy, created_at) "
                "VALUES (gen_random_uuid(), :uid, :sid, 'confirmed', 'optimistic', now()) RETURNING id"
            ),
            {"uid": user_id, "sid": sale_id},
        ).fetchone()[0]
        db.commit()
        return checkout_result("confirmed", "Order confirmed.", order_id, "optimistic")
    finally:
        log_checkout_attempt(user_id, sale_id, order_id, page_load_time, checkout_time, ip_address)


@app.post("/checkout/redis")
def checkout_redis(
    sale_id: str, user_id: str, db: Session = Depends(get_db),
    page_load_time: Optional[datetime] = None,
    checkout_time: Optional[datetime] = None,
    ip_address: Optional[str] = None,
):
    """Redis atomic DECR fast path, with PostgreSQL as the source of truth for the order record."""
    key = f"stock:{sale_id}"
    order_id = None
    try:
        # Seed the counter from PostgreSQL the first time we see this sale.
        # Without this the endpoint is permanently "sold out" for any sale
        # created by scripts/seed.py: DECR on a missing key starts at -1, so
        # every request fails the `remaining < 0` check even though Postgres
        # still has stock. SET NX is atomic, so concurrent first-requests
        # cannot double-initialize -- only the first one writes, and the
        # losers fall through to the DECR below against that same value.
        if not r.exists(key):
            inv = db.execute(
                text("SELECT total_stock - reserved_stock FROM inventory WHERE sale_id = :sid"),
                {"sid": sale_id},
            ).fetchone()
            if not inv:
                return checkout_result("failed", "Sale not found.", strategy="redis", http_status=404)
            r.set(key, max(0, inv[0]), nx=True)

        # DELIBERATE CAP TRADE-OFF -- NOT AN OVERSIGHT.
        #
        # This path decrements the Redis counter and never touches
        # inventory.reserved_stock. Redis is the source of truth for stock on
        # the fast path; PostgreSQL stays the source of truth for the durable
        # Order record. The consequence is visible and intended:
        # GET /sales/{sale_id} reads reserved_stock out of PostgreSQL, so it
        # does NOT reflect purchases made through this endpoint, and the two
        # counters are allowed to diverge.
        #
        # That divergence is the Section 9 AP-vs-CP trade-off in practice: the
        # fast path favours availability and partition tolerance (an immediate,
        # non-blocking answer under flash-sale load, with most "sold out"
        # rejections never reaching PostgreSQL at all), while the SQL paths
        # (/checkout/pessimistic, /checkout/optimistic) favour strict
        # consistency. Reconciling the two counters is out of scope for the
        # fast path by design -- see demo_4_redis_atomic.py, which behaves the
        # same way for the same reason.
        remaining = r.decr(key)
        if remaining < 0:
            r.incr(key)
            return checkout_result("sold_out", "Sold out -- someone beat you to it by milliseconds.",
                                   strategy="redis", http_status=409)
        try:
            order_id = db.execute(
                text(
                    # See the pessimistic handler's identical note. For THIS
                    # strategy specifically, `strategy='redis'` is what lets
                    # a declined payment's release target the Redis counter
                    # (INCR stock:{sale_id}) instead of reserved_stock --
                    # this path never touches reserved_stock going forward
                    # either, so releasing there would silently do nothing.
                    "INSERT INTO orders (id, user_id, sale_id, status, strategy, created_at) "
                    "VALUES (gen_random_uuid(), :uid, :sid, 'confirmed', 'redis', now()) RETURNING id"
                ),
                {"uid": user_id, "sid": sale_id},
            ).fetchone()[0]
            db.commit()
            return checkout_result("confirmed", "Order confirmed.", order_id, "redis")
        except HTTPException:
            raise
        except Exception:
            db.rollback()
            r.incr(key)
            order_id = None
            return checkout_result("failed", "Order insert failed; Redis counter compensated.",
                                   strategy="redis", http_status=500)
    finally:
        log_checkout_attempt(user_id, sale_id, order_id, page_load_time, checkout_time, ip_address)


@app.get("/flagged-orders")
def flagged_orders(db: Session = Depends(get_db)):
    rows = db.execute(
        text("SELECT order_id, anomaly_score, review_status FROM flagged_orders ORDER BY flagged_at DESC LIMIT 50")
    ).fetchall()
    return [{"order_id": str(r_[0]), "anomaly_score": float(r_[1]) if r_[1] else None, "review_status": r_[2]} for r_ in rows]


@app.get("/demand-forecast")
def demand_forecast():
    """Section 10.1 model, trained once and cached in-process -- see
    app/ml/demand_forecast.get_forecast_sample(). Backs the /dashboard chart."""
    return get_forecast_sample()


ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


@app.post("/admin/reset-sale/{sale_id}")
def reset_sale(sale_id: str, stock: int = 1, token: str = "", db: Session = Depends(get_db)):
    """
    Put a sale's stock back so the same drop can be run again, without
    reseeding the whole database between rounds.

    Resets BOTH sources of truth in one go -- inventory.total_stock /
    reserved_stock / version in PostgreSQL, and the Redis fast-path counter --
    because a demo can be run under any of the three checkout strategies and
    they don't read the same place. Leaving one stale would make the next
    round start "sold out" for whichever strategy reads it.

    Past orders are deliberately NOT deleted: they're the evidence of what
    happened, and stock is the only thing that needs rewinding.

    Access: set ADMIN_TOKEN in the environment to require ?token=<value>.
    Unset (the default) leaves it open, which is fine for the LAN demo this
    exists for -- the endpoint is unlinked and resets nothing but demo stock.
    Set the token before exposing the port to anything wider.
    """
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        return JSONResponse(status_code=403,
                            content={"status": "failed", "message": "Bad or missing admin token."})
    if stock < 0:
        return JSONResponse(status_code=400,
                            content={"status": "failed", "message": "stock must be >= 0."})

    row = db.execute(
        text("SELECT id FROM inventory WHERE sale_id = :sid"), {"sid": sale_id}
    ).fetchone()
    if not row:
        return JSONResponse(status_code=404,
                            content={"status": "failed", "message": "Sale not found."})

    db.execute(
        text("""
            UPDATE inventory
            SET total_stock = :stock, reserved_stock = 0, version = 0
            WHERE sale_id = :sid
        """),
        {"stock": stock, "sid": sale_id},
    )
    db.commit()

    redis_ok = True
    try:
        r.set(f"stock:{sale_id}", stock)
    except Exception:                                          # noqa: BLE001
        redis_ok = False

    return {
        "status": "ok",
        "sale_id": sale_id,
        "total_stock": stock,
        "reserved_stock": 0,
        "available": stock,
        "redis_counter": stock if redis_ok else None,
        "message": f"Stock reset to {stock}." + ("" if redis_ok else " (Redis unreachable)"),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/dashboard")
def dashboard():
    """Section 11 (optional) -- a single-file live demo dashboard. Not a
    real deliverable, just polish for showing the system running during
    the viva. See app/static/dashboard.html."""
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "dashboard.html"))


# ---------------------------------------------------------------------------
# Storefront (frontend/dist) served by this same app.
#
# MUST stay at the very bottom of this file: a mount at "/" matches
# everything, and Starlette resolves routes in registration order, so any API
# route declared *below* it would become unreachable. New endpoints go above.
#
# One process on one port serves both the API and the site, which is what
# makes the LAN demo a single shareable URL -- and, since it is one origin,
# removes CORS from the picture entirely for the built site.
# ---------------------------------------------------------------------------
FRONTEND_DIST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "dist"
)


class SPAStaticFiles(StaticFiles):
    """
    StaticFiles that falls back to index.html on a 404.

    The storefront is a client-side-routed SPA: a friend opening
    /sale/<uuid> directly (or refreshing on it) asks the server for a path
    that has no file behind it. Plain StaticFiles would 404; React needs to
    receive index.html and resolve the route itself.
    """

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


if os.path.isdir(FRONTEND_DIST):
    app.mount("/", SPAStaticFiles(directory=FRONTEND_DIST, html=True), name="storefront")
else:
    @app.get("/")
    def storefront_missing():
        return {
            "message": "Storefront not built yet.",
            "fix": "cd frontend && npm install && npm run build",
            "then": "restart this server -- it serves frontend/dist at /",
            "api_docs": "/docs",
        }


if __name__ == "__main__":
    # `python -m app.main` -- binds 0.0.0.0 so phones and laptops on the same
    # Wi-Fi can reach it. uvicorn's own default is localhost-only, which is
    # invisible to every other device on the network.
    #
    # Defaults to 8010, not 8000, for the same reason docker-compose.yml
    # publishes Postgres on 5435 and Redis on 6390: 8000 is a crowded port and
    # is already taken on this machine by an unrelated container. Landing on
    # someone else's app is worse than failing to bind, because it looks like
    # your own frontend "didn't update". Override with PORT=... if you like.
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8010")),
        reload=False,
    )
