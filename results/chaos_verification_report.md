# Chaos Verification Report

Generated: 2026-08-04 16:14:31 UTC

Black-box run against the live HTTP API at `http://127.0.0.1:8010`. No checkout code, schema, or configuration was modified; stock was prepared through the existing `POST /admin/reset-sale` endpoint and verified with read-only `SELECT`s.

## Configuration

- Concurrent buyers (N): **200** per strategy
- Chaos fraction: **30%** of requests
- Starting stock: **5** units per sale
- Strategies: pessimistic, optimistic, redis
- RNG seed: 1337 (re-run with the same seed to reproduce)

## The invariant

> **Confirmed orders created during a run must never exceed the stock the sale started with.**

Counted from the `orders` table rather than from this harness's own responses. A client that aborts mid-flight can leave behind a committed order it never saw confirmed, so a client-side tally would under-count exactly the case chaos testing exists to surface. It is also not read from `GET /sales/{id}`: the Redis fast path never decrements `inventory.reserved_stock`, so SQL availability would under-report Redis purchases.

## Summary

| Strategy | N | Stock | Confirmed (DB) | Oversold | Invariant | Sold out | Aborted | Failed | Dup pairs | Dup both OK | p50 | p95 | p99 | Wall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `pessimistic` | 200 | 5 | **0** | 0 | ✅ HOLDS | 206 | 12 | 0 | 23 | 0 | 1126.9ms | 2227.1ms | 2356.7ms | 2.49s |
| `optimistic` | 200 | 5 | **5** | 0 | ✅ HOLDS | 185 | 12 | 21 | 23 | 0 | 3022.8ms | 6490.1ms | 6747.2ms | 7.02s |
| `redis` | 200 | 5 | **5** | 0 | ✅ HOLDS | 206 | 12 | 0 | 23 | 0 | 2995.1ms | 6635.8ms | 6909.7ms | 7.11s |

### Verdict: ✅ **All invariants held.**

Under this load, with chaos injected, no strategy sold more units than it had. Note that this is evidence, not proof: concurrency bugs are probabilistic, and a passing run bounds how easily a defect reproduces rather than showing there is none. Re-run at higher N and higher chaos before treating it as settled.

## Per-strategy detail

### `pessimistic` — sale `5a1e0000-0000-4000-8000-000000000005`
*Lumen 4K Action Camera*

- Requests sent: **223** (200 logical buyers + 23 chaos duplicates)
- Confirmed in database: **0** / 5 stock
- Confirmed seen by clients: 5
- Rejected as sold out: 206
- Aborted mid-flight (chaos): 12
- Failed / errored: 0
- Final availability — reported `5` (SQL `0`, Redis `5`)
  - The two disagree, which is expected here rather than alarming: `pessimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 1126.9ms / 2227.1ms / 2356.7ms
- Wall time: 2.49s (89 req/s)

**Duplicate requests:** 23 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
None of them had both requests confirmed — in this run the duplicates lost the race for remaining stock rather than being deduplicated. That is scarcity doing the work, not idempotency; at higher stock levels both would be expected to succeed.

### `optimistic` — sale `5a1e0000-0000-4000-8000-000000000004`
*Halo Studio Headphones*

- Requests sent: **223** (200 logical buyers + 23 chaos duplicates)
- Confirmed in database: **5** / 5 stock
- Confirmed seen by clients: 5
- Rejected as sold out: 185
- Aborted mid-flight (chaos): 12
- Failed / errored: 21
- Final availability — reported `5` (SQL `0`, Redis `5`)
  - The two disagree, which is expected here rather than alarming: `optimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 3022.8ms / 6490.1ms / 6747.2ms
- Wall time: 7.02s (32 req/s)

**Duplicate requests:** 23 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
None of them had both requests confirmed — in this run the duplicates lost the race for remaining stock rather than being deduplicated. That is scarcity doing the work, not idempotency; at higher stock levels both would be expected to succeed.

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
- Latency p50 / p95 / p99: 2995.1ms / 6635.8ms / 6909.7ms
- Wall time: 7.11s (31 req/s)

**Duplicate requests:** 23 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
None of them had both requests confirmed — in this run the duplicates lost the race for remaining stock rather than being deduplicated. That is scarcity doing the work, not idempotency; at higher stock levels both would be expected to succeed.

## Reading these numbers

- **Aborted** requests have no latency recorded — they were cancelled before a response arrived, so percentiles cover completed requests only.
- **Failed** includes optimistic-strategy version conflicts, which are the OCC design working (a losing writer is told to retry), not an error.
- A `confirmed (DB)` count *lower* than stock is normal when chaos aborts requests before they reach the server.

Re-run at other scales:

```bash
python -m scripts.chaos_verification --n 1000 --chaos 0.5
python -m scripts.chaos_verification --strategy optimistic --n 500 --stock 25
```