# Chaos Verification Report

Generated: 2026-08-04 16:41:17 UTC

Black-box run against the live HTTP API at `http://127.0.0.1:8010`. No checkout code, schema, or configuration was modified; stock was prepared through the existing `POST /admin/reset-sale` endpoint and verified with read-only `SELECT`s.

## Configuration

- Concurrent buyers (N): **600** per strategy
- Chaos fraction: **50%** of requests
- Starting stock: **60** units per sale
- Strategies: pessimistic, pessimistic, optimistic, optimistic, redis, redis
- RNG seed: 99 (re-run with the same seed to reproduce)

## The invariant

> **Confirmed orders created during a run must never exceed the stock the sale started with.**

Counted from the `orders` table rather than from this harness's own responses. A client that aborts mid-flight can leave behind a committed order it never saw confirmed, so a client-side tally would under-count exactly the case chaos testing exists to surface. It is also not read from `GET /sales/{id}`: the Redis fast path never decrements `inventory.reserved_stock`, so SQL availability would under-report Redis purchases.

## Summary

| Strategy | Idem | N | Stock | Confirmed (DB) | Oversold | Invariant | Sold out | Aborted | Failed | Dup pairs | Bought twice | Both said OK | Replayed | p50 | p95 | p99 | Wall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `pessimistic` | off | 600 | 60 | **60** | 0 | ✅ HOLDS | 537 | 104 | 0 | 101 | **0** | 0 | 0 | 8317.6ms | 12058.9ms | 12700.0ms | 12.98s |
| `pessimistic` | on | 600 | 60 | **60** | 0 | ✅ HOLDS | 524 | 104 | 0 | 101 | **0** | 13 | 101 | 8309.8ms | 12676.0ms | 13286.5ms | 13.91s |
| `optimistic` | off | 600 | 60 | **60** | 0 | ✅ HOLDS | 499 | 104 | 38 | 101 | **0** | 0 | 0 | 6230.8ms | 10165.0ms | 11138.0ms | 11.54s |
| `optimistic` | on | 600 | 60 | **60** | 0 | ✅ HOLDS | 471 | 104 | 52 | 101 | **0** | 14 | 101 | 8840.2ms | 13682.9ms | 14259.1ms | 14.89s |
| `redis` | off | 600 | 60 | **60** | 0 | ✅ HOLDS | 537 | 104 | 0 | 101 | **0** | 0 | 0 | 6121.0ms | 9488.1ms | 10247.9ms | 11.03s |
| `redis` | on | 600 | 60 | **60** | 0 | ✅ HOLDS | 524 | 104 | 0 | 101 | **0** | 13 | 101 | 11623.9ms | 15789.8ms | 16312.7ms | 17.08s |

**Bought twice** = duplicate pairs that produced two different `order_id`s (the real defect). **Both said OK** = pairs where both responses read `confirmed`, which after idempotency includes replays of the *same* order and is therefore expected to stay high. Read the first column.

## Before / after: Idempotency-Key

Each strategy run twice at seed **99** with identical parameters. The chaos plan and the generated keys are seeded, so both arms send the same requests in the same order -- the only difference is whether a retry carries the `Idempotency-Key` header. Same experiment, one variable.

| Strategy | Dup pairs | **Bought twice BEFORE** | **Bought twice AFTER** | Retries served from cache | Confirmed (DB) before → after | Invariant |
|---|---|---|---|---|---|---|
| `pessimistic` | 101 | **0** | **0** | 101 | 60 → 60 | ✅ |
| `optimistic` | 101 | **0** | **0** | 101 | 60 → 60 | ✅ |
| `redis` | 101 | **0** | **0** | 101 | 60 → 60 | ✅ |

> "Bought twice" counts duplicate pairs that produced **two different `order_id`s** — one intent that really did purchase twice. That is the defect, and it is deliberately not the same as *both responses said "confirmed"*: once idempotency is on, a replayed response is also `confirmed`, because it is the original result being handed back. Both counts appear in the summary table above; only this one tracks the bug.

**No double-confirms in either arm at this configuration.** That is not evidence idempotency worked -- at this contention level the retry loses the race for the last unit and is rejected as sold out regardless. Scarcity, not deduplication, is doing the work. Use the low-contention control below, where stock is not the limiting factor, to see the difference.

The `Replayed` column counts responses the server served from cache (the `Idempotent-Replay: true` header). It is the direct evidence that the mechanism engaged, as opposed to inferring it from an absence.

### Verdict: ✅ **All invariants held.**

Under this load, with chaos injected, no strategy sold more units than it had. Note that this is evidence, not proof: concurrency bugs are probabilistic, and a passing run bounds how easily a defect reproduces rather than showing there is none. Re-run at higher N and higher chaos before treating it as settled.

## Per-strategy detail

### `pessimistic` — sale `5a1e0000-0000-4000-8000-000000000005`
*Lumen 4K Action Camera*

- Requests sent: **701** (600 logical buyers + 101 chaos duplicates)
- Confirmed in database: **60** / 60 stock
- Confirmed seen by clients: 60
- Rejected as sold out: 537
- Aborted mid-flight (chaos): 104
- Failed / errored: 0
- Final availability — reported `60` (SQL `0`, Redis `60`)
  - The two disagree, which is expected here rather than alarming: `pessimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 8317.6ms / 12058.9ms / 12700.0ms
- Wall time: 12.98s (54 req/s)

**Duplicate requests:** 101 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
None of them had both requests confirmed — in this run the duplicates lost the race for remaining stock rather than being deduplicated. That is scarcity doing the work, not idempotency; at higher stock levels both would be expected to succeed.

### `pessimistic` — sale `5a1e0000-0000-4000-8000-000000000005`
*Lumen 4K Action Camera*

- Requests sent: **701** (600 logical buyers + 101 chaos duplicates)
- Confirmed in database: **60** / 60 stock
- Confirmed seen by clients: 73
  - **13 more client(s) were told 'confirmed' than there are orders** — and that is the idempotency layer working, not a discrepancy. A replayed retry legitimately reports the confirmation of the order its first attempt already created, so N confirmations can correspond to fewer than N orders. Cross-check: 101 response(s) were served from cache.
- Rejected as sold out: 524
- Aborted mid-flight (chaos): 104
- Failed / errored: 0
- Final availability — reported `60` (SQL `0`, Redis `60`)
  - The two disagree, which is expected here rather than alarming: `pessimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 8309.8ms / 12676.0ms / 13286.5ms
- Wall time: 13.91s (50 req/s)

**Duplicate requests:** 101 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
13 of those had **both requests confirmed** — the same buyer got two units from one intent.

> Expected for now: the API has no idempotency key, so two independent POSTs are two independent purchases and the server has no way to know they were one intent. This does **not** violate the stock invariant — the total is still capped — but it is the exact behaviour idempotency is meant to remove, recorded here as a before-picture for that work.

### `optimistic` — sale `5a1e0000-0000-4000-8000-000000000004`
*Halo Studio Headphones*

- Requests sent: **701** (600 logical buyers + 101 chaos duplicates)
- Confirmed in database: **60** / 60 stock
- Confirmed seen by clients: 60
- Rejected as sold out: 499
- Aborted mid-flight (chaos): 104
- Failed / errored: 38
- Final availability — reported `60` (SQL `0`, Redis `60`)
  - The two disagree, which is expected here rather than alarming: `optimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 6230.8ms / 10165.0ms / 11138.0ms
- Wall time: 11.54s (61 req/s)

**Duplicate requests:** 101 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
None of them had both requests confirmed — in this run the duplicates lost the race for remaining stock rather than being deduplicated. That is scarcity doing the work, not idempotency; at higher stock levels both would be expected to succeed.

### `optimistic` — sale `5a1e0000-0000-4000-8000-000000000004`
*Halo Studio Headphones*

- Requests sent: **701** (600 logical buyers + 101 chaos duplicates)
- Confirmed in database: **60** / 60 stock
- Confirmed seen by clients: 74
  - **14 more client(s) were told 'confirmed' than there are orders** — and that is the idempotency layer working, not a discrepancy. A replayed retry legitimately reports the confirmation of the order its first attempt already created, so N confirmations can correspond to fewer than N orders. Cross-check: 101 response(s) were served from cache.
- Rejected as sold out: 471
- Aborted mid-flight (chaos): 104
- Failed / errored: 52
- Final availability — reported `60` (SQL `0`, Redis `60`)
  - The two disagree, which is expected here rather than alarming: `optimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 8840.2ms / 13682.9ms / 14259.1ms
- Wall time: 14.89s (47 req/s)

**Duplicate requests:** 101 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
14 of those had **both requests confirmed** — the same buyer got two units from one intent.

> Expected for now: the API has no idempotency key, so two independent POSTs are two independent purchases and the server has no way to know they were one intent. This does **not** violate the stock invariant — the total is still capped — but it is the exact behaviour idempotency is meant to remove, recorded here as a before-picture for that work.

### `redis` — sale `5a1e0000-0000-4000-8000-000000000003`
*Vortex Pro Controller*

- Requests sent: **701** (600 logical buyers + 101 chaos duplicates)
- Confirmed in database: **60** / 60 stock
- Confirmed seen by clients: 60
- Rejected as sold out: 537
- Aborted mid-flight (chaos): 104
- Failed / errored: 0
- Final availability — reported `0` (SQL `60`, Redis `0`)
  - The two disagree, which is expected here rather than alarming: `redis` decrements **Redis** only, so **PostgreSQL** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 6121.0ms / 9488.1ms / 10247.9ms
- Wall time: 11.03s (64 req/s)

**Duplicate requests:** 101 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
None of them had both requests confirmed — in this run the duplicates lost the race for remaining stock rather than being deduplicated. That is scarcity doing the work, not idempotency; at higher stock levels both would be expected to succeed.

### `redis` — sale `5a1e0000-0000-4000-8000-000000000003`
*Vortex Pro Controller*

- Requests sent: **701** (600 logical buyers + 101 chaos duplicates)
- Confirmed in database: **60** / 60 stock
- Confirmed seen by clients: 73
  - **13 more client(s) were told 'confirmed' than there are orders** — and that is the idempotency layer working, not a discrepancy. A replayed retry legitimately reports the confirmation of the order its first attempt already created, so N confirmations can correspond to fewer than N orders. Cross-check: 101 response(s) were served from cache.
- Rejected as sold out: 524
- Aborted mid-flight (chaos): 104
- Failed / errored: 0
- Final availability — reported `0` (SQL `60`, Redis `0`)
  - The two disagree, which is expected here rather than alarming: `redis` decrements **Redis** only, so **PostgreSQL** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 11623.9ms / 15789.8ms / 16312.7ms
- Wall time: 17.08s (41 req/s)

**Duplicate requests:** 101 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
13 of those had **both requests confirmed** — the same buyer got two units from one intent.

> Expected for now: the API has no idempotency key, so two independent POSTs are two independent purchases and the server has no way to know they were one intent. This does **not** violate the stock invariant — the total is still capped — but it is the exact behaviour idempotency is meant to remove, recorded here as a before-picture for that work.

## Reading these numbers

- **Aborted** requests have no latency recorded — they were cancelled before a response arrived, so percentiles cover completed requests only.
- **Failed** includes optimistic-strategy version conflicts, which are the OCC design working (a losing writer is told to retry), not an error.
- A `confirmed (DB)` count *lower* than stock is normal when chaos aborts requests before they reach the server.
- **Duplicates both succeeding is currently expected** and is tracked separately from the stock invariant.

Re-run at other scales:

```bash
python -m scripts.chaos_verification --n 1000 --chaos 0.5
python -m scripts.chaos_verification --strategy optimistic --n 500 --stock 25
```