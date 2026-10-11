"""
Needs Postgres + Redis (skipped otherwise). Checks the real handlers against a
real database, with its own throw-away sale so it never disturbs seeded data.
"""
import uuid

import pytest
from sqlalchemy import text


@pytest.fixture
def sale(db_engine, redis_client):
    from app.models import Base
    Base.metadata.create_all(bind=db_engine)
    pid, sid, uid = (str(uuid.uuid4()) for _ in range(3))
    with db_engine.begin() as c:
        c.execute(text("INSERT INTO products (id,name,base_price) VALUES (:i,'t',10)"), {"i": pid})
        c.execute(text("INSERT INTO flash_sale_events (id,product_id,start_time,end_time,sale_price) "
                       "VALUES (:i,:p,now()-interval '1h',now()+interval '1h',5)"), {"i": sid, "p": pid})
        c.execute(text("INSERT INTO inventory (id,sale_id,total_stock,reserved_stock,version) "
                       "VALUES (gen_random_uuid(),:s,0,0,0)"), {"s": sid})
        c.execute(text("INSERT INTO users (id,name,email) VALUES (:i,'t',:e)"),
                  {"i": uid, "e": f"{uid}@t.test"})
    yield sid, uid
    with db_engine.begin() as c:
        c.execute(text("DELETE FROM user_behavior_logs WHERE sale_id=:s"), {"s": sid})
        c.execute(text("DELETE FROM orders WHERE sale_id=:s"), {"s": sid})
        c.execute(text("DELETE FROM inventory WHERE sale_id=:s"), {"s": sid})
        c.execute(text("DELETE FROM flash_sale_events WHERE id=:s"), {"s": sid})
        c.execute(text("DELETE FROM users WHERE id=:u"), {"u": uid})
        c.execute(text("DELETE FROM products WHERE id=:p"), {"p": pid})
    redis_client.delete(f"stock:{sid}")


@pytest.mark.parametrize("strategy", ["pessimistic", "optimistic"])
def test_sold_out_and_not_found_hold_no_connection_while_logging(db_engine, sale, strategy, monkeypatch):
    """
    Audit finding: the optimistic handler returned from its sold-out and
    not-found branches with the SELECT's transaction still open, so the
    `finally` that writes the behaviour log needed a SECOND pooled
    connection. Under a sold-out stampede that is two connections per request
    against a 30-connection pool.
    """
    import app.main as m
    from app.database import SessionLocal

    sid, uid = sale
    seen = []
    real = m.log_checkout_attempt

    def spy(*a, **k):
        seen.append(db_engine.pool.checkedout())
        return real(*a, **k)

    monkeypatch.setattr(m, "log_checkout_attempt", spy)
    handler = getattr(m, f"checkout_{strategy}")

    db = SessionLocal()
    try:
        handler(sid, uid, db)                              # stock 0 -> sold out
        handler(str(uuid.uuid4()), uid, db)                # unknown sale
    finally:
        db.close()
    assert seen == [0, 0], f"connections still checked out while logging: {seen}"
