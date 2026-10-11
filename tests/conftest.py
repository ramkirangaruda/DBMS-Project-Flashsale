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


@pytest.fixture(scope="session")
def redis_client():
    import redis
    from app.redisconf import make_redis_client
    r = make_redis_client()
    try:
        r.ping()
    except redis.RedisError:
        pytest.skip("Redis not reachable (docker compose up -d redis)")
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
        pytest.skip("Postgres not reachable (docker compose up -d postgres)")
    return engine
