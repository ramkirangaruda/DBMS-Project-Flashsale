"""
Concurrency through the REAL app (app.main: sale-window gate, admission door,
idempotency layer, the frozen checkout handlers, Postgres and Redis).

    * oversell      -- many admitted buyers race for a few units
    * double-claim  -- one admission, many simultaneous checkouts
    * double-click  -- one buyer, one Idempotency-Key, many simultaneous sends

Needs Postgres + Redis; skipped otherwise.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import text

from tests.conftest import mint_admission, sale_state

STRATEGIES = ["pessimistic", "optimistic", "redis"]


@pytest.fixture
def app_main():
    import app.main as m
    return m


def _checkout(c, strategy, sale_id, user_id, token, key=None):
    headers = {"X-Admission-Token": token}
    if key:
        headers["Idempotency-Key"] = key
    return c.post(f"/checkout/{strategy}", params={"sale_id": sale_id, "user_id": user_id},
                  headers=headers)


def _redis_orders(engine, sale_id):
    with engine.connect() as c:
        return c.execute(text("SELECT count(*) FROM orders WHERE sale_id=:s"), {"s": sale_id}).scalar()


# ---------------------------------------------------------------------------
# Oversell
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("strategy", STRATEGIES)
def test_concurrent_buyers_never_oversell(strategy, app_main, make_sale, call_app,
                                         redis_client, schema):
    stock, buyers = 5, 40
    sid, users = make_sale(stock=stock, n_users=buyers)
    tokens = [mint_admission(redis_client, sid) for _ in users]

    responses = call_app(app_main.app, lambda c: asyncio.gather(
        *[_checkout(c, strategy, sid, u, t) for u, t in zip(users, tokens)]))

    statuses = [r.json()["status"] for r in responses]
    confirmed = statuses.count("confirmed")
    assert all(r.status_code < 500 for r in responses), [r.text for r in responses if r.status_code >= 500]
    assert confirmed <= stock
    order_ids = [r.json()["order_id"] for r in responses if r.json()["status"] == "confirmed"]
    assert len(set(order_ids)) == confirmed

    total, reserved, db_orders = sale_state(schema, sid)
    assert db_orders == confirmed
    if strategy == "redis":
        # Redis owns stock on this path; reserved_stock is deliberately untouched.
        assert confirmed == stock
        assert int(redis_client.get(f"stock:{sid}")) == 0
        assert reserved == 0
    else:
        assert reserved == confirmed <= total
        if strategy == "pessimistic":
            # Row lock serialises everyone: 40 buyers for 5 units sells all 5.
            assert confirmed == stock
            assert statuses.count("sold_out") == buyers - stock
        else:
            # One-shot OCC: losers see a version conflict, never an oversell.
            assert confirmed >= 1
            assert set(statuses) <= {"confirmed", "sold_out", "failed"}


@pytest.mark.parametrize("strategy", ["pessimistic", "optimistic"])
def test_last_unit_goes_to_exactly_one_buyer(strategy, app_main, make_sale, call_app,
                                             redis_client, schema):
    sid, users = make_sale(stock=1, n_users=25)
    tokens = [mint_admission(redis_client, sid) for _ in users]
    responses = call_app(app_main.app, lambda c: asyncio.gather(
        *[_checkout(c, strategy, sid, u, t) for u, t in zip(users, tokens)]))
    assert [r.json()["status"] for r in responses].count("confirmed") == 1
    assert sale_state(schema, sid) == (1, 1, 1)


def test_mixed_strategies_on_one_sale_do_not_oversell_sql_stock(app_main, make_sale, call_app,
                                                                 redis_client, schema):
    """Pessimistic and optimistic buyers racing on the same inventory row."""
    sid, users = make_sale(stock=4, n_users=30)
    tokens = [mint_admission(redis_client, sid) for _ in users]
    strategies = ["pessimistic", "optimistic"] * 15
    responses = call_app(app_main.app, lambda c: asyncio.gather(
        *[_checkout(c, s, sid, u, t) for s, u, t in zip(strategies, users, tokens)]))
    confirmed = [r.json()["status"] for r in responses].count("confirmed")
    total, reserved, orders = sale_state(schema, sid)
    assert confirmed == orders == reserved <= total == 4


# ---------------------------------------------------------------------------
# Double-claim: one admission token used many times at once
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("strategy", STRATEGIES)
def test_one_admission_buys_once_under_simultaneous_reuse(strategy, app_main, make_sale,
                                                          call_app, redis_client, schema):
    sid, (user,) = make_sale(stock=10, n_users=1)
    token = mint_admission(redis_client, sid)
    responses = call_app(app_main.app, lambda c: asyncio.gather(
        *[_checkout(c, strategy, sid, user, token) for _ in range(12)]))
    confirmed = [r for r in responses if r.json()["status"] == "confirmed"]
    assert len(confirmed) == 1
    refused = {r.headers.get("X-Admission-Refused") for r in responses if r not in confirmed}
    assert refused <= {"busy", "spent"}
    assert _redis_orders(schema, sid) == 1


def test_one_admission_shared_by_many_accounts_buys_once(app_main, make_sale, call_app,
                                                          redis_client, schema):
    sid, users = make_sale(stock=10, n_users=8)
    token = mint_admission(redis_client, sid)
    responses = call_app(app_main.app, lambda c: asyncio.gather(
        *[_checkout(c, "pessimistic", sid, u, token) for u in users]))
    assert [r.json()["status"] for r in responses].count("confirmed") == 1
    assert sale_state(schema, sid)[1:] == (1, 1)


# ---------------------------------------------------------------------------
# Double-click: same buyer, same Idempotency-Key, sent many times at once
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("strategy", STRATEGIES)
def test_simultaneous_retries_with_one_key_create_one_order(strategy, app_main, make_sale,
                                                             call_app, redis_client, schema):
    sid, (user,) = make_sale(stock=10, n_users=1)
    token = mint_admission(redis_client, sid)
    key = f"dbl-{uuid.uuid4().hex}"
    responses = call_app(app_main.app, lambda c: asyncio.gather(
        *[_checkout(c, strategy, sid, user, token, key) for _ in range(8)]))
    bodies = [r.json() for r in responses]
    assert {b["status"] for b in bodies} == {"confirmed"}, bodies
    assert len({b["order_id"] for b in bodies}) == 1
    assert _redis_orders(schema, sid) == 1
