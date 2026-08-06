# Chaos Verification Report

Generated: 2026-08-06 08:12:02 UTC

Black-box run against the live HTTP API at `http://127.0.0.1:8010`. No checkout code, schema, or configuration was modified; stock was prepared through the existing `POST /admin/reset-sale` endpoint and verified with read-only `SELECT`s.

## Configuration

- Concurrent buyers (N): **1000** per strategy
- Chaos fraction: **30%** of requests
- Starting stock: **60** units per sale
- Strategies: pessimistic, optimistic, redis
- RNG seed: 2024 (re-run with the same seed to reproduce)
- Front door: **waiting room ON** — buyers join `/queue/join/{sale_id}` and are admitted 10 every 0.5s (**20.0/s**)

## The invariant

> **Confirmed orders created during a run must never exceed the stock the sale started with.**

Counted from the `orders` table rather than from this harness's own responses. A client that aborts mid-flight can leave behind a committed order it never saw confirmed, so a client-side tally would under-count exactly the case chaos testing exists to surface. It is also not read from `GET /sales/{id}`: the Redis fast path never decrements `inventory.reserved_stock`, so SQL availability would under-report Redis purchases.

## Summary

| Strategy | Idem | N | Stock | Confirmed (DB) | Oversold | Invariant | Sold out | Aborted | Failed | Dup pairs | Bought twice | Both said OK | Replayed | p50 | p95 | p99 | Wall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `pessimistic` | on | 1000 | 60 | **60** | 0 | ✅ HOLDS | 195 | 22 | 745 | 23 | **0** | 1 | 22 | 3807.8ms | 14928.9ms | 15491.7ms | 72.38s |
| `optimistic` | on | 1000 | 60 | **60** | 0 | ✅ HOLDS | 347 | 37 | 600 | 49 | **0** | 5 | 48 | 7465.2ms | 23888.9ms | 31390.6ms | 52.67s |
| `redis` | on | 1000 | 60 | **60** | 0 | ✅ HOLDS | 632 | 75 | 311 | 88 | **0** | 10 | 67 | 5711.9ms | 61228.8ms | 77290.3ms | 105.80s |

**Bought twice** = duplicate pairs that produced two different `order_id`s (the real defect). **Both said OK** = pairs where both responses read `confirmed`, which after idempotency includes replays of the *same* order and is therefore expected to stay high. Read the first column.

## Waiting room

| Strategy | Queue | Buyers | **Max concurrent checkouts** | Queue wait p50 / p95 / max | Refused at door (403) |
|---|---|---|---|---|---|
| `pessimistic` | on | 1000 | **60** | 13.3s / 67.8s / 70.1s | 1 |
| `optimistic` | on | 1000 | **199** | 5.3s / 46.0s / 52.1s | 11 |
| `redis` | on | 1000 | **169** | 64.1s / 99.6s / 105.0s | 118 |

**Max concurrent checkouts** is the peak number of checkout requests in flight at the same instant, computed by sweep line over every request's start and end time. It is the number the waiting room exists to bound: with the queue off it should approach the buyer count, and with the queue on it should stay near the admission batch size no matter how many people showed up.

It is measured client-side on purpose. The server also publishes `checkout_in_flight`, but Prometheus samples that every 2 seconds and can step over a peak between scrapes; this sees every interval. Two independent measurements of the same quantity is the point.

### Verdict: ✅ **All invariants held.**

Under this load, with chaos injected, no strategy sold more units than it had. Note that this is evidence, not proof: concurrency bugs are probabilistic, and a passing run bounds how easily a defect reproduces rather than showing there is none. Re-run at higher N and higher chaos before treating it as settled.

## Per-strategy detail

### `pessimistic` — sale `5a1e0000-0000-4000-8000-000000000005`
*Lumen 4K Action Camera*

- Requests sent: **1023** (1000 logical buyers + 23 chaos duplicates)
- Confirmed in database: **60** / 60 stock
- Confirmed seen by clients: 61
  - **1 more client(s) were told 'confirmed' than there are orders** — and that is the idempotency layer working, not a discrepancy. A replayed retry legitimately reports the confirmation of the order its first attempt already created, so N confirmations can correspond to fewer than N orders. Cross-check: 22 response(s) were served from cache.
- Rejected as sold out: 195
- Aborted mid-flight (chaos): 22
- Failed / errored: 745
- Final availability — reported `60` (SQL `0`, Redis `60`)
  - The two disagree, which is expected here rather than alarming: `pessimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 3807.8ms / 14928.9ms / 15491.7ms
- Wall time: 72.38s (14 req/s)

**Duplicate requests:** 23 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
1 of those had **both requests confirmed** — the same buyer got two units from one intent.

> Expected for now: the API has no idempotency key, so two independent POSTs are two independent purchases and the server has no way to know they were one intent. This does **not** violate the stock invariant — the total is still capped — but it is the exact behaviour idempotency is meant to remove, recorded here as a before-picture for that work.

### `optimistic` — sale `5a1e0000-0000-4000-8000-000000000004`
*Halo Studio Headphones*

- Requests sent: **1049** (1000 logical buyers + 49 chaos duplicates)
- Confirmed in database: **60** / 60 stock
- Confirmed seen by clients: 65
  - **5 more client(s) were told 'confirmed' than there are orders** — and that is the idempotency layer working, not a discrepancy. A replayed retry legitimately reports the confirmation of the order its first attempt already created, so N confirmations can correspond to fewer than N orders. Cross-check: 48 response(s) were served from cache.
- Rejected as sold out: 347
- Aborted mid-flight (chaos): 37
- Failed / errored: 600
- Final availability — reported `60` (SQL `0`, Redis `60`)
  - The two disagree, which is expected here rather than alarming: `optimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 7465.2ms / 23888.9ms / 31390.6ms
- Wall time: 52.67s (20 req/s)

**Duplicate requests:** 49 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
5 of those had **both requests confirmed** — the same buyer got two units from one intent.

> Expected for now: the API has no idempotency key, so two independent POSTs are two independent purchases and the server has no way to know they were one intent. This does **not** violate the stock invariant — the total is still capped — but it is the exact behaviour idempotency is meant to remove, recorded here as a before-picture for that work.

### `redis` — sale `5a1e0000-0000-4000-8000-000000000003`
*Vortex Pro Controller*

- Requests sent: **1088** (1000 logical buyers + 88 chaos duplicates)
- Confirmed in database: **60** / 60 stock
- Confirmed seen by clients: 70
  - **10 more client(s) were told 'confirmed' than there are orders** — and that is the idempotency layer working, not a discrepancy. A replayed retry legitimately reports the confirmation of the order its first attempt already created, so N confirmations can correspond to fewer than N orders. Cross-check: 67 response(s) were served from cache.
- Rejected as sold out: 632
- Aborted mid-flight (chaos): 75
- Failed / errored: 311
- Final availability — reported `0` (SQL `60`, Redis `0`)
  - The two disagree, which is expected here rather than alarming: `redis` decrements **Redis** only, so **PostgreSQL** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 5711.9ms / 61228.8ms / 77290.3ms
- Wall time: 105.80s (10 req/s)

**Duplicate requests:** 88 logical buyer(s) sent the same purchase twice (simulated retry-after-timeout).
10 of those had **both requests confirmed** — the same buyer got two units from one intent.

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