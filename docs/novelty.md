# The one contribution: bound the concurrency, don't just pick a smarter lock

## Claim

In a flash sale, the most effective contention control is **limiting how many
buyers reach checkout at once** (a virtual waiting room that releases K buyers
at a time), not choosing between locking schemes. Measured here, the waiting
room beats every fixed strategy on its own terms, and it exposes a failure
that correctness tests cannot see.

This is not a claim that waiting rooms are new. The contribution is the
controlled measurement of *what bounding concurrency does to each
database-level strategy*, using the same load, the same stock and the same
invariant check for all of them.

## Evidence (all from real runs, see `results/`)

**Single API instance, N = 1,000 buyers, 60 units, seed 2024** (`queue_notes.md`):

| | no queue | through queue |
|---|---|---|
| Peak concurrent checkouts | 1,014 | 60 to 199 |
| `SELECT ... FOR UPDATE` wait p95 | 1,218.9 ms | 93.7 ms (-92%) |
| OCC version conflicts | 69 | 11 (-84%) |
| Optimistic p50 latency | 4,116.7 ms | 45.7 ms (-99%) |
| Confirmed orders vs stock | 60 / 60 | 60 / 60 |

**Three API replicas, k6 load, N = 1,000** (`horizontal_scaling_notes.md`):

| | no queue | through queue |
|---|---|---|
| Optimistic: units sold | **47 / 60** | **60 / 60** |
| Optimistic: version conflicts | 953 | 98 |
| Pessimistic p50 | 4,820.8 ms | 52.7 ms |

The invariant (confirmed orders never exceed stock) held in every arm. The
no-queue optimistic run still **under-sold by 22%** with no error, so a
correctness check alone would have called it a pass.

**Adding servers does not fix a contended row.** Tripling the API tier raised
pessimistic lock-wait p95 from 41.8 ms to about 208 to 230 ms, roughly 5x,
because the queue relocates into the database's lock manager. This was
predicted in writing before the run (`scaling_prediction.md`) and confirmed.

## How it works

- A Redis sorted set holds buyers; `POST /queue/join` returns a position.
- A background worker admits K buyers every 500 ms and issues a 30-second
  admission token; `/checkout/*` refuses requests without one.
- The gate sits **outside** the idempotency cache, so a rejected buyer's 403
  is never cached for 24 hours under their key.
- With several replicas, one worker holds a Redis lease so the admit rate is
  K, not K times the replica count (a bug found and fixed during scaling).

## An alternative that was tested and rejected

A natural idea is an **adaptive switch**: use optimistic concurrency control
while load is low, and pessimistic locking when contended. I implemented it
(with an in-flight counter and escalation on a version conflict) and
benchmarked it over HTTP against both fixed strategies at 10, 40, 100 and 200
concurrent buyers for 30 units, median of three trials.

It did **not** win. Pessimistic locking was fastest or tied at every level
(for example at 200 buyers p50 was about 1,217 ms for pessimistic, 1,385 ms
for optimistic and 1,457 ms for adaptive), because the critical section is
tiny and the HTTP and Python overhead dominates. The feature was removed. The
result supports the claim above: the gain comes from bounding concurrency in
front of the database, not from cleverness inside it.

## Limits (say these before you are asked)

- One machine; results vary run to run.
- The waiting room is evaluated with a synthetic buyer herd, not real traffic.
- The 47 / 60 under-sell was observed at N = 1,000 under k6; the smaller HTTP
  benchmark above could not reproduce it.

## Reproduce

```bash
ADMISSION_ENFORCED=false python -m app.main                      # raw arm
python -m scripts.chaos_verification --n 1000 --chaos 0.3 --stock 60 --seed 2024 --no-queue
python -m scripts.queue_comparison --label raw

python -m app.main                                               # queued arm
python -m scripts.chaos_verification --n 1000 --chaos 0.3 --stock 60 --seed 2024
python -m scripts.queue_comparison --label queued
python -m scripts.queue_comparison --report
```
