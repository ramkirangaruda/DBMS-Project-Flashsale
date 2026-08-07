# Chaos Verification Report

Generated: 2026-08-06 18:08:27 UTC

Black-box run against the live HTTP API at `http://127.0.0.1:8080`. No checkout code, schema, or configuration was modified; stock was prepared through the existing `POST /admin/reset-sale` endpoint and verified with read-only `SELECT`s.

## Configuration

- Concurrent buyers (N): **300** per strategy
- Chaos fraction: **0%** of requests
- Starting stock: **300** units per sale
- Strategies: redis
- RNG seed: 555 (re-run with the same seed to reproduce)
- Front door: **waiting room ON** — buyers join `/queue/join/{sale_id}` and are admitted 50 every 0.5s (**100.0/s**)

## The invariant

> **Confirmed orders created during a run must never exceed the stock the sale started with.**

Counted from the `orders` table rather than from this harness's own responses. A client that aborts mid-flight can leave behind a committed order it never saw confirmed, so a client-side tally would under-count exactly the case chaos testing exists to surface. It is also not read from `GET /sales/{id}`: the Redis fast path never decrements `inventory.reserved_stock`, so SQL availability would under-report Redis purchases.

## Summary

| Strategy | Idem | N | Stock | Confirmed (DB) | Oversold | Invariant | Sold out | Aborted | Failed | Dup pairs | Bought twice | Both said OK | Replayed | p50 | p95 | p99 | Wall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `redis` | on | 300 | 300 | **300** | 0 | ✅ HOLDS | 0 | 0 | 0 | 0 | **0** | 0 | 0 | 1395.9ms | 7627.9ms | 11341.0ms | 18.37s |

**Bought twice** = duplicate pairs that produced two different `order_id`s (the real defect). **Both said OK** = pairs where both responses read `confirmed`, which after idempotency includes replays of the *same* order and is therefore expected to stay high. Read the first column.

## Waiting room

| Strategy | Queue | Buyers | **Max concurrent checkouts** | Queue wait p50 / p95 / max | Refused at door (403) |
|---|---|---|---|---|---|
| `redis` | on | 300 | **73** | 7.7s / 16.7s / 18.1s | 0 |

**Max concurrent checkouts** is the peak number of checkout requests in flight at the same instant, computed by sweep line over every request's start and end time. It is the number the waiting room exists to bound: with the queue off it should approach the buyer count, and with the queue on it should stay near the admission batch size no matter how many people showed up.

It is measured client-side on purpose. The server also publishes `checkout_in_flight`, but Prometheus samples that every 2 seconds and can step over a peak between scrapes; this sees every interval. Two independent measurements of the same quantity is the point.

### Verdict: ✅ **All invariants held.**

Under this load, with chaos injected, no strategy sold more units than it had. Note that this is evidence, not proof: concurrency bugs are probabilistic, and a passing run bounds how easily a defect reproduces rather than showing there is none. Re-run at higher N and higher chaos before treating it as settled.

## Per-strategy detail

### `redis` — sale `5a1e0000-0000-4000-8000-000000000005`
*Lumen 4K Action Camera*

- Requests sent: **300** (300 logical buyers + 0 chaos duplicates)
- Confirmed in database: **300** / 300 stock
- Confirmed seen by clients: 300
- Rejected as sold out: 0
- Aborted mid-flight (chaos): 0
- Failed / errored: 0
- Final availability — reported `0` (SQL `300`, Redis `0`)
  - The two disagree, which is expected here rather than alarming: `redis` decrements **Redis** only, so **PostgreSQL** still shows its pre-run figure. Each strategy owns one counter; nothing reconciles them, by design (see the CAP note on `/checkout/redis`). It is also why the invariant above is counted from the `orders` table, which every strategy writes to, instead of from either counter.
- Latency p50 / p95 / p99: 1395.9ms / 7627.9ms / 11341.0ms
- Wall time: 18.37s (16 req/s)

## Reading these numbers

- **Aborted** requests have no latency recorded — they were cancelled before a response arrived, so percentiles cover completed requests only.
- **Failed** includes optimistic-strategy version conflicts, which are the OCC design working (a losing writer is told to retry), not an error.
- A `confirmed (DB)` count *lower* than stock is normal when chaos aborts requests before they reach the server.

Re-run at other scales:

```bash
python -m scripts.chaos_verification --n 1000 --chaos 0.5
python -m scripts.chaos_verification --strategy optimistic --n 500 --stock 25
```