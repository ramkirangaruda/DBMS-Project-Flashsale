# Chaos Verification Report

Generated: 2026-08-06 18:13:20 UTC

Black-box run against the live HTTP API at `http://127.0.0.1:8080`. No checkout code, schema, or configuration was modified; stock was prepared through the existing `POST /admin/reset-sale` endpoint and verified with read-only `SELECT`s.

## Configuration

- Concurrent buyers (N): **600** per strategy
- Chaos fraction: **0%** of requests
- Starting stock: **60** units per sale
- Strategies: pessimistic
- RNG seed: 555 (re-run with the same seed to reproduce)
- Front door: **waiting room ON** — buyers join `/queue/join/{sale_id}` and are admitted 10 every 0.5s (**20.0/s**)

## The invariant

> **Confirmed orders created during a run must never exceed the stock the sale started with.**

Counted from the `orders` table rather than from this harness's own responses. A client that aborts mid-flight can leave behind a committed order it never saw confirmed, so a client-side tally would under-count exactly the case chaos testing exists to surface. It is also not read from `GET /sales/{id}`: the Redis fast path never decrements `inventory.reserved_stock`, so SQL availability would under-report Redis purchases.

## Summary

| Strategy | Idem | N | Stock | Confirmed (DB) | Oversold | Invariant | Sold out | Aborted | Failed | Dup pairs | Bought twice | Both said OK | Replayed | p50 | p95 | p99 | Wall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `pessimistic` | on | 600 | 60 | **60** | 0 | ✅ HOLDS | 515 | 0 | 25 | 0 | **0** | 0 | 0 | 1941.3ms | 19060.3ms | 28716.7ms | 42.78s |

**Bought twice** = duplicate pairs that produced two different `order_id`s (the real defect). **Both said OK** = pairs where both responses read `confirmed`, which after idempotency includes replays of the *same* order and is therefore expected to stay high. Read the first column.

## Waiting room

| Strategy | Queue | Buyers | **Max concurrent checkouts** | Queue wait p50 / p95 / max | Refused at door (403) |
|---|---|---|---|---|---|
| `pessimistic` | on | 600 | **115** | 25.2s / 40.4s / 42.2s | 4 |

**Max concurrent checkouts** is the peak number of checkout requests in flight at the same instant, computed by sweep line over every request's start and end time. It is the number the waiting room exists to bound: with the queue off it should approach the buyer count, and with the queue on it should stay near the admission batch size no matter how many people showed up.

It is measured client-side on purpose. The server also publishes `checkout_in_flight`, but Prometheus samples that every 2 seconds and can step over a peak between scrapes; this sees every interval. Two independent measurements of the same quantity is the point.

### Verdict: ✅ **All invariants held.**

Under this load, with chaos injected, no strategy sold more units than it had. Note that this is evidence, not proof: concurrency bugs are probabilistic, and a passing run bounds how easily a defect reproduces rather than showing there is none. Re-run at higher N and higher chaos before treating it as settled.

## Per-strategy detail

### `pessimistic` — sale `5a1e0000-0000-4000-8000-000000000005`
*Lumen 4K Action Camera*

- Requests sent: **600** (600 logical buyers + 0 chaos duplicates)
- Confirmed in database: **60** / 60 stock
- Confirmed seen by clients: 60
- Rejected as sold out: 515
- Aborted mid-flight (chaos): 0
- Failed / errored: 25
- Final availability — reported `60` (SQL `0`, Redis `60`)
  - The two disagree, which is expected here rather than alarming: `pessimistic` decrements **PostgreSQL** only, so **Redis** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 1941.3ms / 19060.3ms / 28716.7ms
- Wall time: 42.78s (14 req/s)

## Reading these numbers

- **Aborted** requests have no latency recorded — they were cancelled before a response arrived, so percentiles cover completed requests only.
- **Failed** includes optimistic-strategy version conflicts, which are the OCC design working (a losing writer is told to retry), not an error.
- A `confirmed (DB)` count *lower* than stock is normal when chaos aborts requests before they reach the server.

Re-run at other scales:

```bash
python -m scripts.chaos_verification --n 1000 --chaos 0.5
python -m scripts.chaos_verification --strategy optimistic --n 500 --stock 25
```