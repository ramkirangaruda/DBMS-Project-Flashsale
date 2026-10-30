"""The decoupled DB concurrency limit: control law reuse, the Redis gate."""
import asyncio

import pytest
import redis.asyncio as aioredis

from app import admission_control as ac
from app import db_limiter as dl


def test_control_law_is_admission_control_step_and_cap_is_the_pool():
    assert dl.ac.step is ac.step
    assert dl.CFG.max_cwnd == 30.0
    assert dl.CFG.decrease == ac.CFG.decrease and dl.CFG.tolerance == ac.CFG.tolerance


@pytest.fixture
def dlr(redis_client, monkeypatch):
    import redis
    kw = redis_client.connection_pool.connection_kwargs
    r = redis.Redis(host=kw["host"], port=kw["port"], db=13, decode_responses=True)
    r.flushdb()
    monkeypatch.setattr(dl, "ENABLED", True)
    yield r, kw
    r.flushdb()


def _ar(kw):
    return aioredis.Redis(host=kw["host"], port=kw["port"], db=13, decode_responses=True)


def test_gate_admits_up_to_the_limit_then_waits(dlr, monkeypatch):
    r, kw = dlr
    r.set(dl.LIMIT_KEY, 2)
    monkeypatch.setattr(dl, "MAX_WAIT_S", 0.15)

    async def go():
        ar = _ar(kw)
        a = await dl.acquire(ar)
        b = await dl.acquire(ar)
        c = await dl.acquire(ar)                 # full: waits, then times out
        await dl.release(ar)
        d = await dl.acquire(ar)                 # a slot came back
        await ar.aclose()
        return a, b, c, d

    a, b, c, d = asyncio.run(go())
    assert a[0] and b[0] and d[0]
    assert not c[0] and c[1] >= 140
    assert r.hget(dl.WAIT_KEY, "timeouts") == "1"
    assert int(r.get(dl.INFLIGHT_KEY)) == 2


def test_waiter_gets_the_slot_when_released(dlr, monkeypatch):
    r, kw = dlr
    r.set(dl.LIMIT_KEY, 1)
    monkeypatch.setattr(dl, "MAX_WAIT_S", 2.0)

    async def go():
        ar = _ar(kw)
        await dl.acquire(ar)

        async def later():
            await asyncio.sleep(0.1)
            await dl.release(ar)

        rel = asyncio.create_task(later())
        got = await dl.acquire(ar)
        await rel
        await ar.aclose()
        return got

    ok, waited = asyncio.run(go())
    assert ok and 80 <= waited < 1000


def test_release_never_goes_negative(dlr):
    r, kw = dlr

    async def go():
        ar = _ar(kw)
        await dl.release(ar)
        await ar.aclose()

    asyncio.run(go())
    assert int(r.get(dl.INFLIGHT_KEY)) == 0


def test_leader_tick_slow_starts_then_halves_on_latency(dlr):
    r, _kw = dlr
    now = 2000.0

    def observe(t, ms, n=20):
        pipe = r.pipeline()
        for _ in range(n):
            pipe.hincrby(dl.OBS_KEY.format(sec=int(t)), "n", 1)
            pipe.hincrby(dl.OBS_KEY.format(sec=int(t)), f"b{ac.bucket_of(ms)}", 1)
        pipe.execute()

    observe(now, 10, n=20)
    st = dl.leader_tick(r, queue_depth=100, now=now)
    first = st.cwnd
    assert int(r.get(dl.LIMIT_KEY)) == int(first)
    observe(now + 1.5, 10, n=60)                          # throughput up: keep doubling
    st = dl.leader_tick(r, queue_depth=100, now=now + 1.5)
    assert st.cwnd == min(dl.CFG.max_cwnd, first * 2)
    before = st.cwnd
    observe(now + 3.5, 5000, n=40)                        # latency far above target
    st = dl.leader_tick(r, queue_depth=100, now=now + 3.5)
    assert st.cwnd == pytest.approx(before * dl.CFG.decrease)
    assert st.last_signal == "latency"


def test_limit_never_exceeds_the_cap(dlr):
    r, _kw = dlr
    st = ac.State(cwnd=29, ssthresh=1000, phase="avoid")
    r.set(dl.STATE_KEY, st.to_json())
    pipe = r.pipeline()
    for _ in range(50):
        pipe.hincrby(dl.OBS_KEY.format(sec=3000), "n", 1)
        pipe.hincrby(dl.OBS_KEY.format(sec=3000), f"b{ac.bucket_of(5)}", 1)
    pipe.execute()
    for i in range(5):
        st = dl.leader_tick(r, queue_depth=50, now=3000.2 + i * 1.1)
    assert st.cwnd <= dl.CFG.max_cwnd
