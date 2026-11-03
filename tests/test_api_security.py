"""
API surface checks against the real app: operator endpoints honour
ADMIN_TOKEN, the table browser cannot be steered into arbitrary SQL, and bad
client input is a 4xx, never a 500.

Needs Postgres + Redis; the pure token tests at the top do not.
"""
import asyncio
import uuid

import pytest

from app.security import admin_token_ok
from tests.conftest import mint_admission, sale_state


def test_admin_token_open_when_unset(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    assert admin_token_ok("") and admin_token_ok(None) and admin_token_ok("anything")


def test_admin_token_required_when_set(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    assert admin_token_ok("s3cret")
    assert not admin_token_ok("")
    assert not admin_token_ok(None)
    assert not admin_token_ok("s3cre")
    assert not admin_token_ok("s3cret ")
    assert not admin_token_ok("S3CRET")


@pytest.fixture
def app_main():
    import app.main as m
    return m


ADMIN_CALLS = [
    ("post", "/admin/reset-sale/{sid}", {"stock": 3}),
    ("post", "/queue/reset/{sid}", {}),
    ("post", "/queue/control/reset", {}),
    ("get", "/db-explorer/tables", {}),
    ("get", "/db-explorer/tables/users", {}),
]


@pytest.mark.parametrize("method,path,params", ADMIN_CALLS)
def test_operator_endpoints_require_the_admin_token(method, path, params, app_main, make_sale,
                                                    call_app, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    sid, _ = make_sale()
    url = path.format(sid=sid)

    async def go(c):
        send = getattr(c, method)
        return (await send(url, params=params),
                await send(url, params={**params, "token": "wrong"}),
                await send(url, params={**params, "token": "s3cret"}))

    missing, wrong, right = call_app(app_main.app, go)
    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert right.status_code == 200, right.text


def test_queue_reset_without_token_does_not_revoke_admissions(app_main, make_sale, call_app,
                                                               redis_client, monkeypatch):
    """Before the fix anyone could revoke every admission of a live sale."""
    from app import queue
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    sid, _ = make_sale()
    token = mint_admission(redis_client, sid)
    redis_client.sadd(queue.TOKENS_KEY.format(sale_id=sid), token)
    try:
        r = call_app(app_main.app, lambda c: c.post(f"/queue/reset/{sid}"))
        assert r.status_code == 403
        assert redis_client.exists(queue.ADMISSION_KEY.format(token=token))
    finally:
        redis_client.delete(queue.TOKENS_KEY.format(sale_id=sid))


def test_reset_sale_without_token_leaves_stock_alone(app_main, make_sale, call_app, schema,
                                                    monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    sid, _ = make_sale(stock=2)
    r = call_app(app_main.app, lambda c: c.post(f"/admin/reset-sale/{sid}", params={"stock": 999}))
    assert r.status_code == 403
    assert sale_state(schema, sid)[0] == 2


def test_reset_sale_rejects_negative_stock(app_main, make_sale, call_app, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    sid, _ = make_sale()
    r = call_app(app_main.app, lambda c: c.post(f"/admin/reset-sale/{sid}", params={"stock": -1}))
    assert r.status_code == 400


@pytest.mark.parametrize("name", [
    'users"; DROP TABLE users; --',
    "users; SELECT 1",
    "pg_catalog.pg_authid",
    "../users",
    "USERS",
])
def test_db_explorer_only_serves_real_public_tables(name, app_main, call_app, schema,
                                                    monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    r = call_app(app_main.app, lambda c: c.get(f"/db-explorer/tables/{name}"))
    assert r.status_code == 404
    ok = call_app(app_main.app, lambda c: c.get("/db-explorer/tables/users", params={"limit": 1}))
    assert ok.status_code == 200 and ok.json()["table"] == "users"


def test_db_explorer_caps_page_size(app_main, call_app, schema, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    r = call_app(app_main.app, lambda c: c.get("/db-explorer/tables/users", params={"limit": 10_000}))
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Bad client input is a 4xx, not a 500
# ---------------------------------------------------------------------------
BAD_IDS = {
    "malformed_sale": lambda sid, user: {"sale_id": "abc", "user_id": user},
    "injection_sale": lambda sid, user: {"sale_id": "' OR 1=1 --", "user_id": user},
    "malformed_user": lambda sid, user: {"sale_id": sid, "user_id": "abc"},
    "unknown_user": lambda sid, user: {"sale_id": sid, "user_id": str(uuid.uuid4())},
}
REDIS_500 = pytest.mark.xfail(strict=True, reason=(
    "BUG, not fixed: checkout_redis catches every exception from its order INSERT "
    "and answers 500 (the Redis counter is compensated, so nothing oversells). "
    "The handler is frozen by tests/test_frozen_files.py."))


@pytest.mark.parametrize("strategy,case", [
    pytest.param(s, c, marks=REDIS_500 if (s == "redis" and c.endswith("_user")) else ())
    for s in ("pessimistic", "optimistic", "redis") for c in BAD_IDS])
def test_checkout_with_bad_ids_is_a_client_error(strategy, case, app_main, make_sale, call_app,
                                                 redis_client, schema):
    sid, (user,) = make_sale(stock=3)
    params = BAD_IDS[case](sid, user)
    token = mint_admission(redis_client, params["sale_id"])
    r = call_app(app_main.app, lambda c: c.post(f"/checkout/{strategy}", params=params,
                                                headers={"X-Admission-Token": token}))
    assert sale_state(schema, sid)[1:] == (0, 0)
    if strategy == "redis" and redis_client.get(f"stock:{sid}") is not None:
        assert redis_client.get(f"stock:{sid}") == "3"      # compensated, never leaked
    assert 400 <= r.status_code < 500, (r.status_code, r.text)


def test_checkout_without_admission_is_refused(app_main, make_sale, call_app, schema):
    sid, (user,) = make_sale()
    r = call_app(app_main.app, lambda c: c.post("/checkout/pessimistic",
                                                params={"sale_id": sid, "user_id": user}))
    assert r.status_code == 403
    assert sale_state(schema, sid)[2] == 0


def test_sale_endpoints(app_main, make_sale, call_app, redis_client):
    sid, _ = make_sale(stock=4)

    async def go(c):
        return (await c.get("/sales"), await c.get(f"/sales/{sid}"),
                await c.get(f"/sales/{uuid.uuid4()}"), await c.get("/sales/not-a-uuid"))

    listing, one, missing, bad = call_app(app_main.app, go)
    assert listing.status_code == 200
    assert sid in {s["sale_id"] for s in listing.json()["sales"]}
    body = one.json()
    assert body["available"] == body["sql_available"] == 4 and body["redis_available"] is None
    assert body["has_started"] and not body["has_ended"] and not body["sold_out"]
    assert missing.status_code == 404
    assert bad.status_code == 400

    redis_client.set(f"stock:{sid}", 1)       # Redis fast path owns stock once seeded
    body = call_app(app_main.app, lambda c: c.get(f"/sales/{sid}")).json()
    assert body["available"] == body["redis_available"] == 1 and body["sql_available"] == 4


def test_reset_sale_rewinds_both_counters(app_main, make_sale, call_app, redis_client, schema,
                                          monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    sid, users = make_sale(stock=2, n_users=2)
    tokens = [mint_admission(redis_client, sid) for _ in users]
    call_app(app_main.app, lambda c: asyncio.gather(*[
        c.post("/checkout/pessimistic", params={"sale_id": sid, "user_id": u},
               headers={"X-Admission-Token": t}) for u, t in zip(users, tokens)]))
    assert sale_state(schema, sid)[:2] == (2, 2)
    r = call_app(app_main.app, lambda c: c.post(f"/admin/reset-sale/{sid}", params={"stock": 7}))
    assert r.status_code == 200
    assert sale_state(schema, sid) == (7, 0, 2)          # orders are kept as evidence
    assert redis_client.get(f"stock:{sid}") == "7"
