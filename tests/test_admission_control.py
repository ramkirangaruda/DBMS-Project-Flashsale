"""The AIMD admission controller: pure-algorithm tests (no Redis, no clock)."""
import copy

import pytest

from app import admission_control as ac

CFG = ac.Config(min_cwnd=1, max_cwnd=100, init_cwnd=2, decrease=0.5, increase_frac=0.05,
                tolerance=3.0, target_min_ms=40.0, target_max_ms=400.0, quantile=0.9, loss_threshold=0.3,
                min_samples=8, step_s=1.0, window_s=2,
                stall_inflight_min=4, tick_s=0.1)


def healthy(q=20.0, n=20, loss=0.0):
    return {"n": n, "q_ms": q, "loss": loss}


def run(st, stats, inflight=0, depth=100, now=100.0):
    return ac.step(st, stats, inflight, depth, CFG, now)


def test_histogram_quantile_is_within_one_bucket():
    hist = {}
    for ms in [10] * 90 + [200] * 10:
        b = ac.bucket_of(ms)
        hist[b] = hist.get(b, 0) + 1
    p90 = ac.quantile_from_hist(hist, 0.90)
    p99 = ac.quantile_from_hist(hist, 0.99)
    assert 10 <= p90 <= 10 * 1.25          # ~19% bucket width
    assert 200 <= p99 <= 200 * 1.25
    assert ac.quantile_from_hist({}, 0.9) is None


def test_slow_start_doubles_while_throughput_keeps_growing():
    st = ac.State(cwnd=2, ssthresh=100)
    seen = []
    for i in range(4):
        run(st, healthy(n=20 * 2 ** i), now=100 + i)       # throughput doubles with the window
        seen.append(st.cwnd)
    assert seen == [4, 8, 16, 32]
    assert st.phase == "slow_start"


def test_slow_start_stops_doubling_when_throughput_plateaus():
    """The 'full pipe' test: more window that buys no throughput is only queue."""
    st = ac.State(cwnd=2, ssthresh=1000)
    for i in range(8):
        run(st, healthy(n=40), now=100 + i)                # flat throughput
    assert st.phase == "avoid" and st.last_signal in ("full_pipe", "increase")
    assert st.ssthresh <= 16


def test_slow_start_hands_over_to_avoidance_at_ssthresh():
    st = ac.State(cwnd=32, ssthresh=40)
    run(st, healthy())
    assert st.cwnd == 64 and st.phase == "avoid"


def test_latency_congestion_halves_and_sets_ssthresh():
    st = ac.State(cwnd=40, ssthresh=100, baseline_ms=20.0, phase="avoid")
    run(st, healthy(q=200.0))                  # 10x baseline -> congested
    assert st.cwnd == 20 and st.ssthresh == 20
    assert st.phase == "avoid" and st.last_signal == "latency" and st.decreases == 1


def test_first_congestion_leaves_slow_start():
    st = ac.State(cwnd=64, ssthresh=100, baseline_ms=20.0)
    run(st, healthy(q=500.0))
    assert st.phase == "avoid" and st.cwnd == 32


def test_latency_within_tolerance_is_not_congestion():
    st = ac.State(cwnd=40, ssthresh=40, baseline_ms=20.0, phase="avoid")
    run(st, healthy(q=55.0))                   # < 3x baseline, above floor
    assert st.last_signal == "increase" and st.cwnd > 40


def test_ceiling_stops_a_polluted_baseline_from_excusing_huge_latencies():
    st = ac.State(cwnd=64, ssthresh=64, baseline_ms=300.0, phase="avoid")   # learned during a join flood
    run(st, healthy(q=700.0))                  # 3 x 300 = 900 would excuse this; the 400 ms ceiling does not
    assert st.last_signal == "latency" and st.cwnd == 32


def test_target_is_clamped_to_the_configured_range():
    assert ac.target_ms(5.0, CFG) == 40.0      # floor
    assert ac.target_ms(50.0, CFG) == 150.0    # 3 x baseline
    assert ac.target_ms(900.0, CFG) == 400.0   # ceiling


def test_floor_prevents_jitter_at_tiny_latencies():
    st = ac.State(cwnd=10, ssthresh=10, baseline_ms=2.0, phase="avoid")
    run(st, healthy(q=12.0))                   # 6x baseline but under the 40ms floor
    assert st.last_signal == "increase"


def test_additive_increase_is_at_least_one():
    st = ac.State(cwnd=4, ssthresh=4, baseline_ms=20.0, phase="avoid")
    run(st, healthy())
    assert st.cwnd == 5                       # max(1, 5% of 4)


def test_additive_increase_scales_with_window():
    st = ac.State(cwnd=80, ssthresh=80, baseline_ms=20.0, phase="avoid")
    run(st, healthy())
    assert st.cwnd == pytest.approx(84.0)


def test_loss_triggers_decrease_even_when_latency_is_fine():
    st = ac.State(cwnd=40, ssthresh=40, baseline_ms=20.0, phase="avoid")
    run(st, healthy(q=20.0, loss=0.5))
    assert st.last_signal == "loss" and st.cwnd == 20


def test_baseline_is_the_best_latency_seen_and_does_not_follow_load():
    st = ac.State(cwnd=10, ssthresh=10, phase="avoid")
    run(st, healthy(q=30.0))
    assert st.baseline_ms == 30.0
    run(st, healthy(q=20.0))
    assert st.baseline_ms == 20.0
    for _ in range(3):                         # congestion (12 -> 6 -> 3 -> 1.5) must never raise it
        run(st, healthy(q=1000.0))
    assert st.baseline_ms == 20.0 and st.cwnd == pytest.approx(1.5)


def test_stale_baseline_is_raised_only_when_the_window_is_already_at_its_floor():
    st = ac.State(cwnd=1, ssthresh=1, baseline_ms=10.0, phase="avoid")
    run(st, healthy(q=500.0))                  # even a window of 1 is 50x slower: database changed
    assert st.baseline_ms == 500.0 and st.cwnd >= 1 and st.decreases == 0


def test_idle_queue_without_samples_resets_so_the_next_drop_recalibrates():
    st = ac.State(cwnd=64, ssthresh=64, baseline_ms=20.0, phase="avoid")
    st = run(st, {"n": 0, "q_ms": None, "loss": 0.0}, depth=0)
    assert st.cwnd == ac.CFG.init_cwnd and st.baseline_ms is None
    assert st.phase == "slow_start" and st.last_signal == "idle"


def test_waiting_buyers_without_samples_still_probe_upward():
    st = ac.State(cwnd=4, ssthresh=4, phase="avoid")
    run(st, {"n": 0, "q_ms": None, "loss": 0.0}, inflight=0, depth=500)
    assert st.cwnd > 4


def test_stall_is_detected_when_the_window_is_full_and_nothing_finishes():
    st = ac.State(cwnd=20, ssthresh=20, phase="avoid")
    run(st, {"n": 1, "q_ms": 900.0, "loss": 0.0}, inflight=20, depth=500)
    assert st.last_signal == "stall" and st.cwnd == 10


def test_no_stall_while_the_window_still_has_room():
    st = ac.State(cwnd=20, ssthresh=20, phase="avoid")
    run(st, {"n": 1, "q_ms": 900.0, "loss": 0.0}, inflight=5, depth=500)
    assert st.last_signal != "stall" and st.cwnd >= 20


def test_slots_is_window_minus_outstanding():
    assert ac.slots(8.9, 3) == 5
    assert ac.slots(8.9, 8) == 0
    assert ac.slots(2.0, 50) == 0               # never negative


def test_bounds_are_respected():
    st = ac.State(cwnd=90, ssthresh=1000)
    for i in range(5):
        run(st, healthy(), now=100 + i)
    assert st.cwnd == 100
    st = ac.State(cwnd=1.2, ssthresh=1.2, baseline_ms=10.0, phase="avoid")
    run(st, healthy(q=999.0), now=100)
    assert st.cwnd == 1                        # halved from 1.2 but clamped at the floor


def test_converges_near_the_knee_of_a_simulated_serial_database():
    """
    Closed-loop toy: a single-server database with service time s. Latency at
    window w is w*s (queueing behind the row lock), throughput saturates at
    1/s as soon as w >= 1. Unloaded latency is ~s, so the controller should
    stop growing the window once latency passes tolerance x baseline -- i.e.
    settle at a SMALL window (the knee), not climb to the cap, and not
    collapse to the floor.
    """
    s_ms = 10.0
    st = ac.State(cwnd=2, ssthresh=1000)
    cfg = ac.Config(**{**CFG.__dict__, "max_cwnd": 1000})
    history = []
    for t in range(120):
        w = max(1, int(st.cwnd))
        q = w * s_ms                           # p90 latency at this window
        n = int(2 * 1000 / s_ms)               # saturated throughput over the 2 s window
        ac.step(st, {"n": n, "q_ms": q, "loss": 0.0, "secs": 2}, 0, 100, cfg, 100 + t)
        history.append(st.cwnd)
    tail = history[60:]
    assert max(tail) <= 12                     # nowhere near the cap
    assert min(tail) >= 2                      # did not collapse
    assert st.decreases >= 3                   # kept probing and backing off (sawtooth)
    assert st.throughput == pytest.approx(100.0)


def test_observations_round_trip_through_redis(redis_client, uniq):
    import asyncio
    import redis.asyncio as aioredis
    from app.redisconf import make_redis_client
    kw = redis_client.connection_pool.connection_kwargs
    ar = aioredis.Redis(host=kw["host"], port=kw["port"], decode_responses=True)

    async def feed():
        now = 5_000_000 + hash(uniq) % 1000
        for ms in [10] * 18 + [300, 300]:
            await ac._record(ar, ms, "ok", now)
        await ac._record(ar, 10, "conflict", now)
        await ar.aclose()
        return now

    now = asyncio.run(feed())
    stats = ac.read_window(redis_client, now, 2)
    assert stats["n"] == 21
    assert stats["loss"] == pytest.approx(1 / 21)
    assert 10 <= stats["q_ms"] <= 13           # p90 of 21 samples is still a 10 ms one
    # `since` hides observations at/before a decrease
    assert ac.read_window(redis_client, now, 2, since=now)["n"] == 0
