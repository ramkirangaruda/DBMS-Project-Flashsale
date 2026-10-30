"""
A fixed walk through the waiting room and the door, reduced to a trace that
does not depend on random tokens or timing.

tests/test_flags_off.py compares this trace with tests/golden/flags_off.json,
which was recorded from the code BEFORE any futility-aware admission module
existed (commit 53cf50e). With every new flag at its default (off), the trace
must not change: same responses, same Redis keys, same TTL behaviour.

Regenerate the golden file only from a commit whose behaviour is the reference:
    python -m tests.flags_off_scenario > tests/golden/flags_off.json
"""
import asyncio
import json
import os
import re
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

SALE = "22222222-2222-2222-2222-222222222222"
GOLDEN_DB = int(os.getenv("FLAGS_OFF_REDIS_DB", "15"))   # flushed by the scenario

_HEX = re.compile(r"[0-9a-f]{32}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _norm(value):
    """Replace every random token or id with a placeholder."""
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in sorted(value.items())
                if k not in ("joined_at", "worker_id")}
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, str):
        return _UUID.sub("<uuid>", _HEX.sub("<hex>", value))
    if isinstance(value, float):
        return round(value, 2)
    return value


def _keyspace(r):
    out = {}
    for k in r.scan_iter(count=1000):
        name = _HEX.sub("<hex>", k)
        ttl = r.ttl(k)
        entry = (r.type(k), "ttl" if ttl > 0 else ("persist" if ttl == -1 else "gone"))
        out.setdefault(name, {})
        key = f"{entry[0]}/{entry[1]}"
        out[name][key] = out[name].get(key, 0) + 1
    return dict(sorted(out.items()))


def run():
    import redis
    import redis.asyncio as aioredis
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from starlette.testclient import TestClient

    from app import admission_control, queue
    from app.admission import AdmissionMiddleware

    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6390"))
    r = redis.Redis(host=host, port=port, db=GOLDEN_DB, decode_responses=True)
    r.flushdb()
    trace = {}

    async def queue_part():
        ar = aioredis.Redis(host=host, port=port, db=GOLDEN_DB, decode_responses=True)
        joined = [await queue.join(ar, SALE) for _ in range(5)]
        await ar.aclose()
        return joined

    joined = asyncio.run(queue_part())
    trace["join"] = [_norm(j) for j in joined]
    trace["admit_3"] = queue.admit(r, SALE, batch=3)

    async def status_part():
        ar = aioredis.Redis(host=host, port=port, db=GOLDEN_DB, decode_responses=True)
        out = [await queue.status(ar, SALE, j["queue_token"]) for j in joined]
        out.append(await queue.status(ar, SALE, "no-such-token"))
        tokens = [s.get("admission_token") for s in out[:3]]
        valid = [bool(await queue.validate(ar, t, SALE)) for t in tokens]
        valid.append(bool(await queue.validate(ar, tokens[0], "other-sale")))
        await ar.aclose()
        return out, tokens, valid

    statuses, tokens, valid = asyncio.run(status_part())
    trace["status"] = [_norm(s) for s in statuses]
    trace["validate"] = valid
    trace["estimated_wait_s"] = [queue.estimated_wait_s(p) for p in (0, 1, 10, 25)]
    trace["control_snapshot"] = _norm(admission_control.snapshot())

    state = {"stock": 1, "ar": None}

    def getter():
        if state["ar"] is None:
            state["ar"] = aioredis.Redis(host=host, port=port, db=GOLDEN_DB, decode_responses=True)
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
        return {"status": "confirmed", "order_id": uuid.uuid4().hex, "message": "ok", "strategy": "x"}

    app.add_middleware(AdmissionMiddleware, redis_client=r, enforced=True, aredis_getter=getter)

    def buy(client, token, user, **params):
        headers = {"X-Admission-Token": token} if token else {}
        resp = client.post("/checkout/pessimistic",
                           params={"sale_id": SALE, "user_id": user, **params}, headers=headers)
        hdrs = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-admission")}
        return {"code": resp.status_code, "body": _norm(resp.json()), "headers": hdrs}

    with TestClient(app) as client:
        trace["door"] = [
            buy(client, None, "u1"),
            buy(client, "f" * 32, "u1"),
            buy(client, tokens[0], "u1", fail=True),
            buy(client, tokens[0], "u1"),
            buy(client, tokens[0], "u1"),
            buy(client, tokens[1], "u9"),
            buy(client, tokens[1], "u2"),
            buy(client, tokens[2], "u2"),
        ]
    trace["keyspace"] = _keyspace(r)
    r.flushdb()
    return trace


if __name__ == "__main__":
    print(json.dumps(run(), indent=1, sort_keys=True))
