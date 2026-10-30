"""Protego-style baseline: the lock budget reaches checkout connections only."""
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text

from app import protego


def test_disabled_by_default():
    assert protego.ENABLED is False


def test_lock_budget_applies_to_the_given_engine_only(db_engine, monkeypatch):
    monkeypatch.setattr(protego, "LOCK_TIMEOUT_MS", 75)
    monkeypatch.setattr(protego, "ENABLED", True)
    monkeypatch.setattr(protego, "_installed", False)
    checkout = create_engine(db_engine.url, pool_size=1, max_overflow=0)
    other = create_engine(db_engine.url, pool_size=1, max_overflow=0)
    app = FastAPI()
    protego.install(app, engine=checkout)
    with checkout.connect() as c:
        assert c.execute(text("SHOW lock_timeout")).scalar() == "75ms"
    with other.connect() as c:
        assert c.execute(text("SHOW lock_timeout")).scalar() == "0"
    checkout.dispose()
    other.dispose()


def test_lock_timeout_is_recognised(db_engine):
    """A real lock wait past the budget raises SQLSTATE 55P03, which the
    drop handler recognises."""
    eng = create_engine(db_engine.url, pool_size=2, max_overflow=0)
    with eng.connect() as holder, eng.connect() as waiter:
        waiter.execute(text("SET lock_timeout = 50"))
        holder.execute(text("BEGIN"))
        holder.execute(text("LOCK TABLE inventory IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(Exception) as ei:
            waiter.execute(text("SELECT 1 FROM inventory LIMIT 1 FOR UPDATE"))
        holder.execute(text("ROLLBACK"))
    eng.dispose()
    assert protego.is_lock_timeout(ei.value)
