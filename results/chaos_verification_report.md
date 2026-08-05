# Chaos Verification Report

Generated: 2026-08-04 16:43:12 UTC

Black-box run against the live HTTP API at `http://127.0.0.1:8010`. No checkout code, schema, or configuration was modified; stock was prepared through the existing `POST /admin/reset-sale` endpoint and verified with read-only `SELECT`s.

## Configuration

- Concurrent buyers (N): **40** per strategy
- Chaos fraction: **100%** of requests
- Starting stock: **500** units per sale
- Strategies: pessimistic, pessimistic, optimistic, optimistic, redis, redis
- RNG seed: 7 (re-run with the same seed to reproduce)

## The invariant

> **Confirmed orders created during a run must never exceed the stock the sale started with.**

Counted from the `orders` table rather than from this harness's own responses. A client that aborts mid-flight can leave behind a committed order it never saw confirmed, so a client-side tally would under-count exactly the case chaos testing exists to surface. It is also not read from `GET /sales/{id}`: the Redis fast path never decrements `inventory.reserved_stock`, so SQL availability would under-report Redis purchases.

## Summary

| Strategy | Idem | N | Stock | Confirmed (DB) | Oversold | Invariant | Sold out | Aborted | Failed | Dup pairs | Bought twice | Both said OK | Replayed | p50 | p95 | p99 | Wall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `pessimistic` | off | 40 | 500 | **38** | 0 | ✅ HOLDS | 0 | 16 | 0 | 14 | **14** | 14 | 0 | 1014.4ms | 1284.6ms | 1323.3ms | 1.35s |
| `pessimistic` | on | 40 | 500 | **24** | 0 | ✅ HOLDS | 0 | 16 | 0 | 14 | **0** | 14 | 14 | 571.0ms | 681.2ms | 698.9ms | 0.72s |
| `optimistic` | off | 40 | 500 | **11** | 0 | ✅ HOLDS | 0 | 16 | 27 | 14 | **1** | 1 | 0 | 357.6ms | 704.1ms | 710.0ms | 0.77s |
| `optimistic` | on | 40 | 500 | **8** | 0 | ✅ HOLDS | 0 | 16 | 26 | 14 | **0** | 4 | 14 | 943.7ms | 1095.6ms | 1118.3ms | 1.14s |
| `redis` | off | 40 | 500 | **38** | 0 | ✅ HOLDS | 0 | 16 | 0 | 14 | **14** | 14 | 0 | 426.6ms | 759.5ms | 762.9ms | 0.88s |
| `redis` | on | 40 | 500 | **24** | 0 | ✅ HOLDS | 0 | 16 | 0 | 14 | **0** | 14 | 14 | 416.2ms | 534.3ms | 550.6ms | 0.57s |

**Bought twice** = duplicate pairs that produced two different `order_id`s (the real defect). **Both said OK** = pairs where both responses read `confirmed`, which after idempotency includes replays of the *same* order and is therefore expected to stay high. Read the first column.

## Before / after: Idempotency-Key

Each strategy run twice at seed **7** with identical parameters. The chaos plan is seeded, so both arms send the same requests in the same order, and both arms share one per-invocation key namespace, so a retry in either arm carries the same key its original would have. The only difference is whether that header is sent at all. Same experiment, one variable.

(Keys are derived from a nonce generated fresh each invocation rather than from the seed. Seeded keys would be reproducible across *runs*, which sounds desirable but means a re-run replays the previous run's 24h cache and creates zero orders -- and with `--strategy all`, every strategy would share one key space and strategies 2 and 3 would replay strategy 1.)

| Strategy | Dup pairs | **Bought twice BEFORE** | **Bought twice AFTER** | Retries served from cache | Confirmed (DB) before → after | Invariant |
|---|---|---|---|---|---|---|
| `pessimistic` | 14 | **14** | **0** | 14 | 38 → 24 | ✅ |
| `optimistic` | 14 | **1** | **0** | 14 | 11 → 8 | ✅ |
| `redis` | 14 | **14** | **0** | 14 | 38 → 24 | ✅ |

> "Bought twice" counts duplicate pairs that produced **two different `order_id`s** — one intent that really did purchase twice. That is the defect, and it is deliberately not the same as *both responses said "confirmed"*: once idempotency is on, a replayed response is also `confirmed`, because it is the original result being handed back. Both counts appear in the summary table above; only this one tracks the bug.

**`pessimistic`, `optimistic`, `redis`: double-confirms went to zero.** The retry now hits `idempotency:{key}` and the cached response is replayed verbatim -- same `order_id`, same status -- without the checkout handler running a second time.

The `Replayed` column counts responses the server served from cache (the `Idempotent-Replay: true` header). It is the direct evidence that the mechanism engaged, as opposed to inferring it from an absence.

### Verdict: ✅ **All invariants held.**

Under this load, with chaos injected, no strategy sold more units than it had. Note that this is evidence, not proof: concurrency bugs are probabilistic, and a passing run bounds how easily a defect reproduces rather than showing there is none. Re-run at higher N and higher chaos before treating it as settled.

## Per-strategy detail

### `pessimistic` — sale `5a1e0000-0000-4000-8000-000000000005`
*Lumen 4K Action Camera*

- Requests sent: **54** (40 logical buyers + 14 chaos duplicates)
- Confirmed in database: **38** / 500 stock
- Confirmed seen by clients: 38
- Rejected as sold out: 0
- Aborted mid-flight (chaos): 16
- Failed / errored: 0
- Final availability — reported `500` (SQL `462`, Redis `500`)
  - The two disagree, which is expected here rather than alarming: `pessimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 1014.4ms / 1284.6ms / 1323.3ms
- Wall time: 1.35s (40 req/s)

**Duplicate requests:** 14 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
14 of those had **both requests confirmed** — the same buyer got two units from one intent.

> Expected for now: the API has no idempotency key, so two independent POSTs are two independent purchases and the server has no way to know they were one intent. This does **not** violate the stock invariant — the total is still capped — but it is the exact behaviour idempotency is meant to remove, recorded here as a before-picture for that work.

### `pessimistic` — sale `5a1e0000-0000-4000-8000-000000000005`
*Lumen 4K Action Camera*

- Requests sent: **54** (40 logical buyers + 14 chaos duplicates)
- Confirmed in database: **24** / 500 stock
- Confirmed seen by clients: 38
  - **14 more client(s) were told 'confirmed' than there are orders** — and that is the idempotency layer working, not a discrepancy. A replayed retry legitimately reports the confirmation of the order its first attempt already created, so N confirmations can correspond to fewer than N orders. Cross-check: 14 response(s) were served from cache.
- Rejected as sold out: 0
- Aborted mid-flight (chaos): 16
- Failed / errored: 0
- Final availability — reported `500` (SQL `476`, Redis `500`)
  - The two disagree, which is expected here rather than alarming: `pessimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 571.0ms / 681.2ms / 698.9ms
- Wall time: 0.72s (75 req/s)

**Duplicate requests:** 14 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
14 of those had **both requests confirmed** — the same buyer got two units from one intent.

> Expected for now: the API has no idempotency key, so two independent POSTs are two independent purchases and the server has no way to know they were one intent. This does **not** violate the stock invariant — the total is still capped — but it is the exact behaviour idempotency is meant to remove, recorded here as a before-picture for that work.

### `optimistic` — sale `5a1e0000-0000-4000-8000-000000000004`
*Halo Studio Headphones*

- Requests sent: **54** (40 logical buyers + 14 chaos duplicates)
- Confirmed in database: **11** / 500 stock
- Confirmed seen by clients: 11
- Rejected as sold out: 0
- Aborted mid-flight (chaos): 16
- Failed / errored: 27
- Final availability — reported `500` (SQL `489`, Redis `500`)
  - The two disagree, which is expected here rather than alarming: `optimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 357.6ms / 704.1ms / 710.0ms
- Wall time: 0.77s (70 req/s)

**Duplicate requests:** 14 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
1 of those had **both requests confirmed** — the same buyer got two units from one intent.

> Expected for now: the API has no idempotency key, so two independent POSTs are two independent purchases and the server has no way to know they were one intent. This does **not** violate the stock invariant — the total is still capped — but it is the exact behaviour idempotency is meant to remove, recorded here as a before-picture for that work.

### `optimistic` — sale `5a1e0000-0000-4000-8000-000000000004`
*Halo Studio Headphones*

- Requests sent: **54** (40 logical buyers + 14 chaos duplicates)
- Confirmed in database: **8** / 500 stock
- Confirmed seen by clients: 12
  - **4 more client(s) were told 'confirmed' than there are orders** — and that is the idempotency layer working, not a discrepancy. A replayed retry legitimately reports the confirmation of the order its first attempt already created, so N confirmations can correspond to fewer than N orders. Cross-check: 14 response(s) were served from cache.
- Rejected as sold out: 0
- Aborted mid-flight (chaos): 16
- Failed / errored: 26
- Final availability — reported `500` (SQL `492`, Redis `500`)
  - The two disagree, which is expected here rather than alarming: `optimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 943.7ms / 1095.6ms / 1118.3ms
- Wall time: 1.14s (48 req/s)

**Duplicate requests:** 14 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
4 of those had **both requests confirmed** — the same buyer got two units from one intent.

> Expected for now: the API has no idempotency key, so two independent POSTs are two independent purchases and the server has no way to know they were one intent. This does **not** violate the stock invariant — the total is still capped — but it is the exact behaviour idempotency is meant to remove, recorded here as a before-picture for that work.

### `redis` — sale `5a1e0000-0000-4000-8000-000000000003`
*Vortex Pro Controller*

- Requests sent: **54** (40 logical buyers + 14 chaos duplicates)
- Confirmed in database: **38** / 500 stock
- Confirmed seen by clients: 38
- Rejected as sold out: 0
- Aborted mid-flight (chaos): 16
- Failed / errored: 0
- Final availability — reported `462` (SQL `500`, Redis `462`)
  - The two disagree, which is expected here rather than alarming: `redis` decrements **Redis** only, so **PostgreSQL** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 426.6ms / 759.5ms / 762.9ms
- Wall time: 0.88s (62 req/s)

**Duplicate requests:** 14 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
14 of those had **both requests confirmed** — the same buyer got two units from one intent.

> Expected for now: the API has no idempotency key, so two independent POSTs are two independent purchases and the server has no way to know they were one intent. This does **not** violate the stock invariant — the total is still capped — but it is the exact behaviour idempotency is meant to remove, recorded here as a before-picture for that work.

### `redis` — sale `5a1e0000-0000-4000-8000-000000000003`
*Vortex Pro Controller*

- Requests sent: **54** (40 logical buyers + 14 chaos duplicates)
- Confirmed in database: **24** / 500 stock
- Confirmed seen by clients: 38
  - **14 more client(s) were told 'confirmed' than there are orders** — and that is the idempotency layer working, not a discrepancy. A replayed retry legitimately reports the confirmation of the order its first attempt already created, so N confirmations can correspond to fewer than N orders. Cross-check: 14 response(s) were served from cache.
- Rejected as sold out: 0
- Aborted mid-flight (chaos): 16
- Failed / errored: 0
- Final availability — reported `476` (SQL `500`, Redis `476`)
  - The two disagree, which is expected here rather than alarming: `redis` decrements **Redis** only, so **PostgreSQL** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 416.2ms / 534.3ms / 550.6ms
- Wall time: 0.57s (95 req/s)

**Duplicate requests:** 14 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
14 of those had **both requests confirmed** — the same buyer got two units from one intent.

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