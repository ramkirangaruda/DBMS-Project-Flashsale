# Horizontal scaling — three API replicas, one database

The FastAPI app now runs as three container replicas behind nginx. PostgreSQL
and Redis are untouched single instances, deliberately: the question is what
happens to a contended row lock when you put three times as many processes in
front of one database.

The prediction was written before any of this was built and is not edited:
[`scaling_prediction.md`](scaling_prediction.md). It is scored below,
including the part it got wrong.

```
buyers -> nginx:8080 -> api1 / api2 / api3 -> postgres (1)  +  redis (1)
```

Data: [`k6_raw_n1000.json`](k6_raw_n1000.json) ·
[`k6_queued_n1000.json`](k6_queued_n1000.json) ·
[`queue_comparison_1replica.json`](queue_comparison_1replica.json) ·
driver in [`load/`](load/)

---

## First, the load generator had to be replaced

The previous phase's conclusions were partly measuring the instrument.
`scripts/chaos_verification.py` is one Python process driving 1000 coroutines,
and at N=600 it reported buyers waiting **25.2s** in the queue while the
server's own histogram said **p50 0.22s, p99 0.50s** — a ~50× gap, with the
harness pinned at ~130% CPU. It was reporting its own scheduling delay as
system latency.

So this phase adds **k6** (Go, real OS-level concurrency) under
[`load/flashsale.js`](load/flashsale.js) purely as a load driver.
`chaos_verification.py` is **not modified** and remains the correctness tool —
it still owns the stock invariant, counted from the `orders` table.

How much difference the driver makes, same deployment, same N=1000:

| | Python harness | k6 |
|---|---|---|
| peak concurrent checkouts achieved | ~115 | **997–1000** |

Everything below is k6-driven. Where a number is compared against the
single-instance phase, that comparison is flagged, because those baselines
were produced by the slower driver and are not like-for-like.

---

## 1. K-sweep at scale — the previous diagnosis was wrong, and it was the instrument

N=300, redis strategy, ample stock. Three replicas each run their own
admission worker, so effective rate is 3× nominal (see "the rate limiter does
not survive replication" below).

| K per 500ms | nominal | effective | peak concurrent (k6) | peak concurrent (Python harness) | wall |
|---|---|---|---|---|---|
| 2 | 4/s | 12/s | **19** | 75 | 25.8s |
| 5 | 10/s | 30/s | **38** | 78 | 11.8s |
| 10 | 20/s | 60/s | **78** | 77 | 9.9s |
| 25 | 50/s | 150/s | **25** | 75 | 9.4s |
| 50 | 100/s | 300/s | **86** | 73 | 9.6s |

**With a load generator that can actually push, concurrency tracks K —
19 → 38 → 78 as the effective rate goes 12 → 30 → 60/s.** That is very close
to linear, and it is exactly what admission control is supposed to do.

This **complicates rather than confirms** the previous phase's thread-pool
diagnosis:

- The old finding — "flat ~31–39 across a 25× range of K" — does not survive a
  competent driver. The plateau was substantially the harness, not the app's
  40-thread pool.
- Above K≈10 the relationship breaks down and the numbers get noisy (25 at
  K=25, 86 at K=50) with wall time flat at ~9.5s. Past that point the queue is
  no longer the binding constraint, so K stops mattering — but for a different
  reason than "the thread pool caps it".
- The thread pool was never disproven as *a* ceiling; it was just never
  reached, because the instrument gave out first.

The honest summary: **admission rate does shape checkout concurrency, in the
regime where admission is the scarcest thing.** The previous phase could not
see that because it could not generate enough load to leave that regime.

---

## 2. The N=1000 comparison at scale

N=1000 buyers, 60 units, three strategies, k6-driven, three replicas.

| | raw herd | through queue |
|---|---|---|
| **pessimistic** | | |
| peak concurrent checkouts | 997 | **109** |
| wall | 46.5s | 31.1s |
| p50 / p95 / p99 | 4820.8 / 6150.6 / 6319.7ms | **52.7 / 826.2 / 942.6ms** |
| OCC conflicts | 0 | 0 |
| refused at door | 0 | 0 |
| confirmed in DB | 60 / 60 | 60 / 60 |
| **optimistic** | | |
| peak concurrent checkouts | 997 | **126** |
| wall | 36.3s | 38.6s |
| p50 / p95 / p99 | 2962.8 / 4563.7 / 4718.3ms | **72.3 / 1513.8 / 1833.4ms** |
| OCC conflicts | **953** | **98** |
| confirmed in DB | **47 / 60** | **60 / 60** |
| **redis** | | |
| peak concurrent checkouts | 1000 | **75** |
| wall | 14.6s | 29.3s |
| p50 / p95 / p99 | 4350.0 / 5106.2 / 5150.6ms | **25.8 / 418.3 / 492.9ms** |
| confirmed in DB | 60 / 60 | 60 / 60 |

**The invariant held in every arm — no oversell anywhere.**

### The most interesting number here is 47

Under a genuine 1000-way herd, the raw optimistic arm **sold only 47 of its 60
units**. 953 version conflicts, and the losers retried into a stampede they
kept losing, so thirteen units simply never sold. With the queue in front, the
same load sold all 60.

That is a failure mode the invariant check cannot catch: the stock guarantee
was never violated, nothing errored, and the run "passed" — it just quietly
under-sold by 22%. It only became visible once the load generator was strong
enough to produce real contention. Under-selling is a business loss that looks
like success from the correctness harness's point of view.

---

## 3. The prediction, scored

### 3a. Pessimistic lock wait — **prediction correct, mechanism correct**

> *Predicted, verbatim:* "queued-arm p95 rises from 93.7ms to somewhere in
> 200–400ms (roughly 2–4×)… tripling the API tier does not change [lock hold
> time] at all, and it makes [queue depth] worse."

To test this without the driver confound, I ran the same k6 load against **one
replica** and **three replicas** — one variable, same instrument:

| pessimistic, queued, k6 | 1 replica | 3 replicas |
|---|---|---|
| lock wait p50 | 8.37ms | 2.08 / 4.07ms |
| **lock wait p95** | **41.79ms** | **229.7 / 208.18ms** |
| lock wait p99 | 65.3ms | 265.7 / 512.5ms |
| lock wait max | 85.19ms | 279.7 / 664.5ms |
| peak concurrent checkouts | 20 | 109 / 92 |
| Postgres sessions waiting on lock | 0 | **20** |

**Scaling the API tier made pessimistic lock-wait p95 roughly 5× worse**
(41.8ms → ~208–230ms, two independent samples). Against the stated 93.7ms
baseline the result lands inside the predicted 200–400ms band; against the
clean same-driver control the degradation is larger than the predicted 2–4×.

The mechanism is exactly as predicted, and the numbers line up almost too
neatly: concurrent checkouts went 20 → ~100 (5×) and lock wait p95 went 41.8 →
~220ms (5×). More clients on a strictly serial resource means a proportionally
longer line, nothing more.

**One nuance I did not predict:** the *median* improved (8.37ms → 2–4ms) while
the tail got much worse. With three replicas, most requests find the lock free
and are served faster; the ones that collide queue behind a much deeper line.
Scaling made the common case better and the bad case worse.

### 3b. Postgres connection exhaustion — **WRONG**

> *Predicted:* "up to **90** [connections], plus… PostgreSQL's default
> `max_connections` is **100**. I predict we come close enough to that ceiling
> to matter, and there is a real chance of seeing `FATAL: sorry, too many
> clients already`."

**Measured peak: 51 connections of 100.** Never close. No `FATAL`, not once,
in any arm.

The reasoning was wrong in a specific and instructive way. I sized the risk
from the pool's *maximum* (3 × `pool_size=10 + max_overflow=20` = 90) rather
than from actual demand. SQLAlchemy pools are lazy — they open connections
only when concurrent checkouts need them, and even at 997 concurrent requests
only ~16–21 were ever *inside* PostgreSQL at once, because the rest were
queued on the row lock or in the app. **Connections track concurrent database
work, not concurrent HTTP requests**, and the row lock kept the former far
below the latter.

The right way to have predicted this was to reason about how many transactions
can be simultaneously active given a serialized lock, not to multiply a
configured ceiling by three. Configured capacity is not demand.

### 3c. K-sweep plateau — **partly wrong**

> *Predicted:* "the ceiling should rise to roughly **90–120**… I predict
> concurrency still will not track K."

The plateau claim was wrong in both directions: measured concurrency tops out
around 78–86 rather than 90–120, and more importantly it *does* track K in the
low-K regime. I predicted the right shape for the wrong system — I was
reasoning about a ceiling that the instrument never let us reach.

### 3d. /metrics scrape gap — **correct**

> *Predicted:* "the hole shrinks or disappears… I do not expect it to be a
> clean win."

Measured scrape success ratio over each arm:

| replica | raw arm | queued arm |
|---|---|---|
| api1 | 90.5% (14 failed samples) | 96.2% (6) |
| api2 | 91.1% (14) | 96.8% (5) |
| api3 | 91.1% (14) | 96.2% (6) |

**The gap persists at scale.** It shrinks under the queue (≈9% of scrapes lost
→ ≈3.5%) but never resolves. Roughly 28 seconds of a 300-second raw window
have no data on any replica, and it is the same 28 seconds you would most want
to look at.

It is also now *harder* to read, exactly as predicted: three series with
independent holes instead of one, so an apparent dip can be a real dip or a
missing scrape, and telling them apart requires checking `up` separately. The
client-side sweep-line measurement remains the number to trust for peak
concurrency.

---

## 4. What the bottleneck actually is now

Measured during the runs (`ResourceSampler` in `load/run_k6.py`), not inferred:

| | 1 replica | 3 replicas, raw | 3 replicas, queued |
|---|---|---|---|
| API CPU | api1 **96.1%** | 164 / 101 / 106% | 104 / 105 / 103% |
| nginx CPU | — | 144.1% | 13.0% |
| Postgres connections | 12 | 44 | 51 |
| Postgres sessions **active** | 1 | 16 | 21 |
| Postgres sessions **waiting on lock** | 0 | **15** | **20** |

**At one replica, the API process is the bottleneck** — pinned at 96% of a
single core, which is one Python process's GIL ceiling. Postgres is idle
behind it (12 connections, 1 active, nothing waiting).

**At three replicas, the bottleneck moves into PostgreSQL's lock manager.**
15–20 sessions sitting in `wait_event_type = 'Lock'` is the row lock doing
exactly what it must: serializing. That is the resource that cannot be scaled
by adding API replicas, and it is now the thing in the way.

Ranking the candidates from the prediction:

1. **PostgreSQL's single row lock — yes, this is it** for the pessimistic
   strategy. Not the connection limit (51/100), not CPU (Postgres was never
   the CPU constraint) — the lock itself.
2. **The API tier — no longer binding but not far off.** Three replicas at
   ~100–164% CPU on an 8-core host is ~4 of 8 cores. A fourth replica would
   help the non-blocking strategies and do nothing for pessimistic.
3. **nginx — not a bottleneck**, but not free either: 144% CPU on the raw
   pessimistic arm, where it holds ~1000 open upstream connections. Under the
   queue it drops to 13%.
4. **The load generator — no longer the limit, but still single-machine.** k6
   reached 997–1000 concurrent, close enough to the 1000 VUs requested that it
   is clearly not throttling. It is not proven to have headroom beyond this,
   so I would not claim a number past ~1000 from this setup.

**Is it still instrument-bound? No — but only just.** k6 delivered essentially
all 1000 requested concurrent checkouts, and the system's response (lock waits,
CPU, connection counts) moved in ways that scale with real contention. To push
meaningfully past this I would need distributed load generation, and I would
expect the row lock to keep absorbing it while everything else got worse.

---

## 5. The rate limiter does not survive replication

Worth stating on its own, because it is a defect and it is silent.

`app/queue.py` runs the admission worker as a **per-process thread**. Three
replicas run three of them, each independently popping K buyers every
interval, so **the effective admission rate is 3× the configured one** — the
`/queue/config` endpoint reports 20/s while the fleet actually admits 60/s.

Nothing warns about this. A rate limiter that silently triples when you add
replicas is the kind of thing that is discovered during an incident. Left
as-is here, because fixing it means changing `queue.py`'s internals, which
this phase is not permitted to touch — and because it is the honest
out-of-the-box behaviour of scaling this design. The fix is to run the
admission worker as a singleton service rather than in every API process,
which is what the previous phase's notes already recommended for a different
reason.

---

## 6. Files touched

**New:**

| file | purpose |
|---|---|
| `Dockerfile` | API image for the replicas |
| `.dockerignore` | keeps venv/results out of the build context |
| `monitoring/nginx.conf` | load balancer, `least_conn`, no sticky sessions |
| `results/load/flashsale.js` | k6 load driver |
| `results/load/run_k6.py` | orchestrator: stock reset, user ids, order counting, CPU/pg sampling, sweep-line concurrency |
| `results/load/users.json` | exported user ids (k6 cannot query PostgreSQL) |
| `results/k6_raw_n1000.json`, `k6_queued_n1000.json`, `k6_queued_n300.json` | run data |
| `results/queue_comparison_1replica.json` | the 1-replica control |
| `results/scaling_prediction.md`, `results/horizontal_scaling_notes.md` | prediction and this file |

**Modified:**

| file | change |
|---|---|
| `docker-compose.yml` | **appended** `api1`/`api2`/`api3`/`nginx` — 85 added lines, **0 removed** |
| `monitoring/prometheus.yml` | appended a `flashsale-api-replicas` scrape job (each replica scraped separately — counters are per-process) |
| `scripts/queue_comparison.py` | multi-replica `/metrics` merge, `scrape_health`, implausible-span filter |

### Confirmations

- **`postgres` and `redis` service definitions are BYTE-IDENTICAL** to the
  pre-scaling commit, verified by extracting and comparing the blocks. The
  whole compose diff is 85 added / **0 removed** lines. No service was
  removed; four were added.
- **No application logic was modified.** `app/main.py`, `app/queue.py`,
  `app/admission.py`, `app/idempotency.py`, `app/metrics.py`, `app/tracing.py`,
  `app/database.py`, `app/models.py` — all unchanged in this phase.
- **`scripts/chaos_verification.py` is unchanged**, as required. It stays the
  correctness tool; k6 only generates load.
- Every checkout, queue and admission code path is byte-for-byte what it was.
  The only application-visible difference is environment: `POSTGRES_HOST`,
  `REDIS_HOST` and the OTLP endpoint point at compose service names.

### One measurement bug fixed along the way

`queue_comparison.py`'s lock-wait collector had no sanity filter. Jaeger stores
span duration unsigned, so a clock-skewed span arrived as ~2^64 microseconds
and turned `max` into 1.8e16 ms and the mean into 3.7e13 ms. Percentiles
survived (order statistics), but the mean was silently garbage. Now filtered
with the discard count reported: 2 of 1001 spans in the raw arm, 3 of 761 in
the queued arm.

---

## Reproducing

```bash
docker compose build api1
docker compose up -d                      # postgres, redis, api1-3, nginx, obs stack
python -m scripts.seed_storefront --drop-in 0

# raw arm (pre-queue front door)
ADMISSION_ENFORCED=false docker compose up -d api1 api2 api3
python results/load/run_k6.py --arm raw --n 1000 --stock 60

# queued arm
docker compose up -d api1 api2 api3
python results/load/run_k6.py --arm queued --n 1000 --stock 60
python results/load/run_k6.py --report

# lock waits, scrape health, per-replica metric merge
export API_METRICS_URLS=http://127.0.0.1:8011,http://127.0.0.1:8012,http://127.0.0.1:8013
export PROM_SELECTOR='{job="flashsale-api-replicas"}'
python -m scripts.queue_comparison --label queued --lookback 200
```

Storefront on <http://localhost:8080>, replicas directly on 8011–8013.
