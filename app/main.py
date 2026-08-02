"""
FastAPI application tying the project together into something demoable
live in a browser/Postman, not just via scripts.

Run with: venv/bin/uvicorn app.main:app --reload --port 8000
Then visit http://localhost:8000/docs for interactive API docs.
"""
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
import redis

from app.database import get_db, Base, engine, SessionLocal
from app import models

app = FastAPI(
    title="Flash-Sale Inventory & Oversell Prevention Engine",
    description="DBMS course project -- concurrency control, indexing, recovery, and ML demand/bot detection.",
    version="1.0",
)

r = redis.Redis(host="localhost", port=6379, decode_responses=True)


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


@app.get("/")
def root():
    return {
        "message": "Flash-Sale Inventory Engine is running.",
        "docs": "/docs",
        "endpoints": ["/sales/{sale_id}", "/checkout/pessimistic", "/checkout/optimistic", "/checkout/redis", "/flagged-orders"],
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
