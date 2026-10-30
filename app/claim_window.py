"""
Futility-aware admission: never admit a buyer who cannot win.

THE PROBLEM THIS SOLVES
    The waiting room releases buyers at a rate (static K) or into a latency-
    driven window (app/admission_control.py). Neither knows how much stock is
    left, so both keep admitting after the stock is spoken for. Measured in
    this repo (results/load/summary_queued_pessimistic.json): 820 admitted
    buyers for 60 units, 760 of whom queued, were admitted, and opened a
    SELECT ... FOR UPDATE only to read "sold out". A generic overload
    controller cannot see this: a sold-out checkout is cheap, so the database
    looks healthy while every admission is doomed.

    The server KNOWS the remaining stock, so it can bound admissions by it:

        unresolved admitted buyers  <=  W = ceil(free_units / p_hat)

    p_hat is the show rate: the fraction of admitted buyers who actually
    check out before their token expires. With p_hat = 1 the window admits
    exactly as many buyers as there are units; as p_hat falls it admits more,
    so the EXPECTED number who show still matches the stock.

MODES (CLAIM_WINDOW)
    off           default: nothing in this module runs
    fixed:<p>     p_hat pinned at p (CW-1 is fixed:1)
    oracle        p_hat = FA_ORACLE_P, the true effective show rate, set by
                  the benchmark (upper-bound arm)
    naive         p_hat = shows / admitted (app/show_estimator.naive)
    censored      the censoring-aware estimate (app/show_estimator.censored)

    The claim window forces the fixed-TTL mode (FA_TTL_MODE=fixed): a token's
    30 s clock starts at first notification and is not renewed by later polls.
    The estimator needs a fixed horizon to know when a cohort is fully
    observed; under renewal a polling buyer could hold a slot forever.

WHAT IS "UNRESOLVED"
    Admitted, and none of: a confirmed order, a sold-out answer, or token
    expiry. An optimistic buyer retrying a version conflict stays unresolved.

KEYS (per sale)
    fa:U:{sale}         ZSET token -> token expiry (epoch s): the unresolved set
    fa:t0:{sale}        HASH token -> clock start (grant, then first notification)
    fa:show:{sale}      HASH token -> first checkout attempt (epoch s)
    fa:stats:{sale}     HASH counters (admitted, confirmed, sold_out, expired_unshown, ...)
    fa:state:{sale}     STR  JSON of the leader's last decision (for /queue/control)
    fa:soldout:{sale}   STR  "1" while free = 0 (every unit reserved)
    queue:notified:{token}  STR  first-notification marker (app/queue.py)
"""
import asyncio
import json
import math
import os
import time

from app import show_estimator, stock_view

RAW_MODE = os.getenv("CLAIM_WINDOW", "off").strip().lower()
ENABLED = RAW_MODE not in ("", "off")
SOLDOUT_NOTIFY = os.getenv("FA_SOLDOUT_NOTIFY", "off").strip().lower() in ("on", "1", "true")
# The claim window always runs with the fixed TTL (decided, plan section 10);
# FA_TTL_MODE=fixed can also be set on its own.
TTL_FIXED = ENABLED or os.getenv("FA_TTL_MODE", "renew").strip().lower() == "fixed"

P_FLOOR = float(os.getenv("FA_P_FLOOR", str(show_estimator.DEFAULT_FLOOR)))
PRIOR_K = float(os.getenv("FA_PRIOR_K", str(show_estimator.DEFAULT_PRIOR_K)))
W_MAX = int(os.getenv("FA_W_MAX", "100000"))
ESTIMATE_EVERY_S = float(os.getenv("FA_ESTIMATE_EVERY_S", "0.1"))   # the pilot's cadence

U_KEY = "fa:U:{sale_id}"
T0_KEY = "fa:t0:{sale_id}"
SHOW_KEY = "fa:show:{sale_id}"
STATS_KEY = "fa:stats:{sale_id}"
STATE_KEY = "fa:state:{sale_id}"
SOLDOUT_KEY = "fa:soldout:{sale_id}"
KEY_TTL_S = 3600


def parse_mode(raw):
    """'fixed:0.5' -> ('fixed', 0.5); 'censored' -> ('censored', None)."""
    raw = (raw or "off").strip().lower()
    if raw.startswith("fixed:"):
        return "fixed", float(raw.split(":", 1)[1])
    if raw in ("off", "", "oracle", "naive", "censored"):
        return (raw or "off"), None
    raise ValueError(f"unknown CLAIM_WINDOW mode {raw!r}")


MODE, FIXED_P = parse_mode(RAW_MODE)


# ---------------------------------------------------------------- pure
def window(free, p_hat, floor=P_FLOOR, w_max=W_MAX):
    """W = ceil(free / p_hat), with p_hat clamped to [floor, 1] and W capped."""
    if free is None or free <= 0:
        return 0
    p = min(1.0, max(floor, p_hat))
    return min(w_max, math.ceil(free / p - 1e-9))


def budget(w, unresolved):
    """How many more buyers may be admitted right now."""
    return max(0, int(w) - int(unresolved))


def is_sold_out(free):
    """Terminal for the queue: every unit is reserved. An admitted buyer who
    has not resolved yet cannot change that -- they can only lose -- so this
    does not wait for them. Only a declined payment returns a unit; the stock
    refresh then shows free > 0 and the leader clears the flag again."""
    return free is not None and free <= 0


def estimate(mode, buyers, now, ttl, fixed_p=None, oracle_p=None, k=PRIOR_K, floor=P_FLOOR):
    """p_hat for the configured mode. Pure."""
    if mode == "fixed":
        return min(1.0, max(floor, fixed_p))
    if mode == "oracle":
        return min(1.0, max(floor, oracle_p if oracle_p is not None else 1.0))
    return show_estimator.ESTIMATORS[mode](buyers, now, ttl, k=k, floor=floor)


def buyers_from_hashes(t0, shows):
    """Turn the two Redis hashes into the (clock_start, delay) pairs the
    estimator takes."""
    out = []
    for token, start in t0.items():
        start = float(start)
        shown = shows.get(token)
        out.append((start, None if shown is None else max(0.0, float(shown) - start)))
    return out


# ---------------------------------------------------------------- grant side (app/queue.py)
def track_grants(pipe, sale_id, tokens, now, ttl):
    """Freshly issued tokens: unresolved until they resolve or expire."""
    if not ENABLED or not tokens:
        return
    u, t0 = U_KEY.format(sale_id=sale_id), T0_KEY.format(sale_id=sale_id)
    pipe.zadd(u, {t: now + ttl for t in tokens})
    pipe.hset(t0, mapping={t: now for t in tokens})
    pipe.hincrby(STATS_KEY.format(sale_id=sale_id), "admitted", len(tokens))
    for k in (u, t0, STATS_KEY.format(sale_id=sale_id)):
        pipe.expire(k, KEY_TTL_S)


def note_notified(pipe, sale_id, token, now, ttl):
    """The buyer has just been told. Their clock restarts here (fixed TTL)."""
    if not ENABLED:
        return
    pipe.zadd(U_KEY.format(sale_id=sale_id), {token: now + ttl}, xx=True)
    pipe.hset(T0_KEY.format(sale_id=sale_id), token, now)


# ---------------------------------------------------------------- door side (app/admission.py, async)
_pending = set()


def _bg(coro):
    """Fire-and-forget, like admission_control.record(): a buyer's response
    never waits on bookkeeping."""
    async def run():
        try:
            await coro
        except Exception:                                      # noqa: BLE001, S110
            pass
    t = asyncio.ensure_future(run())
    _pending.add(t)
    t.add_done_callback(_pending.discard)


async def _on_show(ar, sale_id, token, now):
    await ar.hsetnx(SHOW_KEY.format(sale_id=sale_id), token, now)
    await ar.expire(SHOW_KEY.format(sale_id=sale_id), KEY_TTL_S)


def on_show(ar, sale_id, token, now=None):
    """A buyer's checkout request reached the door with a live token. Only
    the first one per token counts as the show."""
    if ENABLED and ar is not None and sale_id and token:
        _bg(_on_show(ar, sale_id, token, now or time.time()))


def outcome_kind(status_code, body_status):
    """confirmed | sold_out | pending. A version conflict or an error leaves
    the claim unresolved: the buyer may retry inside the window."""
    if body_status == "confirmed":
        return "confirmed"
    if body_status == "sold_out":
        return "sold_out"
    return "pending"


async def _on_outcome(ar, sale_id, token, kind):
    # Resolve each token once: only the request that actually removes it
    # from the unresolved set counts it and, if confirmed, takes a unit off
    # the free mirror (a replayed confirmation must not count twice).
    if not token or not await ar.zrem(U_KEY.format(sale_id=sale_id), token):
        return
    pipe = ar.pipeline()
    pipe.hincrby(STATS_KEY.format(sale_id=sale_id), kind, 1)
    if kind == "confirmed":
        pipe.decr(stock_view.FREE_KEY.format(sale_id=sale_id))
    res = await pipe.execute()
    if kind == "confirmed" and res[-1] is not None and int(res[-1]) < 0:
        await ar.set(stock_view.FREE_KEY.format(sale_id=sale_id), 0, ex=KEY_TTL_S)


def on_outcome(ar, sale_id, token, status_code, body_status):
    if not (ENABLED and ar is not None and sale_id):
        return
    kind = outcome_kind(status_code, body_status)
    if kind != "pending":
        _bg(_on_outcome(ar, sale_id, token, kind))


# ---------------------------------------------------------------- leader side (sync, admission worker)
_last = {}          # sale_id -> (computed_at, p_hat)


def oracle_p():
    raw = os.getenv("FA_ORACLE_P")
    return float(raw) if raw else None


def leader_tick(r, sale_id, ttl, now=None):
    """Called by the admission LEADER once per tick per sale. Returns how many
    buyers may be admitted now, or None when the claim window is off."""
    if not ENABLED:
        return None
    now = now or time.time()
    u_key = U_KEY.format(sale_id=sale_id)
    pipe = r.pipeline()
    pipe.zrangebyscore(u_key, "-inf", now)
    pipe.zremrangebyscore(u_key, "-inf", now)
    pipe.zcard(u_key)
    expired, _, unresolved = pipe.execute()
    if expired:
        shows = r.hmget(SHOW_KEY.format(sale_id=sale_id), expired)
        n_unshown = sum(1 for s in shows if s is None)
        pipe = r.pipeline()
        pipe.hincrby(STATS_KEY.format(sale_id=sale_id), "expired_unshown", n_unshown)
        pipe.hincrby(STATS_KEY.format(sale_id=sale_id), "expired_shown", len(expired) - n_unshown)
        pipe.execute()

    free = stock_view.read_free(r, sale_id)

    last_t, p_hat = _last.get(sale_id, (-1e18, 1.0))
    if now - last_t >= ESTIMATE_EVERY_S:
        buyers = []
        if MODE in ("naive", "censored"):
            buyers = buyers_from_hashes(r.hgetall(T0_KEY.format(sale_id=sale_id)),
                                        r.hgetall(SHOW_KEY.format(sale_id=sale_id)))
        p_hat = estimate(MODE, buyers, now, ttl, fixed_p=FIXED_P, oracle_p=oracle_p())
        _last[sale_id] = (now, p_hat)

    if free is None:
        # The stock view has not run yet. Admit nobody rather than guess:
        # it refreshes every FA_STOCK_REFRESH_S, so this lasts one refresh.
        w, slots = 0, 0
    else:
        w = window(free, p_hat)
        slots = budget(w, unresolved)

    sold_out = is_sold_out(free)
    pipe = r.pipeline()
    if sold_out:
        pipe.set(SOLDOUT_KEY.format(sale_id=sale_id), "1", ex=KEY_TTL_S)
    else:
        pipe.delete(SOLDOUT_KEY.format(sale_id=sale_id))
    pipe.set(STATE_KEY.format(sale_id=sale_id), json.dumps({
        "t": round(now, 3), "mode": MODE, "p_hat": round(p_hat, 4), "free": free,
        "window": w, "unresolved": unresolved, "budget": slots, "sold_out": sold_out,
    }), ex=KEY_TTL_S)
    pipe.execute()
    return slots


def snapshot(r, sale_id):
    """For /queue/control: the leader's last decision and the counters."""
    if not ENABLED:
        return None
    raw = r.get(STATE_KEY.format(sale_id=sale_id))
    stats = r.hgetall(STATS_KEY.format(sale_id=sale_id)) or {}
    return {"state": json.loads(raw) if raw else None,
            "stats": {k: int(v) for k, v in stats.items()}}


def active_sales(r):
    """Sales with a claim-window state: still worth ticking after their
    queue has drained."""
    prefix = STATE_KEY.format(sale_id="")
    return [k[len(prefix):] for k in r.scan_iter(match=f"{prefix}*", count=500)]


def reset(r):
    for pat in ("fa:*", "queue:notified:*"):
        for k in r.scan_iter(match=pat, count=1000):
            r.delete(k)
    _last.clear()
