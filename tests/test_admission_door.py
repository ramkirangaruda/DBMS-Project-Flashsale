"""
The admission door: one admission = one buyer = one purchase.

Regression tests for the audit finding "a single queue slot bought the whole
stock through five accounts".
"""
import json
import uuid

import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from app.admission import AdmissionMiddleware
from app.idempotency import install as install_idempotency
from app.queue import ADMISSION_KEY

SALE = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def door(redis_client):
    """A stub checkout behind the real door (and the real idempotency layer,
    in the production order: door outside, idempotency inside)."""
    state = {"stock": 5, "orders": [], "ar": None}

    def getter():
        if state["ar"] is None:
            kw = redis_client.connection_pool.connection_kwargs
            state["ar"] = aioredis.Redis(host=kw["host"], port=kw["port"], decode_responses=True)
        return state["ar"]

    app = FastAPI()

    @app.post("/checkout/pessimistic")
    def checkout(sale_id: str, user_id: str, fail: bool = False):
        if fail:
            return JSONResponse(status_code=409, content={
                "status": "failed", "order_id": None, "message": "Version conflict", "strategy": "x"})
        if state["stock"] <= 0:
            return JSONResponse(status_code=409, content={
                "status": "sold_out", "order_id": None, "message": "gone", "strategy": "x"})
        state["stock"] -= 1
        oid = uuid.uuid4().hex
        state["orders"].append((user_id, oid))
        return {"status": "confirmed", "order_id": oid, "message": "ok", "strategy": "x"}

    install_idempotency(app, redis_client)
    app.add_middleware(AdmissionMiddleware, redis_client=redis_client, enforced=True,
                       aredis_getter=getter)

    def mint():
        token = uuid.uuid4().hex
        redis_client.set(ADMISSION_KEY.format(token=token),
                         json.dumps({"sale_id": SALE}), ex=60)
        return token

    with TestClient(app) as client:
        yield client, state, mint


def buy(client, token, user, **kw):
    headers = {"X-Admission-Token": token}
    headers.update(kw.pop("headers", {}))
    return client.post("/checkout/pessimistic",
                       params={"sale_id": SALE, "user_id": user, **kw}, headers=headers)


def test_single_token_cannot_buy_through_many_accounts(door):
    client, state, mint = door
    token = mint()
    results = [buy(client, token, f"user-{i}") for i in range(8)]
    assert sum(r.json()["status"] == "confirmed" for r in results) == 1
    assert len(state["orders"]) == 1
    assert results[1].status_code == 403
    assert results[1].headers["X-Admission-Refused"] in ("other_buyer", "spent")
    # Refusals about ownership must not tell the storefront to rejoin & retry.
    assert "X-Admission-Required" not in results[1].headers


def test_same_buyer_cannot_buy_twice_on_one_token(door):
    client, state, mint = door
    token = mint()
    assert buy(client, token, "u1").json()["status"] == "confirmed"
    second = buy(client, token, "u1")
    assert second.status_code == 403 and second.headers["X-Admission-Refused"] == "spent"
    assert len(state["orders"]) == 1


def test_failed_attempt_releases_the_claim_so_try_again_works(door):
    client, state, mint = door
    token = mint()
    assert buy(client, token, "u1", fail=True).json()["status"] == "failed"
    assert buy(client, token, "u1").json()["status"] == "confirmed"      # OCC "try again"
    assert len(state["orders"]) == 1


def test_a_different_buyer_is_refused_even_after_a_failed_first_attempt(door):
    client, _, mint = door
    token = mint()
    buy(client, token, "u1", fail=True)
    other = buy(client, token, "u2")
    assert other.status_code == 403 and other.headers["X-Admission-Refused"] == "other_buyer"


def test_retry_with_the_same_idempotency_key_replays_the_order(door):
    """A buyer whose confirmation response was lost must get their order back,
    not a 403 for a purchase that actually succeeded."""
    client, state, mint = door
    token, key = mint(), f"k-{uuid.uuid4().hex}"
    first = buy(client, token, "u1", headers={"Idempotency-Key": key})
    retry = buy(client, token, "u1", headers={"Idempotency-Key": key})
    assert retry.status_code == 200
    assert retry.json()["order_id"] == first.json()["order_id"]
    assert len(state["orders"]) == 1


def test_a_fresh_idempotency_key_cannot_launder_a_spent_token(door):
    client, state, mint = door
    token = mint()
    buy(client, token, "u1", headers={"Idempotency-Key": f"a-{uuid.uuid4().hex}"})
    again = buy(client, token, "u1", headers={"Idempotency-Key": f"b-{uuid.uuid4().hex}"})
    assert again.status_code == 403
    assert len(state["orders"]) == 1


def test_missing_and_unknown_tokens_still_get_the_rejoin_hint(door):
    client, _, _ = door
    r = client.post("/checkout/pessimistic", params={"sale_id": SALE, "user_id": "u"})
    assert r.status_code == 403 and r.headers["X-Admission-Required"] == "true"
    r = buy(client, "nope", "u")
    assert r.status_code == 403 and r.headers["X-Admission-Required"] == "true"


def test_token_for_another_sale_is_refused(door, redis_client):
    client, _, _ = door
    token = uuid.uuid4().hex
    redis_client.set(ADMISSION_KEY.format(token=token), json.dumps({"sale_id": "other"}), ex=60)
    assert buy(client, token, "u").status_code == 403
