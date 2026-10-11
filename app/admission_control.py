"""
Self-tuning admission for the waiting room -- TCP congestion control, applied
to a database.

THE PROBLEM THIS SOLVES
    app/queue.py releases K buyers every 500 ms and K is a constant that a
    human picks (QUEUE_ADMIT_BATCH). results/queue_notes.md and
    results/horizontal_scaling_notes.md show why that is fragile: the right
    amount of work to let into the database at once is the point where it is
    saturated but not yet queueing, and that point MOVES with the strategy (a
    row lock serialises, an OCC retry storm does not), the hardware, the
    number of API replicas and the size of the critical section. Pick K too
    low and the sale drags on with an idle database; pick it too high and the
    waiting room stops protecting anything -- latency explodes, the
    connection pool starves, and (measured in this repo) the optimistic
    strategy under-sells. Finding K was done by hand, by sweeping it.

    A fixed RATE is also the wrong kind of knob. It is open loop: it keeps
    admitting at K per tick whether or not the database is keeping up, so when
    the database slows down the work in progress just grows. Measured here
    with a stampede-sized K, in-flight checkouts reached the whole herd.

THE IDEA
    Make admission a CLOSED loop, exactly as TCP does:

        window (cwnd)      the number of admitted buyers that may be
                           outstanding at once -- granted a token but not yet
                           checking out, plus checkouts in flight
        ACK clock          every time a checkout finishes, a slot frees and
                           the next buyer is admitted. Work in progress can
                           never exceed the window, so a slow database
                           automatically slows admission (this is what makes
                           the controller immune to delayed feedback)
        slow start         double the window each step until the first
                           congestion signal
        congestion         the window's p90 checkout latency exceeds
        avoidance          `tolerance` x the best p90 seen so far (a Vegas-
                           style baseline that needs no per-machine tuning)
                           or too many 5xx / OCC version conflicts occur
        AIMD               healthy: window += max(1, 5%).  Congested:
                           window *= 0.5 and remember it as ssthresh

    The only tuned quantities are a latency target range (default: 3x the
    unloaded latency, clamped to 100..500 ms), which says what "queueing"
    means; the window itself -- the number that used to be swept by hand --
    is found live.

WHAT IS NOVEL HERE, AND WHAT IS NOT
    AIMD, ACK-clocking and delay-based windows are old (TCP Reno, TCP Vegas,
    Netflix's concurrency-limits). The claim is narrower: this system's
    decisive knob was a hand-set open-loop admission rate; turning it into a
    closed-loop, database-latency-driven window finds the operating point
    without a sweep. docs/aimd_admission.md reports what that buys and, just
    as importantly, where it does not beat a well-chosen constant.

HOW IT IS WIRED (all additive; default is OFF)
    ADMISSION_CONTROL=aimd   turns it on. Anything else keeps the static K.

    app/admission.py   records one observation per admitted checkout
                       (latency + outcome) into per-second Redis hashes,
                       keeps a global in-flight counter, and releases a
                       grant's slot from the outstanding set when the buyer
                       actually starts checking out.
    app/queue.py       the admission-worker LEADER calls leader_tick() each
                       tick; it returns how many buyers may be admitted now
                       (window - outstanding). Grants are recorded in the
                       outstanding set as they are issued. Every replica calls
                       refresh_cache() so estimated_wait_s uses the live rate.

    State lives in Redis (admctl:state), so a leader failover continues from
    where the last leader stopped instead of restarting slow start.
"""
import asyncio
import json
import math
import os
import time
from dataclasses import asdict, dataclass

from prometheus_client import Counter, Gauge

MODE = os.getenv("ADMISSION_CONTROL", "static").strip().lower()
ENABLED = MODE == "aimd"

STATE_KEY = "admctl:state"
OBS_KEY = "admctl:obs:{sec}"
INFLIGHT_KEY = "admctl:inflight"
OUTSTANDING_KEY = "admctl:out"          # ZSET token -> grant expiry (epoch s)

# 4 buckets per octave -> each bucket spans ~19% in latency, so a quantile
# read from the histogram is within ~19% of the true value. Plenty for
# "is it 3x the baseline", and it keeps one observation to a single HINCRBY.
BUCKETS_PER_OCTAVE = 4
N_BUCKETS = 80          # covers 0 ms .. ~1,000,000 ms


def _envf(name, default):
    return float(os.getenv(name, default))


@dataclass
class Config:
    min_cwnd: float = _envf("AIMD_MIN_CWND", 1)
    max_cwnd: float = _envf("AIMD_MAX_CWND", 256)
    init_cwnd: float = _envf("AIMD_INIT_CWND", 2)
    decrease: float = _envf("AIMD_DECREASE", 0.5)
    increase_frac: float = _envf("AIMD_INCREASE_FRAC", 0.05)   # additive step = max(1, frac*cwnd)
    tolerance: float = _envf("AIMD_TOLERANCE", 3.0)            # x baseline quantile latency
    # The latency the controller defends is  clamp(tolerance x baseline,
    # target_min_ms, target_max_ms): relative to this machine's unloaded
    # latency, but never stricter than a latency nobody notices (min) and
    # never looser than one buyers feel (max). These two numbers are the
    # operator-facing knob that replaced hand-tuning K.
    target_min_ms: float = _envf("AIMD_TARGET_MIN_MS", 100.0)
    target_max_ms: float = _envf("AIMD_TARGET_MAX_MS", 500.0)
    quantile: float = _envf("AIMD_QUANTILE", 0.90)
    loss_threshold: float = _envf("AIMD_LOSS_THRESHOLD", 0.30)
    min_samples: int = int(_envf("AIMD_MIN_SAMPLES", 8))
    step_s: float = _envf("AIMD_STEP_S", 1.0)
    window_s: int = int(_envf("AIMD_WINDOW_S", 2))
    stall_inflight_min: int = int(_envf("AIMD_STALL_INFLIGHT_MIN", 4))
    tick_s: float = _envf("AIMD_TICK_S", 0.1)                   # admission-worker cadence in aimd mode
    startup_gain: float = _envf("AIMD_STARTUP_GAIN", 1.25)      # slow start needs this throughput gain per doubling
    startup_plateau_rounds: int = int(_envf("AIMD_STARTUP_PLATEAU", 3))


CFG = Config()
DEFAULT_STATIC_BATCH = int(os.getenv("QUEUE_ADMIT_BATCH", "10"))
DEFAULT_STATIC_INTERVAL = float(os.getenv("QUEUE_ADMIT_INTERVAL", "0.5"))

# ---------------------------------------------------------------- metrics
CTRL_CWND = Gauge("admission_control_window", "AIMD admission window (max outstanding admitted buyers)")
CTRL_QUANTILE_MS = Gauge("admission_control_window_latency_ms", "Window checkout latency at the controller quantile")
CTRL_BASELINE_MS = Gauge("admission_control_baseline_latency_ms", "Best (baseline) quantile latency seen")
CTRL_OUTSTANDING = Gauge("admission_control_outstanding", "Admitted buyers not yet finished (grants + in-flight)")
CTRL_DECREASES = Counter("admission_control_decreases_total", "Multiplicative decreases", ["signal"])


# ---------------------------------------------------------------- state
@dataclass
class State:
    cwnd: float = CFG.init_cwnd
    ssthresh: float = CFG.max_cwnd
    phase: str = "slow_start"            # slow_start | avoid
    baseline_ms: float = None
    last_step_ts: float = 0.0
    last_decrease_ts: float = 0.0
    steps: int = 0
    decreases: int = 0
    last_signal: str = "init"
    last_q_ms: float = None
    last_n: int = 0
    last_loss: float = 0.0
    throughput: float = 0.0              # completions / s over the last window
    prev_throughput: float = 0.0         # throughput when the window last grew
    plateau: int = 0                     # consecutive slow-start rounds without a throughput gain

    def to_json(self):
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw):
        d = json.loads(raw)
        s = State()
        for k, v in d.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s


# ---------------------------------------------------------------- histogram
def bucket_of(ms):
    return max(0, min(N_BUCKETS - 1, int(BUCKETS_PER_OCTAVE * math.log2(max(0.0, ms) + 1))))


def bucket_upper_ms(idx):
    return 2 ** ((idx + 1) / BUCKETS_PER_OCTAVE) - 1


def quantile_from_hist(hist, q):
    """`hist` maps bucket index -> count. Returns the upper edge (ms) of the
    bucket that holds the q-th observation, or None when empty."""
    total = sum(hist.values())
    if total <= 0:
        return None
    target = max(1, math.ceil(q * total))
    seen = 0
    for idx in sorted(hist):
        seen += hist[idx]
        if seen >= target:
            return bucket_upper_ms(idx)
    return bucket_upper_ms(max(hist))


# ---------------------------------------------------------------- the algorithm
def target_ms(baseline_ms, cfg):
    """The p90 checkout latency above which the window is considered congested."""
    return min(cfg.target_max_ms, max(cfg.target_min_ms, cfg.tolerance * baseline_ms))


def step(st, stats, inflight, queue_depth, cfg, now):
    """
    One control decision. PURE: no Redis, no clock -- everything arrives as an
    argument, which is what makes the behaviour unit-testable.

    stats = {"n": completions in window, "q_ms": window quantile latency,
             "loss": fraction of window that was 5xx / version conflict,
             "secs": seconds of data in the window}
    Returns the (mutated) state.
    """
    n, q_ms, loss = stats["n"], stats["q_ms"], stats["loss"]
    st.last_n, st.last_q_ms, st.last_loss = n, q_ms, round(loss, 4)
    st.throughput = round(n / max(1, stats.get("secs", cfg.window_s)), 2)
    st.last_step_ts = now
    st.steps += 1

    signal = None
    if n >= cfg.min_samples and q_ms is not None:
        # BASELINE = the best latency ever seen (TCP Vegas' baseRTT), learned
        # while the window is small at the start of a drop. It deliberately
        # does NOT drift upward with the observed latency: a baseline that
        # follows the load makes "3x baseline" a moving target, and in a
        # simulation it ratcheted until the controller stopped protecting
        # anything (the window climbed to its cap). It only ever moves up in
        # one case, below: the window is already at its floor and the
        # database is STILL slower than 3x baseline, which means the baseline
        # is stale (the database itself got slower), not that we are overloaded.
        if st.baseline_ms is None or q_ms < st.baseline_ms:
            st.baseline_ms = q_ms
        if q_ms > target_ms(st.baseline_ms, cfg):
            if st.cwnd <= cfg.min_cwnd:
                st.baseline_ms = q_ms
            else:
                signal = "latency"
        elif loss > cfg.loss_threshold:
            signal = "loss"
    else:
        # Too few completions to judge latency. With a window-limited loop
        # that is itself a symptom when the window is full: slots are taken
        # and nothing is finishing, i.e. the database has stalled.
        if inflight >= max(cfg.stall_inflight_min, st.cwnd):
            signal = "stall"

    if signal:
        st.ssthresh = max(cfg.min_cwnd, st.cwnd * cfg.decrease)
        st.cwnd = st.ssthresh
        st.phase = "avoid"
        st.last_decrease_ts = now
        st.decreases += 1
        st.last_signal = signal
        return st

    if n < cfg.min_samples and queue_depth <= 0:
        # Nobody is waiting and nothing was measured: the drop is over (or
        # has not started). Forget everything -- window, ssthresh, baseline --
        # so the next drop calibrates against ITS unloaded database instead
        # of inheriting this one's, and growing now cannot arm a stampede.
        fresh = State()
        fresh.last_step_ts, fresh.steps, fresh.decreases = now, st.steps, st.decreases
        fresh.last_signal = "idle"
        return fresh

    if st.phase == "slow_start":
        # Doubling must keep paying for itself. If the last doubling did not
        # raise throughput by `startup_gain`, the extra window is only adding
        # queue (the BBR "full pipe" test). That matters most at the start of
        # a drop, when thousands of simultaneous /queue/join calls make the
        # server slow for reasons unrelated to checkout: without this test
        # slow start read that noise as "plenty of headroom" and doubled all
        # the way to the cap.
        if st.prev_throughput > 0 and st.throughput < cfg.startup_gain * st.prev_throughput:
            st.plateau += 1
        else:
            st.plateau = 0
        st.prev_throughput = st.throughput
        if st.plateau >= cfg.startup_plateau_rounds:
            st.phase, st.ssthresh = "avoid", st.cwnd
            st.last_signal = "full_pipe"
        else:
            st.cwnd = min(st.cwnd * 2, cfg.max_cwnd)
            if st.cwnd >= st.ssthresh:
                st.phase = "avoid"
            st.last_signal = "slow_start"
    else:
        st.cwnd = min(cfg.max_cwnd, st.cwnd + max(1.0, cfg.increase_frac * st.cwnd))
        st.last_signal = "increase"
    st.cwnd = max(cfg.min_cwnd, st.cwnd)
    return st


def slots(cwnd, outstanding):
    """How many buyers may be admitted right now."""
    return max(0, int(math.floor(cwnd)) - int(outstanding))


# ---------------------------------------------------------------- observations (request path, async)
_pending = set()


async def _record(ar, latency_ms, outcome, now):
    sec = int(now)
    key = OBS_KEY.format(sec=sec)
    pipe = ar.pipeline()
    pipe.hincrby(key, "n", 1)
    if outcome != "ok":
        pipe.hincrby(key, "loss", 1)
    pipe.hincrby(key, f"b{bucket_of(latency_ms)}", 1)
    pipe.expire(key, 60)
    await pipe.execute()


def record(ar, latency_ms, outcome, now=None):
    """Fire-and-forget: the buyer's response must never wait on telemetry."""
    async def run():
        try:
            await _record(ar, latency_ms, outcome, now or time.time())
        except Exception:                                      # noqa: BLE001
            pass
    t = asyncio.ensure_future(run())
    _pending.add(t)
    t.add_done_callback(_pending.discard)


async def enter(ar, token=None):
    """A checkout starts: in-flight +1, and the grant stops being merely
    'outstanding' (it has been used) -- it is now counted as in flight."""
    try:
        pipe = ar.pipeline()
        pipe.incr(INFLIGHT_KEY)
        pipe.expire(INFLIGHT_KEY, 300)
        if token:
            pipe.zrem(OUTSTANDING_KEY, token)
        await pipe.execute()
    except Exception:                                          # noqa: BLE001
        pass


async def leave(ar):
    try:
        v = await ar.decr(INFLIGHT_KEY)
        if v is not None and int(v) < 0:       # self-heal after a restart mid-flight
            await ar.set(INFLIGHT_KEY, 0, ex=300)
    except Exception:                                          # noqa: BLE001
        pass


def classify(status_code, body_status):
    """ok | conflict | error -- what the controller counts as 'loss'."""
    if status_code >= 500 or body_status == "error":
        return "error"
    if status_code == 409 and body_status == "failed":
        return "conflict"                       # OCC version conflict
    return "ok"


# ---------------------------------------------------------------- grants (called from app/queue.py)
def track_grants(pipe, tokens, expires_at):
    """Record freshly issued admission tokens as outstanding. `pipe` is the
    pipeline admit()/admit_async() already built."""
    if ENABLED and tokens:
        pipe.zadd(OUTSTANDING_KEY, {t: expires_at for t in tokens})


# ---------------------------------------------------------------- leader side (sync, worker thread)
def read_window(r, now, window_s, since=0.0, q=None):
    q = CFG.quantile if q is None else q
    first = max(int(now) - window_s + 1, int(since) + 1)
    secs = list(range(first, int(now) + 1))
    if not secs:
        return {"n": 0, "q_ms": None, "loss": 0.0, "secs": 0}
    pipe = r.pipeline()
    for s in secs:
        pipe.hgetall(OBS_KEY.format(sec=s))
    rows = pipe.execute()
    n = loss = 0
    hist = {}
    for row in rows:
        for k, v in (row or {}).items():
            if k == "n":
                n += int(v)
            elif k == "loss":
                loss += int(v)
            elif k.startswith("b"):
                hist[int(k[1:])] = hist.get(int(k[1:]), 0) + int(v)
    return {"n": n, "q_ms": quantile_from_hist(hist, q),
            "loss": (loss / n) if n else 0.0, "secs": len(secs)}


def load_state(r):
    try:
        raw = r.get(STATE_KEY)
        return State.from_json(raw) if raw else State()
    except Exception:                                          # noqa: BLE001
        return State()


def save_state(r, st):
    r.set(STATE_KEY, st.to_json())


_cached = {"cwnd": None, "throughput": 0.0}


def reset_state(r):
    r.delete(STATE_KEY, OUTSTANDING_KEY, INFLIGHT_KEY)
    _cached.update(cwnd=None, throughput=0.0)


def effective_rate():
    """Admissions per second that estimated_wait_s should assume, or None to
    use the static batch/interval."""
    if ENABLED and _cached["throughput"] > 0:
        return _cached["throughput"]
    return None


def _publish(st, outstanding=None):
    _cached["cwnd"] = max(1, int(round(st.cwnd)))
    _cached["throughput"] = st.throughput
    CTRL_CWND.set(_cached["cwnd"])
    if outstanding is not None:
        CTRL_OUTSTANDING.set(outstanding)
    if st.last_q_ms is not None:
        CTRL_QUANTILE_MS.set(st.last_q_ms)
    if st.baseline_ms is not None:
        CTRL_BASELINE_MS.set(st.baseline_ms)


def refresh_cache(r):
    """Every replica, every tick: pick up the leader's latest decision."""
    if not ENABLED:
        return
    _publish(load_state(r))


def outstanding_and_inflight(r, now):
    pipe = r.pipeline()
    pipe.zremrangebyscore(OUTSTANDING_KEY, "-inf", now)       # grants that expired unused
    pipe.zcard(OUTSTANDING_KEY)
    pipe.get(INFLIGHT_KEY)
    _, granted, inflight = pipe.execute()
    return int(granted or 0), max(0, int(inflight or 0))


def leader_tick(r, queue_depth, now=None, cfg=CFG):
    """
    Called once per worker tick by the admission LEADER only. Runs a control
    step when `step_s` has elapsed and returns how many buyers may be admitted
    right now (window minus work already outstanding), or None when control is
    off (the caller keeps the static batch).
    """
    if not ENABLED:
        return None
    now = now or time.time()
    st = load_state(r)
    granted, inflight = outstanding_and_inflight(r, now)
    if now - st.last_step_ts >= cfg.step_s:
        # Observations from before the last decrease describe the old window;
        # judging the new one by them would double-penalise one event.
        stats = read_window(r, now, cfg.window_s, since=st.last_decrease_ts, q=cfg.quantile)
        before = st.decreases
        st = step(st, stats, inflight, queue_depth, cfg, now)
        if st.decreases > before:
            CTRL_DECREASES.labels(signal=st.last_signal).inc()
        save_state(r, st)
    _publish(st, granted + inflight)
    return slots(st.cwnd, granted + inflight)


def snapshot():
    """For /queue/config."""
    return {
        "mode": MODE if MODE in ("aimd", "static") else "static",
        "enabled": ENABLED,
        "window": _cached["cwnd"],
        "observed_throughput_per_s": _cached["throughput"],
        "config": asdict(CFG) if ENABLED else None,
    }
