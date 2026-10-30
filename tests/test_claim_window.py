"""The claim window: pure decision tests, and its Redis bookkeeping."""
import asyncio
import json

import pytest
import redis.asyncio as aioredis

from app import claim_window as cw
from app import stock_view


# ---------------------------------------------------------------- pure
def test_window_is_free_units_over_p_hat_rounded_up():
    assert cw.window(10, 1.0) == 10
    assert cw.window(10, 0.4) == 25
    assert cw.window(10, 0.3) == 34                    # ceil(33.33)
    assert cw.window(7, 0.7) == 10                     # exactly 10, not 11 from float error


def test_window_floors_p_hat_and_caps_w():
    assert cw.window(10, 0.0001, floor=0.1) == 100
    assert cw.window(10, 0.0, floor=0.1, w_max=50) == 50
    assert cw.window(10, 5.0) == 10                    # p_hat above 1 is clamped to 1


def test_window_is_zero_without_stock():
    assert cw.window(0, 0.5) == 0
    assert cw.window(None, 0.5) == 0
    assert cw.window(-3, 0.5) == 0


def test_budget_counts_unresolved_buyers():
    assert cw.budget(25, 0) == 25
    assert cw.budget(25, 20) == 5
    assert cw.budget(25, 40) == 0


def test_sold_out_needs_no_stock_and_nobody_unresolved():
    assert cw.is_sold_out(0, 0)
    assert not cw.is_sold_out(0, 3)                    # someone admitted could still free a unit
    assert not cw.is_sold_out(5, 0)
    assert not cw.is_sold_out(None, 0)                 # stock unknown: never claim sold out


def test_parse_mode():
    assert cw.parse_mode("fixed:1") == ("fixed", 1.0)
    assert cw.parse_mode("censored") == ("censored", None)
    assert cw.parse_mode("OFF") == ("off", None)
    with pytest.raises(ValueError):
        cw.parse_mode("bogus")


def test_estimate_modes():
    buyers = [(0.0, 0.5)] * 3 + [(0.0, None)] * 7     # mature, 3 of 10 showed
    assert cw.estimate("fixed", buyers, 10.0, 2.0, fixed_p=0.5) == 0.5
    assert cw.estimate("oracle", buyers, 10.0, 2.0, oracle_p=0.42) == 0.42
    assert cw.estimate("oracle", buyers, 10.0, 2.0, oracle_p=0.01) == 0.1      # floor applies
    assert cw.estimate("censored", buyers, 10.0, 2.0) == pytest.approx(5 / 12)
    assert cw.estimate("naive", buyers, 10.0, 2.0) == pytest.approx(5 / 12)


def test_outcome_kind():
    assert cw.outcome_kind(200, "confirmed") == "confirmed"
    assert cw.outcome_kind(409, "sold_out") == "sold_out"
    assert cw.outcome_kind(409, "failed") == "pending"        # OCC conflict: may retry
    assert cw.outcome_kind(500, None) == "pending"


def test_buyers_from_hashes():
    out = cw.buyers_from_hashes({"a": "10.0", "b": "11.0"}, {"a": "12.5"})
    assert sorted(out, key=lambda x: x[0]) == [(10.0, 2.5), (11.0, None)]


def test_compute_free_prefers_the_smaller_source():
    assert stock_view.compute_free(10, None) == 10
    assert stock_view.compute_free(10, 4) == 4               # redis strategy owns stock
    assert stock_view.compute_free(None, None) is None
    assert stock_view.compute_free(-2, None) == 0


# ---------------------------------------------------------------- Redis bookkeeping
SALE = "cw-test-sale"


@pytest.fixture
def cwr(redis_client, monkeypatch):
    """Claim window switched on (censored) against Redis db 14."""
    import redis
    kw = redis_client.connection_pool.connection_kwargs
    r = redis.Redis(host=kw["host"], port=kw["port"], db=14, decode_responses=True)
    r.flushdb()
    monkeypatch.setattr(cw, "ENABLED", True)
    monkeypatch.setattr(cw, "MODE", "censored")
    cw._last.clear()
    yield r, kw
    r.flushdb()


def _aredis(kw):
    return aioredis.Redis(host=kw["host"], port=kw["port"], db=14, decode_responses=True)


def test_leader_admits_free_units_at_the_prior_and_tracks_unresolved(cwr):
    r, _kw = cwr
    r.set(stock_view.FREE_KEY.format(sale_id=SALE), 5)
    now = 1000.0
    assert cw.leader_tick(r, SALE, ttl=30, now=now) == 5          # p_hat prior = 1
    pipe = r.pipeline()
    cw.track_grants(pipe, SALE, ["t1", "t2", "t3"], now, 30)
    pipe.execute()
    assert cw.leader_tick(r, SALE, ttl=30, now=now + 0.2) == 2
    st = json.loads(r.get(cw.STATE_KEY.format(sale_id=SALE)))
    assert st["unresolved"] == 3 and st["window"] == 5 and st["free"] == 5


def test_expired_grants_free_their_slots_and_count_as_no_shows(cwr):
    r, _kw = cwr
    r.set(stock_view.FREE_KEY.format(sale_id=SALE), 2)
    pipe = r.pipeline()
    cw.track_grants(pipe, SALE, ["a", "b"], 1000.0, 30)
    pipe.execute()
    assert cw.leader_tick(r, SALE, ttl=30, now=1010.0) == 0
    assert cw.leader_tick(r, SALE, ttl=30, now=1031.0) > 0
    stats = r.hgetall(cw.STATS_KEY.format(sale_id=SALE))
    assert stats["expired_unshown"] == "2"


def test_outcomes_resolve_claims_and_decrement_free(cwr):
    r, kw = cwr
    r.set(stock_view.FREE_KEY.format(sale_id=SALE), 2)
    pipe = r.pipeline()
    cw.track_grants(pipe, SALE, ["a", "b", "c"], 1000.0, 30)
    pipe.execute()

    async def go():
        ar = _aredis(kw)
        await cw._on_outcome(ar, SALE, "a", "confirmed")
        await cw._on_outcome(ar, SALE, "a", "confirmed")          # replay: counted once
        await cw._on_outcome(ar, SALE, "b", "sold_out")
        await ar.aclose()

    asyncio.run(go())
    assert r.zcard(cw.U_KEY.format(sale_id=SALE)) == 1
    assert r.get(stock_view.FREE_KEY.format(sale_id=SALE)) == "1"     # 2 - 1; the replay does not count
    stats = r.hgetall(cw.STATS_KEY.format(sale_id=SALE))
    assert stats["confirmed"] == "1" and stats["sold_out"] == "1"


def test_sold_out_flag_set_only_when_nothing_unresolved(cwr):
    r, _kw = cwr
    r.set(stock_view.FREE_KEY.format(sale_id=SALE), 0)
    pipe = r.pipeline()
    cw.track_grants(pipe, SALE, ["a"], 1000.0, 30)
    pipe.execute()
    cw.leader_tick(r, SALE, ttl=30, now=1001.0)
    assert r.get(cw.SOLDOUT_KEY.format(sale_id=SALE)) is None
    r.zrem(cw.U_KEY.format(sale_id=SALE), "a")
    cw.leader_tick(r, SALE, ttl=30, now=1002.0)
    assert r.get(cw.SOLDOUT_KEY.format(sale_id=SALE)) == "1"
    r.set(stock_view.FREE_KEY.format(sale_id=SALE), 1)                # a payment released a unit
    cw.leader_tick(r, SALE, ttl=30, now=1003.0)
    assert r.get(cw.SOLDOUT_KEY.format(sale_id=SALE)) is None


def test_unknown_stock_admits_nobody(cwr):
    r, _kw = cwr
    assert cw.leader_tick(r, SALE, ttl=30, now=1000.0) == 0


def test_show_delay_is_measured_from_notification(cwr):
    r, kw = cwr
    pipe = r.pipeline()
    cw.track_grants(pipe, SALE, ["a"], 1000.0, 30)
    cw.note_notified(pipe, SALE, "a", 1004.0, 30)
    pipe.execute()

    async def go():
        ar = _aredis(kw)
        await cw._on_show(ar, SALE, "a", 1006.5)
        await cw._on_show(ar, SALE, "a", 1009.0)                  # only the first show counts
        await ar.aclose()

    asyncio.run(go())
    buyers = cw.buyers_from_hashes(r.hgetall(cw.T0_KEY.format(sale_id=SALE)),
                                   r.hgetall(cw.SHOW_KEY.format(sale_id=SALE)))
    assert buyers == [(1004.0, 2.5)]
    assert r.zscore(cw.U_KEY.format(sale_id=SALE), "a") == 1034.0
