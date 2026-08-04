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
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
import redis

from app.database import get_db, Base, engine, SessionLocal
from app import models
from app.ml.demand_forecast import get_forecast_sample

app = FastAPI(
    title="Flash-Sale Inventory & Oversell Prevention Engine",
    description="DBMS course project -- concurrency control, indexing, recovery, and ML demand/bot detection.",
    version="1.0",
)

r = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", "6390")),  # matches docker-compose.yml (6390:6379)
    decode_responses=True,
)


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
    # Warm the /demand-forecast cache in the background so the dashboard's
    # chart doesn't stall a live demo on the first request (training on the
    # real dataset takes well over a minute).
    threading.Thread(target=get_forecast_sample, daemon=True).start()


@app.get("/")
def root():
    return {
        "message": "Flash-Sale Inventory Engine is running.",
        "docs": "/docs",
        "endpoints": ["/sales/{sale_id}", "/checkout/pessimistic", "/checkout/optimistic", "/checkout/redis", "/flagged-orders", "/demand-forecast", "/dashboard"],
    }


@app.get("/sales/{sale_id}")
def get_sale(sale_id: str, db: Session = Depends(get_db)):
    row = db.execute(
        text("""
            SELECT p.name, p.category, f.sale_price, i.total_stock, i.reserved_stock, i.version
            FROM flash_sale_events f
            JOIN products p ON p.id = f.product_id
            JOIN inventory i ON i.sale_id = f.id
            WHERE f.id = :sid
        """),
        {"sid": sale_id},
    ).fetchone()
    if not row:
        raise HTTPException(404, "Sale not found")
    name, category, price, total, reserved, version = row
    return {
        "product": name,
        "category": category,
        "sale_price": float(price),
        "total_stock": total,
        "reserved_stock": reserved,
        "available": total - reserved,
        "version": version,
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
            raise HTTPException(404, "Sale not found")
        inv_id, total, reserved = row
        if total - reserved <= 0:
            db.rollback()
            raise HTTPException(409, "Sold out")
        db.execute(text("UPDATE inventory SET reserved_stock = reserved_stock + 1 WHERE id = :id"), {"id": inv_id})
        order_id = db.execute(
            text(
                "INSERT INTO orders (id, user_id, sale_id, status, created_at) "
                "VALUES (gen_random_uuid(), :uid, :sid, 'confirmed', now()) RETURNING id"
            ),
            {"uid": user_id, "sid": sale_id},
        ).fetchone()[0]
        db.commit()
        return {"status": "confirmed", "order_id": str(order_id), "strategy": "pessimistic"}
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
            raise HTTPException(404, "Sale not found")
        inv_id, total, reserved, version = row
        if total - reserved <= 0:
            raise HTTPException(409, "Sold out")
        result = db.execute(
            text("UPDATE inventory SET reserved_stock = reserved_stock + 1, version = version + 1 WHERE id = :id AND version = :v"),
            {"id": inv_id, "v": version},
        )
        if result.rowcount == 0:
            db.rollback()
            raise HTTPException(409, "Version conflict -- retry")
        order_id = db.execute(
            text(
                "INSERT INTO orders (id, user_id, sale_id, status, created_at) "
                "VALUES (gen_random_uuid(), :uid, :sid, 'confirmed', now()) RETURNING id"
            ),
            {"uid": user_id, "sid": sale_id},
        ).fetchone()[0]
        db.commit()
        return {"status": "confirmed", "order_id": str(order_id), "strategy": "optimistic"}
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
                raise HTTPException(404, "Sale not found")
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
            raise HTTPException(409, "Sold out (Redis fast-path)")
        try:
            order_id = db.execute(
                text(
                    "INSERT INTO orders (id, user_id, sale_id, status, created_at) "
                    "VALUES (gen_random_uuid(), :uid, :sid, 'confirmed', now()) RETURNING id"
                ),
                {"uid": user_id, "sid": sale_id},
            ).fetchone()[0]
            db.commit()
            return {"status": "confirmed", "order_id": str(order_id), "strategy": "redis"}
        except HTTPException:
            raise
        except Exception:
            db.rollback()
            r.incr(key)
            order_id = None
            raise HTTPException(500, "Order insert failed, compensated Redis counter")
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


@app.get("/dashboard")
def dashboard():
    """Section 11 (optional) -- a single-file live demo dashboard. Not a
    real deliverable, just polish for showing the system running during
    the viva. See app/static/dashboard.html."""
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "dashboard.html"))
