"""Idempotency layer: what is replayed, and (the audit fix) what is not."""
import json
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from app.idempotency import install, is_cacheable


@pytest.mark.parametrize("code,body,expected", [
    (200, {"status": "confirmed"}, True),
    (409, {"status": "sold_out"}, True),            # terminal: replaying it is correct
    (404, {"status": "failed", "message": "Sale not found."}, True),
    (409, {"status": "failed", "message": "Version conflict -- Try again."}, False),
    (500, {"status": "failed"}, False),
    (503, {"status": "failed"}, False),
    (429, {"status": "failed"}, False),
])
def test_is_cacheable(code, body, expected):
    assert is_cacheable(code, json.dumps(body).encode()) is expected


def test_non_json_error_is_not_cached():
    assert is_cacheable(502, b"<html>bad gateway</html>") is False
    assert is_cacheable(200, b"ok") is True


def _app(redis_client, script):
    app = FastAPI()
    calls = {"n": 0}

    @app.post("/checkout/optimistic")
    def checkout():
        calls["n"] += 1
        code, body = script(calls["n"])
        return JSONResponse(status_code=code, content=body)

    install(app, redis_client)
    return TestClient(app), calls


def test_version_conflict_retry_with_same_key_reaches_the_handler(redis_client, uniq):
    def script(n):
        if n == 1:
            return 409, {"status": "failed", "message": "Version conflict", "order_id": None}
        return 200, {"status": "confirmed", "order_id": "o1"}
    c, calls = _app(redis_client, script)
    k = {"Idempotency-Key": f"t-{uniq}"}
    assert c.post("/checkout/optimistic", headers=k).status_code == 409
    second = c.post("/checkout/optimistic", headers=k)
    assert second.status_code == 200 and second.json()["status"] == "confirmed"
    assert calls["n"] == 2


def test_server_error_is_not_cached(redis_client, uniq):
    c, calls = _app(redis_client, lambda n: (500, {"status": "failed"}) if n == 1
                    else (200, {"status": "confirmed", "order_id": "o"}))
    k = {"Idempotency-Key": f"t-{uniq}"}
    assert c.post("/checkout/optimistic", headers=k).status_code == 500
    assert c.post("/checkout/optimistic", headers=k).status_code == 200


def test_terminal_results_still_replay_without_reexecuting(redis_client, uniq):
    c, calls = _app(redis_client, lambda n: (200, {"status": "confirmed", "order_id": f"o{n}"}))
    k = {"Idempotency-Key": f"t-{uniq}"}
    a = c.post("/checkout/optimistic", headers=k)
    b = c.post("/checkout/optimistic", headers=k)
    assert a.json()["order_id"] == b.json()["order_id"] == "o1"
    assert b.headers["Idempotent-Replay"] == "true"
    assert calls["n"] == 1


def test_no_key_means_no_caching(redis_client):
    c, calls = _app(redis_client, lambda n: (200, {"status": "confirmed", "order_id": f"o{n}"}))
    c.post("/checkout/optimistic")
    c.post("/checkout/optimistic")
    assert calls["n"] == 2


def test_concurrent_duplicates_execute_exactly_once(redis_client, uniq):
    app = FastAPI()
    calls = {"n": 0}

    @app.post("/checkout/pessimistic")
    def slow():
        calls["n"] += 1
        time.sleep(0.3)
        return {"status": "confirmed", "order_id": "only"}

    install(app, redis_client)
    c = TestClient(app)
    out = []

    def fire():
        out.append(c.post("/checkout/pessimistic", headers={"Idempotency-Key": f"t-{uniq}"}).json())

    ts = [threading.Thread(target=fire) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert calls["n"] == 1
    assert {o["order_id"] for o in out} == {"only"}
