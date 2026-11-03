"""
Shared fixtures. The unit tests are pure; the integration tests need the
docker-compose Redis (6390) and Postgres (5435) and SKIP, not fail, without
them -- so `pytest` is always safe to run.
"""
import os
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

# CI sets REQUIRE_SERVICES=1 so a missing Postgres/Redis FAILS the run instead
# of quietly skipping every integration test and reporting green.
REQUIRE_SERVICES = os.getenv("REQUIRE_SERVICES") == "1"


def _unavailable(msg):
    if REQUIRE_SERVICES:
        pytest.fail(msg + " -- REQUIRE_SERVICES=1", pytrace=False)
    pytest.skip(msg)


@pytest.fixture(scope="session")
def redis_client():
    import redis
    from app.redisconf import make_redis_client
    r = make_redis_client()
    try:
        r.ping()
    except redis.RedisError:
        _unavailable("Redis not reachable (docker compose up -d redis)")
    return r


@pytest.fixture
def uniq():
    return uuid.uuid4().hex[:12]


@pytest.fixture(scope="session")
def db_engine():
    from sqlalchemy import text
    from app.database import engine
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:                                          # noqa: BLE001
        _unavailable("Postgres not reachable (docker compose up -d postgres)")
    return engine


# ---------------------------------------------------------------------------
# Integration helpers for the full app (Postgres + Redis; skip without them)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def schema(db_engine):
    """Tables plus app/migrate.py's additions (orders.strategy, payments)."""
    from app import migrate
    from app.models import Base
    Base.metadata.create_all(bind=db_engine)
    migrate.run(db_engine)
    return db_engine


@pytest.fixture
def make_sale(schema, redis_client):
    """Factory for a throw-away, currently-open sale with `stock` units and
    `n_users` buyers. Everything it creates is deleted afterwards."""
    from sqlalchemy import text

    created = []

    def make(stock=5, n_users=1, price=5):
        pid, sid = str(uuid.uuid4()), str(uuid.uuid4())
        users = [str(uuid.uuid4()) for _ in range(n_users)]
        with schema.begin() as c:
            c.execute(text("INSERT INTO products (id,name,base_price) VALUES (:i,'t',10)"), {"i": pid})
            c.execute(text("INSERT INTO flash_sale_events (id,product_id,start_time,end_time,sale_price) "
                           "VALUES (:i,:p,now()-interval '1h',now()+interval '1h',:price)"),
                      {"i": sid, "p": pid, "price": price})
            c.execute(text("INSERT INTO inventory (id,sale_id,total_stock,reserved_stock,version) "
                           "VALUES (gen_random_uuid(),:s,:n,0,0)"), {"s": sid, "n": stock})
            for u in users:
                c.execute(text("INSERT INTO users (id,name,email) VALUES (:i,'t',:e)"),
                          {"i": u, "e": f"{u}@t.test"})
        redis_client.delete(f"stock:{sid}")
        created.append((pid, sid, users))
        return sid, users

    yield make

    with schema.begin() as c:
        for pid, sid, users in created:
            c.execute(text("DELETE FROM payments WHERE order_id IN (SELECT id FROM orders WHERE sale_id=:s)"),
                      {"s": sid})
            c.execute(text("DELETE FROM stock_audit_log WHERE inventory_id IN "
                           "(SELECT id FROM inventory WHERE sale_id=:s)"), {"s": sid})
            c.execute(text("DELETE FROM user_behavior_logs WHERE sale_id=:s"), {"s": sid})
            c.execute(text("DELETE FROM flagged_orders WHERE order_id IN (SELECT id FROM orders WHERE sale_id=:s)"),
                      {"s": sid})
            c.execute(text("DELETE FROM orders WHERE sale_id=:s"), {"s": sid})
            c.execute(text("DELETE FROM inventory WHERE sale_id=:s"), {"s": sid})
            c.execute(text("DELETE FROM flash_sale_events WHERE id=:s"), {"s": sid})
            for u in users:
                c.execute(text("DELETE FROM users WHERE id=:u"), {"u": u})
            c.execute(text("DELETE FROM products WHERE id=:p"), {"p": pid})
    for _, sid, _ in created:
        redis_client.delete(f"stock:{sid}")


def sale_state(engine, sale_id):
    """(total_stock, reserved_stock, confirmed orders) for one sale."""
    from sqlalchemy import text
    with engine.connect() as c:
        total, reserved = c.execute(text(
            "SELECT total_stock, reserved_stock FROM inventory WHERE sale_id=:s"), {"s": sale_id}).one()
        orders = c.execute(text(
            "SELECT count(*) FROM orders WHERE sale_id=:s AND status='confirmed'"), {"s": sale_id}).scalar()
    return total, reserved, orders


def mint_admission(redis_client, sale_id, ttl=60):
    """An admission token exactly as the queue's admit() writes one."""
    import json
    from app.queue import ADMISSION_KEY
    token = uuid.uuid4().hex
    redis_client.set(ADMISSION_KEY.format(token=token), json.dumps({"sale_id": sale_id}), ex=ttl)
    return token


@pytest.fixture
def call_app(redis_client, monkeypatch):
    """
    Run a coroutine against an ASGI app on a fresh event loop. The app's async
    Redis client (app.queue._aredis, used by the door, the idempotency layer and
    /queue/*) is swapped for one created on that loop -- a redis.asyncio client
    cannot be shared across event loops.

        results = call_app(app, lambda c: asyncio.gather(c.post(...), ...))
    """
    import asyncio

    import httpx
    import redis.asyncio as aioredis

    from app import queue

    def run(app, fn):
        async def main():
            kw = redis_client.connection_pool.connection_kwargs
            ar = aioredis.Redis(host=kw["host"], port=kw["port"], decode_responses=True,
                                max_connections=200)
            monkeypatch.setattr(queue, "_aredis", ar)
            try:
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                             timeout=60) as c:
                    return await fn(c)
            finally:
                await ar.aclose()
        return asyncio.run(main())

    return run
