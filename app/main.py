"""
FastAPI application tying the project together into something demoable
live in a browser/Postman, not just via scripts.

Run with: venv/bin/uvicorn app.main:app --reload --port 8000
Then visit http://localhost:8000/docs for interactive API docs.
"""
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
import redis

from app.database import get_db, Base, engine
from app import models

app = FastAPI(
    title="Flash-Sale Inventory & Oversell Prevention Engine",
    description="DBMS course project -- concurrency control, indexing, recovery, and ML demand/bot detection.",
    version="1.0",
)

r = redis.Redis(host="localhost", port=6379, decode_responses=True)


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
def checkout_pessimistic(sale_id: str, user_id: str, db: Session = Depends(get_db)):
    """Locks the inventory row (SELECT ... FOR UPDATE) before checking stock."""
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


@app.post("/checkout/optimistic")
def checkout_optimistic(sale_id: str, user_id: str, db: Session = Depends(get_db)):
    """Version-based optimistic concurrency control with a single retry attempt exposed via the API."""
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


@app.post("/checkout/redis")
def checkout_redis(sale_id: str, user_id: str, db: Session = Depends(get_db)):
    """Redis atomic DECR fast path, with PostgreSQL as the source of truth for the order record."""
    key = f"stock:{sale_id}"
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
    except Exception:
        db.rollback()
        r.incr(key)
        raise HTTPException(500, "Order insert failed, compensated Redis counter")


@app.get("/flagged-orders")
def flagged_orders(db: Session = Depends(get_db)):
    rows = db.execute(
        text("SELECT order_id, anomaly_score, review_status FROM flagged_orders ORDER BY flagged_at DESC LIMIT 50")
    ).fetchall()
    return [{"order_id": str(r_[0]), "anomaly_score": float(r_[1]) if r_[1] else None, "review_status": r_[2]} for r_ in rows]
