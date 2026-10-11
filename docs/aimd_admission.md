# Adaptive (AIMD) admission control for the waiting room

## What it is
The waiting room (`app/queue.py`) releases buyers to checkout in fixed batches
(`QUEUE_ADMIT_BATCH` every `QUEUE_ADMIT_INTERVAL`). The right batch size depends
on the database, the locking strategy and the hardware, and nobody knows it
before the sale. A value that is too high collapses checkout (lock convoys,
OCC retry storms); one that is too low sells slowly.

`ADMISSION_CONTROL=aimd` replaces the hand-picked number with a closed-loop
controller borrowed from TCP congestion control (`app/admission_control.py`):

| TCP idea | Here |
|---|---|
| congestion window (cwnd) | max admitted-but-unfinished checkouts |
| ACK clock | a slot is freed only when a checkout finishes, not on a timer |
| slow start | cwnd doubles each step until throughput stops growing ("full pipe") |
| additive increase / multiplicative decrease | +max(1, 5% cwnd) when healthy, x0.5 on congestion |
| Vegas baseline RTT | unloaded checkout latency is learned; target = clamp(3 x baseline, 100, 500 ms) |
| loss / timeout | 5xx, timeouts, stalled in-flight work count as congestion signals |

Latencies go into log-bucket histograms in Redis, so every API instance feeds
one controller; the admission-worker leader runs it. Default is `static`, so
nothing changes unless the flag is set. The database schema and the three
checkout handlers are not touched.

## Results (median of 3 runs, single machine, `results/aimd_*.json|log`)
Columns: sell-out time (s) / server p95 (ms) / peak concurrent checkouts.

**Pessimistic, 1,500 units, 1,724 buyers**

| arm | sell-out s | server p95 ms | peak conc. |
|---|---|---|---|
| static 10 | 74.8 | 85.6 | 10 |
| static 20 | 37.4 | 206 | 20 |
| static 40 (best fixed) | **18.9** | 233 | 79 |
| static 80 | 23.4 | 6,473 | 1,330 |
| static 160 | 22.9 | 4,807 | 1,010 |
| **aimd** | 19.4 | **92.6** | 32 |

**Pessimistic, 800 units, +10 ms per inventory UPDATE (slow database)**

| arm | sell-out s | server p95 ms | peak conc. |
|---|---|---|---|
| static 20 | 20.1 | 369 | 28 |
| static 40 (best fixed) | **11.7** | 2,420 | 166 |
| static 80 | 15.0 | 4,658 | 594 |
| **aimd** | 14.2 | **212** | 16 |

**Optimistic, 600 units, 690 buyers**

| arm | sold % | sell-out s | server p95 ms | OCC retries |
|---|---|---|---|---|
| static 10 | 100 | 29.9 | 48.5 | 882 |
| static 20 (best fixed) | 100 | **15.1** | 168 | 748 |
| static 40 | 100 | 28.4 | 1,859 | 1,013 |
| static 80 | 57 | 34.3 | 2,402 | 1,617 |
| static 160 | 47 | 35.2 | 2,493 | 2,019 |
| **aimd** | 100 | 33.3 | **23.7** | **291** |

## What this does and does not show
* **Claim that holds:** AIMD finds a safe operating point with no tuning. It
  never oversold and never collapsed, and its tail latency is 3x to 10x lower
  than the best static setting's, at a fraction of the concurrency.
* **Claim that does NOT hold:** it is not faster than the best hand-tuned
  static value. On pessimistic it ties (19.4 s vs 18.9 s) or loses by about
  20% on the slow database (14.2 s vs 11.7 s). On optimistic it is about 2x
  slower than the best static value (33.3 s vs 15.1 s): it is conservative
  and settled near the low end, trading throughput for the lowest retry count.
* The benefit is against *mis-tuned* static values: K=80/160 on optimistic
  sold only 47-57% of stock in the window, which AIMD avoids.
* One machine, one run of 3 repeats per arm, synthetic herd pre-placed in
  the queue. Not a multi-node or production result.
* The optimistic result suggests the latency target is too tight for that
  strategy; tuning `AIMD_TARGET_MIN_MS` / `AIMD_TOLERANCE` is the obvious
  follow-up and was not done, to avoid fitting to the benchmark.

## Reproduce
```
python -m scripts.admission_control_benchmark --strategy pessimistic --stock 1500 \
  --arms static:10,static:20,static:40,static:80,static:160,aimd --repeats 3 \
  --out results/aimd_pessimistic_native.json
python -m scripts.admission_control_benchmark --strategy pessimistic --stock 800 --db-delay-ms 10 \
  --arms static:5,static:10,static:20,static:40,static:80,aimd --repeats 3 --out results/aimd_pessimistic_slowdb.json
python -m scripts.admission_control_benchmark --strategy optimistic --stock 600 \
  --arms static:10,static:20,static:40,static:80,static:160,aimd --repeats 3 --out results/aimd_optimistic_native.json
```
Live state: `GET /queue/control`; reset: `POST /queue/control/reset`.
