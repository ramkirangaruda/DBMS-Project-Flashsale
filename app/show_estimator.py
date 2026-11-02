"""
How many admitted buyers will actually show up? An online estimate of the
show rate p, from what the server can observe.

WHY THIS EXISTS
    The claim window (app/claim_window.py) admits ceil(free_units / p_hat)
    buyers. Admit too few and the sale crawls (every no-show holds a slot for
    the whole token TTL); admit too many and the extra buyers are doomed --
    they queue, get admitted, and lose. p_hat is the knob between the two.

THE CENSORING PROBLEM
    A buyer admitted two seconds ago who has not checked out yet is not a
    no-show. They may still come. Counting them as a no-show (the "naive"
    estimate, shows / admitted) biases p_hat DOWN early in a sale, which
    opens the window too wide exactly when the sale is most contested.

    The censoring-aware estimate splits buyers by age:

      mature (age >= TTL)  fully observed: their token has expired, so a
                           buyer who did not show is a real no-show. Their
                           show delays give G, the CDF of show delay among
                           buyers who show.
      young  (age <  TTL)  censored. Each one, shown or not, adds G(age) to
                           the expected count: the probability a showing
                           buyer would have shown by now.

        p_hat = (shows + k) / (n_mature + sum_young G(age) + k)

    with k pseudo-observations at p = 1 (a conservative prior: small window,
    few futile admissions), clamped to [floor, 1]. Before any cohort has
    matured there is no unbiased G, and p_hat stays at 1.

PROVENANCE
    This is a line-for-line port of Sim.p_hat in research/pilot/fa_pilot.py
    (same inputs, same arithmetic, same prior and clamp). The pure function
    below is checked against the pilot's own method on the same inputs in
    tests/test_show_estimator.py and research/validation/replay_pilot.py.
    The only change is the floor, 0.1, which the pilot also used.

PURE: no Redis, no clock. Everything arrives as an argument.
"""
import bisect

ENABLED = False          # this module has no switch of its own; app/claim_window.py uses it

DEFAULT_PRIOR_K = 2.0    # pseudo-observations at p = 1 (the pilot's k)
DEFAULT_FLOOR = 0.1      # decided by the project owner (plan section 10)


def censored(buyers, now, ttl, k=DEFAULT_PRIOR_K, floor=DEFAULT_FLOOR):
    """
    buyers: iterable of (clock_start, show_delay_or_None). clock_start is when
            the buyer's token clock started (first notification, or the grant
            when never notified); show_delay is seconds from clock_start to
            their first checkout attempt, None if they have not shown.
    Returns p_hat in [floor, 1].
    """
    return censored_state(buyers, now, ttl, k=k, floor=floor)[0]


def censored_state(buyers, now, ttl, k=DEFAULT_PRIOR_K, floor=DEFAULT_FLOOR,
                   prior_p=1.0, prior_delays=None):
    """
    censored(), plus the sorted show delays G is built from, and two optional
    seeds from a previous sale (the "warm" start; both off by default, which
    is exactly censored()):

      prior_p       the k pseudo-observations sit at prior_p instead of 1
      prior_delays  a sorted delay sample used as G until this sale has a
                    mature cohort of its own

    Returns (p_hat, delays). delays is None when there is no G yet.
    """
    mature_delays, n_mature, shows_mature = [], 0, 0
    young = []
    for t, delay in buyers:
        age = now - t
        if age >= ttl:
            n_mature += 1
            if delay is not None:
                shows_mature += 1
                mature_delays.append(delay)
        else:
            young.append((age, delay))
    if mature_delays:
        mature_delays.sort()
        g = mature_delays
    elif prior_delays:
        g = prior_delays
    else:
        return min(1.0, max(floor, prior_p)), None
    nd = len(g)
    shows = shows_mature + sum(1 for _, d in young if d is not None)
    expect = n_mature + sum(bisect.bisect_right(g, age) / nd for age, _ in young)
    return min(1.0, max(floor, (shows + k * prior_p) / (expect + k))), g


def cdf(delays, age):
    """G(age): the share of the sorted delay sample at or below age."""
    if not delays:
        return 0.0
    return bisect.bisect_right(delays, age) / len(delays)


def posterior_show(p, g):
    """Probability that a buyer who has not shown after an age where a
    fraction g of shows would already have happened will still show:

        p (1 - g) / (1 - p g)

    p is the prior show rate (p_hat). g = 0 gives p (no evidence yet);
    g = 1 gives 0 (every show would have happened by now)."""
    p = min(1.0, max(0.0, p))
    g = min(1.0, max(0.0, g))
    den = 1.0 - p * g
    if den <= 0.0:
        return 0.0 if g >= 1.0 else 1.0
    return p * (1.0 - g) / den


def naive(buyers, now, ttl, k=DEFAULT_PRIOR_K, floor=DEFAULT_FLOOR):
    """
    The ablation: every buyer who has not shown yet counts as a no-show,
    whatever their age. Same prior and clamp as censored(), so the two differ
    only in how young buyers are counted.
    """
    n = shows = 0
    for _t, delay in buyers:
        n += 1
        if delay is not None:
            shows += 1
    if n == 0:
        return 1.0
    return min(1.0, max(floor, (shows + k) / (n + k)))


ESTIMATORS = {"censored": censored, "naive": naive}
