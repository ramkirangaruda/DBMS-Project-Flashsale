# Chaos Verification Report

Generated: 2026-08-06 07:19:12 UTC

Black-box run against the live HTTP API at `http://127.0.0.1:8010`. No checkout code, schema, or configuration was modified; stock was prepared through the existing `POST /admin/reset-sale` endpoint and verified with read-only `SELECT`s.

## Configuration

- Concurrent buyers (N): **1000** per strategy
- Chaos fraction: **30%** of requests
- Starting stock: **60** units per sale
- Strategies: pessimistic, optimistic, redis
- RNG seed: 2024 (re-run with the same seed to reproduce)
- Front door: **waiting room BYPASSED** (`--no-queue`) — the raw pre-queue herd, requires `ADMISSION_ENFORCED=false`

## The invariant

> **Confirmed orders created during a run must never exceed the stock the sale started with.**

Counted from the `orders` table rather than from this harness's own responses. A client that aborts mid-flight can leave behind a committed order it never saw confirmed, so a client-side tally would under-count exactly the case chaos testing exists to surface. It is also not read from `GET /sales/{id}`: the Redis fast path never decrements `inventory.reserved_stock`, so SQL availability would under-report Redis purchases.

## Summary

| Strategy | Idem | N | Stock | Confirmed (DB) | Oversold | Invariant | Sold out | Aborted | Failed | Dup pairs | Bought twice | Both said OK | Replayed | p50 | p95 | p99 | Wall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `pessimistic` | on | 1000 | 60 | **60** | 0 | ✅ HOLDS | 82 | 93 | 872 | 107 | **0** | 0 | 7 | 14480.1ms | 32453.7ms | 42804.4ms | 44.50s |
| `optimistic` | on | 1000 | 60 | **60** | 0 | ✅ HOLDS | 16 | 93 | 935 | 107 | **0** | 3 | 5 | 17337.7ms | 32325.8ms | 43007.0ms | 45.58s |
| `redis` | on | 1000 | 60 | **60** | 0 | ✅ HOLDS | 93 | 93 | 861 | 107 | **0** | 0 | 4 | 13823.2ms | 37970.2ms | 50500.6ms | 53.70s |

**Bought twice** = duplicate pairs that produced two different `order_id`s (the real defect). **Both said OK** = pairs where both responses read `confirmed`, which after idempotency includes replays of the *same* order and is therefore expected to stay high. Read the first column.

## Waiting room

| Strategy | Queue | Buyers | **Max concurrent checkouts** | Queue wait p50 / p95 / max | Refused at door (403) |
|---|---|---|---|---|---|
| `pessimistic` | **off** | 1000 | **1014** | -- | 0 |
| `optimistic` | **off** | 1000 | **1014** | -- | 0 |
| `redis` | **off** | 1000 | **1014** | -- | 0 |

**Max concurrent checkouts** is the peak number of checkout requests in flight at the same instant, computed by sweep line over every request's start and end time. It is the number the waiting room exists to bound: with the queue off it should approach the buyer count, and with the queue on it should stay near the admission batch size no matter how many people showed up.

It is measured client-side on purpose. The server also publishes `checkout_in_flight`, but Prometheus samples that every 2 seconds and can step over a peak between scrapes; this sees every interval. Two independent measurements of the same quantity is the point.

### Verdict: ✅ **All invariants held.**

Under this load, with chaos injected, no strategy sold more units than it had. Note that this is evidence, not proof: concurrency bugs are probabilistic, and a passing run bounds how easily a defect reproduces rather than showing there is none. Re-run at higher N and higher chaos before treating it as settled.

## Per-strategy detail

### `pessimistic` — sale `5a1e0000-0000-4000-8000-000000000005`
*Lumen 4K Action Camera*

- Requests sent: **1107** (1000 logical buyers + 107 chaos duplicates)
- Confirmed in database: **60** / 60 stock
- Confirmed seen by clients: 60
- Rejected as sold out: 82
- Aborted mid-flight (chaos): 93
- Failed / errored: 872
- Final availability — reported `60` (SQL `0`, Redis `60`)
  - The two disagree, which is expected here rather than alarming: `pessimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 14480.1ms / 32453.7ms / 42804.4ms
- Wall time: 44.50s (25 req/s)

**Duplicate requests:** 107 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
None of them had both requests confirmed — in this run the duplicates lost the race for remaining stock rather than being deduplicated. That is scarcity doing the work, not idempotency; at higher stock levels both would be expected to succeed.

### `optimistic` — sale `5a1e0000-0000-4000-8000-000000000004`
*Halo Studio Headphones*

- Requests sent: **1107** (1000 logical buyers + 107 chaos duplicates)
- Confirmed in database: **60** / 60 stock
- Confirmed seen by clients: 63
  - **3 more client(s) were told 'confirmed' than there are orders** — and that is the idempotency layer working, not a discrepancy. A replayed retry legitimately reports the confirmation of the order its first attempt already created, so N confirmations can correspond to fewer than N orders. Cross-check: 5 response(s) were served from cache.
- Rejected as sold out: 16
- Aborted mid-flight (chaos): 93
- Failed / errored: 935
- Final availability — reported `60` (SQL `0`, Redis `60`)
  - The two disagree, which is expected here rather than alarming: `optimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 17337.7ms / 32325.8ms / 43007.0ms
- Wall time: 45.58s (24 req/s)

**Duplicate requests:** 107 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
3 of those had **both requests confirmed** — the same buyer got two units from one intent.

> Expected for now: the API has no idempotency key, so two independent POSTs are two independent purchases and the server has no way to know they were one intent. This does **not** violate the stock invariant — the total is still capped — but it is the exact behaviour idempotency is meant to remove, recorded here as a before-picture for that work.

### `redis` — sale `5a1e0000-0000-4000-8000-000000000003`
*Vortex Pro Controller*

- Requests sent: **1107** (1000 logical buyers + 107 chaos duplicates)
- Confirmed in database: **60** / 60 stock
- Confirmed seen by clients: 60
- Rejected as sold out: 93
- Aborted mid-flight (chaos): 93
- Failed / errored: 861
- Final availability — reported `0` (SQL `60`, Redis `0`)
  - The two disagree, which is expected here rather than alarming: `redis` decrements **Redis** only, so **PostgreSQL** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 13823.2ms / 37970.2ms / 50500.6ms
- Wall time: 53.70s (21 req/s)

**Duplicate requests:** 107 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
None of them had both requests confirmed — in this run the duplicates lost the race for remaining stock rather than being deduplicated. That is scarcity doing the work, not idempotency; at higher stock levels both would be expected to succeed.

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