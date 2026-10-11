"""A drop is a burst. The waiting room must absorb more simultaneous joins than
its Redis pool has connections instead of answering them with HTTP 500."""
import asyncio

import httpx
from fastapi import FastAPI

from app import queue


def test_hundreds_of_simultaneous_joins_all_succeed(redis_client, uniq, monkeypatch):
    monkeypatch.setattr(queue, "_worker_started", True)       # no background threads in a unit test
    monkeypatch.setattr(queue, "_known_sales", set())         # empty cache = fail open for any sale id
    app = FastAPI()
    queue.install(app, redis_client, session_factory=lambda: (_ for _ in ()).throw(RuntimeError))
    sale = f"burst-{uniq}"

    async def burst(n):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as c:
            rs = await asyncio.gather(*[c.post(f"/queue/join/{sale}") for _ in range(n)])
        return [r.status_code for r in rs]

    try:
        codes = asyncio.run(burst(600))                        # > 2x the 256-connection pool
    finally:
        redis_client.delete(queue.Z_KEY.format(sale_id=sale))
    assert codes.count(200) == 600, {c: codes.count(c) for c in set(codes)}
