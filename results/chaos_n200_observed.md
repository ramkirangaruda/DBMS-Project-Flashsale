# Chaos Verification Report

Generated: 2026-08-05 13:08:50 UTC

Black-box run against the live HTTP API at `http://127.0.0.1:8010`. No checkout code, schema, or configuration was modified; stock was prepared through the existing `POST /admin/reset-sale` endpoint and verified with read-only `SELECT`s.

## Configuration

- Concurrent buyers (N): **200** per strategy
- Chaos fraction: **30%** of requests
- Starting stock: **5** units per sale
- Strategies: pessimistic, pessimistic, optimistic, optimistic, redis, redis
- RNG seed: 1337 (re-run with the same seed to reproduce)

## The invariant

> **Confirmed orders created during a run must never exceed the stock the sale started with.**

Counted from the `orders` table rather than from this harness's own responses. A client that aborts mid-flight can leave behind a committed order it never saw confirmed, so a client-side tally would under-count exactly the case chaos testing exists to surface. It is also not read from `GET /sales/{id}`: the Redis fast path never decrements `inventory.reserved_stock`, so SQL availability would under-report Redis purchases.

## Summary

| Strategy | Idem | N | Stock | Confirmed (DB) | Oversold | Invariant | Sold out | Aborted | Failed | Dup pairs | Bought twice | Both said OK | Replayed | p50 | p95 | p99 | Wall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `pessimistic` | off | 200 | 5 | **5** | 0 | ✅ HOLDS | 206 | 12 | 0 | 23 | **0** | 0 | 0 | 1780.8ms | 4522.4ms | 4777.8ms | 4.95s |
| `pessimistic` | on | 200 | 5 | **5** | 0 | ✅ HOLDS | 203 | 12 | 0 | 23 | **0** | 3 | 23 | 1053.6ms | 1585.7ms | 1756.9ms | 2.15s |
| `optimistic` | off | 200 | 5 | **5** | 0 | ✅ HOLDS | 166 | 12 | 40 | 23 | **0** | 0 | 0 | 625.9ms | 1515.6ms | 1581.4ms | 1.66s |
| `optimistic` | on | 200 | 5 | **5** | 0 | ✅ HOLDS | 168 | 12 | 35 | 23 | **0** | 3 | 23 | 724.4ms | 1883.4ms | 2177.9ms | 2.34s |
| `redis` | off | 200 | 5 | **5** | 0 | ✅ HOLDS | 206 | 12 | 0 | 23 | **0** | 0 | 0 | 2974.1ms | 7317.4ms | 7498.4ms | 7.95s |
| `redis` | on | 200 | 5 | **5** | 0 | ✅ HOLDS | 203 | 12 | 0 | 23 | **0** | 3 | 23 | 3032.2ms | 7518.8ms | 7696.2ms | 8.08s |

**Bought twice** = duplicate pairs that produced two different `order_id`s (the real defect). **Both said OK** = pairs where both responses read `confirmed`, which after idempotency includes replays of the *same* order and is therefore expected to stay high. Read the first column.

## Before / after: Idempotency-Key

Each strategy run twice at seed **1337** with identical parameters. The chaos plan is seeded, so both arms send the same requests in the same order, and both arms share one per-invocation key namespace, so a retry in either arm carries the same key its original would have. The only difference is whether that header is sent at all. Same experiment, one variable.

(Keys are derived from a nonce generated fresh each invocation rather than from the seed. Seeded keys would be reproducible across *runs*, which sounds desirable but means a re-run replays the previous run's 24h cache and creates zero orders -- and with `--strategy all`, every strategy would share one key space and strategies 2 and 3 would replay strategy 1.)

| Strategy | Dup pairs | **Bought twice BEFORE** | **Bought twice AFTER** | Retries served from cache | Confirmed (DB) before → after | Invariant |
|---|---|---|---|---|---|---|
| `pessimistic` | 23 | **0** | **0** | 23 | 5 → 5 | ✅ |
| `optimistic` | 23 | **0** | **0** | 23 | 5 → 5 | ✅ |
| `redis` | 23 | **0** | **0** | 23 | 5 → 5 | ✅ |

> "Bought twice" counts duplicate pairs that produced **two different `order_id`s** — one intent that really did purchase twice. That is the defect, and it is deliberately not the same as *both responses said "confirmed"*: once idempotency is on, a replayed response is also `confirmed`, because it is the original result being handed back. Both counts appear in the summary table above; only this one tracks the bug.

**No double-confirms in either arm at this configuration.** That is not evidence idempotency worked -- at this contention level the retry loses the race for the last unit and is rejected as sold out regardless. Scarcity, not deduplication, is doing the work. Use the low-contention control below, where stock is not the limiting factor, to see the difference.

The `Replayed` column counts responses the server served from cache (the `Idempotent-Replay: true` header). It is the direct evidence that the mechanism engaged, as opposed to inferring it from an absence.

### Verdict: ✅ **All invariants held.**

Under this load, with chaos injected, no strategy sold more units than it had. Note that this is evidence, not proof: concurrency bugs are probabilistic, and a passing run bounds how easily a defect reproduces rather than showing there is none. Re-run at higher N and higher chaos before treating it as settled.

## Per-strategy detail

### `pessimistic` — sale `5a1e0000-0000-4000-8000-000000000005`
*Lumen 4K Action Camera*

- Requests sent: **223** (200 logical buyers + 23 chaos duplicates)
- Confirmed in database: **5** / 5 stock
- Confirmed seen by clients: 5
- Rejected as sold out: 206
- Aborted mid-flight (chaos): 12
- Failed / errored: 0
- Final availability — reported `5` (SQL `0`, Redis `5`)
  - The two disagree, which is expected here rather than alarming: `pessimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 1780.8ms / 4522.4ms / 4777.8ms
- Wall time: 4.95s (45 req/s)

**Duplicate requests:** 23 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
None of them had both requests confirmed — in this run the duplicates lost the race for remaining stock rather than being deduplicated. That is scarcity doing the work, not idempotency; at higher stock levels both would be expected to succeed.

### `pessimistic` — sale `5a1e0000-0000-4000-8000-000000000005`
*Lumen 4K Action Camera*

- Requests sent: **223** (200 logical buyers + 23 chaos duplicates)
- Confirmed in database: **5** / 5 stock
- Confirmed seen by clients: 8
  - **3 more client(s) were told 'confirmed' than there are orders** — and that is the idempotency layer working, not a discrepancy. A replayed retry legitimately reports the confirmation of the order its first attempt already created, so N confirmations can correspond to fewer than N orders. Cross-check: 23 response(s) were served from cache.
- Rejected as sold out: 203
- Aborted mid-flight (chaos): 12
- Failed / errored: 0
- Final availability — reported `5` (SQL `0`, Redis `5`)
  - The two disagree, which is expected here rather than alarming: `pessimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 1053.6ms / 1585.7ms / 1756.9ms
- Wall time: 2.15s (104 req/s)

**Duplicate requests:** 23 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
3 of those had **both requests confirmed** — the same buyer got two units from one intent.

> Expected for now: the API has no idempotency key, so two independent POSTs are two independent purchases and the server has no way to know they were one intent. This does **not** violate the stock invariant — the total is still capped — but it is the exact behaviour idempotency is meant to remove, recorded here as a before-picture for that work.

### `optimistic` — sale `5a1e0000-0000-4000-8000-000000000004`
*Halo Studio Headphones*

- Requests sent: **223** (200 logical buyers + 23 chaos duplicates)
- Confirmed in database: **5** / 5 stock
- Confirmed seen by clients: 5
- Rejected as sold out: 166
- Aborted mid-flight (chaos): 12
- Failed / errored: 40
- Final availability — reported `5` (SQL `0`, Redis `5`)
  - The two disagree, which is expected here rather than alarming: `optimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 625.9ms / 1515.6ms / 1581.4ms
- Wall time: 1.66s (134 req/s)

**Duplicate requests:** 23 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
None of them had both requests confirmed — in this run the duplicates lost the race for remaining stock rather than being deduplicated. That is scarcity doing the work, not idempotency; at higher stock levels both would be expected to succeed.

### `optimistic` — sale `5a1e0000-0000-4000-8000-000000000004`
*Halo Studio Headphones*

- Requests sent: **223** (200 logical buyers + 23 chaos duplicates)
- Confirmed in database: **5** / 5 stock
- Confirmed seen by clients: 8
  - **3 more client(s) were told 'confirmed' than there are orders** — and that is the idempotency layer working, not a discrepancy. A replayed retry legitimately reports the confirmation of the order its first attempt already created, so N confirmations can correspond to fewer than N orders. Cross-check: 23 response(s) were served from cache.
- Rejected as sold out: 168
- Aborted mid-flight (chaos): 12
- Failed / errored: 35
- Final availability — reported `5` (SQL `0`, Redis `5`)
  - The two disagree, which is expected here rather than alarming: `optimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 724.4ms / 1883.4ms / 2177.9ms
- Wall time: 2.34s (95 req/s)

**Duplicate requests:** 23 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
3 of those had **both requests confirmed** — the same buyer got two units from one intent.

> Expected for now: the API has no idempotency key, so two independent POSTs are two independent purchases and the server has no way to know they were one intent. This does **not** violate the stock invariant — the total is still capped — but it is the exact behaviour idempotency is meant to remove, recorded here as a before-picture for that work.

### `redis` — sale `5a1e0000-0000-4000-8000-000000000003`
*Vortex Pro Controller*

- Requests sent: **223** (200 logical buyers + 23 chaos duplicates)
- Confirmed in database: **5** / 5 stock
- Confirmed seen by clients: 5
- Rejected as sold out: 206
- Aborted mid-flight (chaos): 12
- Failed / errored: 0
- Final availability — reported `0` (SQL `5`, Redis `0`)
  - The two disagree, which is expected here rather than alarming: `redis` decrements **Redis** only, so **PostgreSQL** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 2974.1ms / 7317.4ms / 7498.4ms
- Wall time: 7.95s (28 req/s)

**Duplicate requests:** 23 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
None of them had both requests confirmed — in this run the duplicates lost the race for remaining stock rather than being deduplicated. That is scarcity doing the work, not idempotency; at higher stock levels both would be expected to succeed.

### `redis` — sale `5a1e0000-0000-4000-8000-000000000003`
*Vortex Pro Controller*

- Requests sent: **223** (200 logical buyers + 23 chaos duplicates)
- Confirmed in database: **5** / 5 stock
- Confirmed seen by clients: 8
  - **3 more client(s) were told 'confirmed' than there are orders** — and that is the idempotency layer working, not a discrepancy. A replayed retry legitimately reports the confirmation of the order its first attempt already created, so N confirmations can correspond to fewer than N orders. Cross-check: 23 response(s) were served from cache.
- Rejected as sold out: 203
- Aborted mid-flight (chaos): 12
- Failed / errored: 0
- Final availability — reported `0` (SQL `5`, Redis `0`)
  - The two disagree, which is expected here rather than alarming: `redis` decrements **Redis** only, so **PostgreSQL** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 3032.2ms / 7518.8ms / 7696.2ms
- Wall time: 8.08s (28 req/s)

**Duplicate requests:** 23 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
3 of those had **both requests confirmed** — the same buyer got two units from one intent.

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