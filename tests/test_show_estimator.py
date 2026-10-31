"""
The show-rate estimator: pure tests, plus the port check against the pilot.

The port check feeds the SAME admit logs to research/pilot/fa_pilot.py's own
Sim.p_hat and to app.show_estimator.censored and requires identical output.
"""
import importlib.util
import os
import random
from types import SimpleNamespace

import pytest

from app import show_estimator as se

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PILOT = os.path.join(ROOT, "research", "pilot", "fa_pilot.py")


def _pilot_sim_class():
    pytest.importorskip("asyncpg")          # the pilot imports it at module level
    spec = importlib.util.spec_from_file_location("fa_pilot", PILOT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Sim


def _pilot_p_hat(Sim, admit_log, now, ttl):
    """Call the pilot's method on a stand-in object holding exactly the
    attributes it reads."""
    s = SimpleNamespace(admit_log=admit_log, a=SimpleNamespace(ttl=ttl),
                        _ph=1.0, _ph_t=-1.0, now=lambda: now)
    return Sim.p_hat(s)


def _synthetic_log(rng, n, p_show, ttl, horizon, delay):
    """Buyers admitted uniformly over [0, horizon); each shows with p_show
    after `delay(rng)` seconds, if that is before both expiry and `horizon`."""
    log = []
    for _ in range(n):
        t = rng.uniform(0, horizon)
        d = delay(rng) if rng.random() < p_show else None
        if d is not None and (d >= ttl or t + d > horizon):
            d = None                      # not observed (yet, or ever)
        log.append([t, None, d])
    return log


DELAYS = {
    "pilot_uniform": lambda rng: rng.uniform(0.05, 0.30),
    "lognormal": lambda rng: rng.lognormvariate(0.0, 1.0),
    "slow_uniform": lambda rng: rng.uniform(0.5, 1.4),
}


@pytest.mark.parametrize("delay_name", sorted(DELAYS))
@pytest.mark.parametrize("p_show", [0.4, 0.7, 1.0])
def test_port_matches_pilot_on_identical_inputs(p_show, delay_name):
    Sim = _pilot_sim_class()
    rng = random.Random(f"{p_show}-{delay_name}")
    ttl = 1.5
    for trial in range(40):
        horizon = rng.uniform(0.2, 6.0)
        log = _synthetic_log(rng, rng.randint(0, 400), p_show, ttl, horizon, DELAYS[delay_name])
        now = horizon
        expected = _pilot_p_hat(Sim, [list(x) for x in log], now, ttl)
        got = se.censored([(t, d) for t, _truth, d in log], now, ttl)
        assert got == expected, (trial, got, expected)


def test_prior_is_one_until_a_cohort_matures():
    buyers = [(9.0, None), (9.5, 0.2), (9.9, None)]
    assert se.censored(buyers, now=10.0, ttl=1.5) == 1.0


def test_mature_cohort_alone_gives_show_fraction_with_prior():
    # 10 mature buyers, 4 showed: (4 + 2) / (10 + 2) = 0.5
    buyers = [(0.0, 0.1)] * 4 + [(0.0, None)] * 6
    assert se.censored(buyers, now=5.0, ttl=1.5) == pytest.approx(0.5)


def test_young_unshown_buyers_are_not_counted_as_no_shows():
    """The censoring case: a burst of buyers too young to have shown. The
    naive estimate collapses; the censored one barely moves."""
    mature = [(0.0, 1.0)] * 7 + [(0.0, None)] * 3          # true p = 0.7, delays = 1.0 s
    young = [(9.9, None)] * 200                              # 0.1 s old, G(0.1) = 0
    now, ttl = 10.0, 2.0
    c = se.censored(mature + young, now, ttl)
    n = se.naive(mature + young, now, ttl)
    assert c == pytest.approx((7 + 2) / (10 + 2))            # young add 0 shows and 0 expected
    assert n < 0.1 + 1e-9                                    # 9 / 212, clamped at the floor
    assert c > 0.7


def test_floor_and_ceiling():
    nobody = [(0.0, None)] * 1000 + [(0.0, 0.1)]
    assert se.censored(nobody, now=5.0, ttl=1.0) == se.DEFAULT_FLOOR
    everyone = [(0.0, 0.1)] * 50
    assert se.censored(everyone, now=5.0, ttl=1.0) == 1.0


def test_naive_matches_censored_once_everyone_is_mature():
    rng = random.Random(7)
    buyers = [(0.0, 0.3 if rng.random() < 0.6 else None) for _ in range(300)]
    assert se.naive(buyers, now=10.0, ttl=1.0) == pytest.approx(se.censored(buyers, now=10.0, ttl=1.0))


def test_converges_to_true_rate_on_a_long_stream():
    rng = random.Random(11)
    ttl, p = 2.0, 0.55
    log = _synthetic_log(rng, 5000, p, ttl, 60.0, DELAYS["lognormal"])
    lognormal_within_ttl = sum(1 for _ in range(20000) if rng.lognormvariate(0.0, 1.0) < ttl) / 20000
    est = se.censored([(t, d) for t, _x, d in log], 60.0, ttl)
    assert est == pytest.approx(p * lognormal_within_ttl, abs=0.03)


# ---------------------------------------------------------------- warm start and posterior (new; cold path above is the pilot)
def test_censored_state_cold_is_censored():
    buyers = [(0.0, 1.0), (0.0, None), (10.0, 2.0), (40.0, None)]
    p, g = se.censored_state(buyers, 50.0, 30)
    assert p == se.censored(buyers, 50.0, 30)
    assert g == [1.0, 2.0]                         # both shown buyers are mature (age >= 30)
    assert se.censored_state([(40.0, None)], 50.0, 30) == (1.0, None)


def test_warm_prior_p_before_any_cohort_matures():
    assert se.censored_state([(40.0, None)], 50.0, 30, prior_p=0.4) == (0.4, None)


def test_warm_prior_delays_stand_in_for_g_until_a_cohort_matures():
    # four young unshown buyers aged 5 s; seeded G says all shows happen by 4 s
    buyers = [(45.0, None)] * 4
    p, g = se.censored_state(buyers, 50.0, 30, prior_p=0.5, prior_delays=[1.0, 2.0, 3.0, 4.0])
    assert g == [1.0, 2.0, 3.0, 4.0]
    assert abs(p - (0 + 2 * 0.5) / (4 + 2)) < 1e-12
    # a matured cohort replaces the seed
    _p, g2 = se.censored_state([(0.0, 7.0)] + buyers, 50.0, 30, prior_delays=[1.0])
    assert g2 == [7.0]


def test_posterior_show():
    assert se.posterior_show(0.7, 0.0) == 0.7
    assert se.posterior_show(0.7, 1.0) == 0.0
    assert se.posterior_show(1.0, 0.5) == 1.0          # everyone shows: waiting proves nothing
    assert abs(se.posterior_show(0.5, 0.5) - 0.25 / 0.75) < 1e-12
    xs = [se.posterior_show(0.6, g / 10) for g in range(11)]
    assert xs == sorted(xs, reverse=True)                            # older claims weigh less


def test_cdf():
    assert se.cdf([1.0, 2.0, 3.0, 4.0], 2.5) == 0.5
    assert se.cdf([], 9.0) == 0.0
