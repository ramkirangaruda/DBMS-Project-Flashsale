# Case Study: Preventing Oversell in a Flash-Sale Inventory Engine

**Course:** Database Management Systems
**System:** Flash-Sale Inventory & Oversell Prevention Engine (FastAPI, PostgreSQL 16, Redis 7, React storefront, scikit-learn)

---

## 1. Summary

A flash sale concentrates thousands of buyers onto a handful of inventory rows within seconds. The core database risk is the **lost update**: many transactions read "1 unit left", all decide to sell it, and the store confirms more orders than it has stock. This project reproduces that failure, then builds and *measures* three concurrency-control strategies, a virtual waiting room, idempotent checkout, horizontal scaling, and two ML components (demand forecasting and bot detection) around it.

The central result: **no strategy ever oversold across any run**, but the strategies differ sharply in latency, retry behaviour and how they fail under a 1,000-buyer herd. The most instructive findings were not the successes. They were a silent under-sell the correctness check could not see, a scaling prediction that was half wrong, and an index that was sometimes slower than a table scan.

## 2. The problem

Each sale has one `inventory` row (`total_stock`, `reserved_stock`, `version`). A naive checkout does read, check, write without isolation. With 8 concurrent buyers and 1 unit of stock, the unprotected version confirmed **8 orders against 1 unit** (`demo_1_oversell`). That reproduced oversell is the baseline everything else is measured against.

Three further problems show up in real drops, and the project addresses each:

- **Contention collapse.** Hundreds of requests fighting one row lock.
- **Duplicate purchases.** A client that times out and retries can buy twice.
- **Bots and scalpers.** Account farms that win the race before humans do.

## 3. Architecture

```
buyers -> nginx -> api1 / api2 / api3 (FastAPI) -> PostgreSQL (source of truth)
                                                -> Redis (queue, idempotency, fast-path stock)
```

The request path, in order: tracing, then admission gate (waiting-room token), then metrics, then idempotency cache, then the checkout handler. Payment state (Stripe test mode) lives in Postgres in its own `payments` table. Prometheus, Grafana and Jaeger observe the whole stack.

The schema follows the course ER/normalization work: `users`, `products`, `flash_sale_events`, `inventory`, `orders`, `order_items`, `user_behavior_logs`, `flagged_orders`, `stock_audit_log`, `payments`. `reserved_stock` is a deliberate denormalization, a live counter instead of `SUM(quantity)` on every read.

## 4. Concurrency control: three strategies, one invariant

All three are exposed as endpoints so they can be compared under identical load.

| Strategy | Mechanism | Trade-off |
|---|---|---|
| Pessimistic | `SELECT ... FOR UPDATE` on the inventory row | Always correct; contenders queue on the lock |
| Optimistic (OCC) | `UPDATE ... WHERE version = :v`, retry on conflict | No lock held; wasted work and retry storms under contention |
| Redis atomic | `DECR` on a Redis counter; Postgres records the order | Fastest; Redis and SQL counters may diverge (an AP-leaning choice) |

Benchmark: 40 concurrent buyers for 5 units (8x oversubscribed), about 30 ms simulated work per request.

| Strategy | Confirmed | Oversold | Retries | Time (s) | Req/s |
|---|---|---|---|---|---|
| Pessimistic | 5 | 0 | 0 | 1.355 | 29.5 |
| Optimistic | 5 | 0 | 125 | 0.311 | 128.6 |
| Redis | 5 | 0 | 0 | 0.061 | 652.9 |

All three are correct. They achieve correctness by queueing, retrying, or avoiding the database on the hot path. The Redis path deliberately does not update `reserved_stock`, so a storefront reading only SQL would show frozen stock; the API reports both counters and prefers the live Redis value.

Supporting demos cover the rest of the syllabus against real Postgres: deadlock detection versus application-level wound-wait prevention, timestamp ordering with Thomas's Write Rule (a stale write is skipped instead of aborting the transaction), multigranularity locking (a table-level lock blocking row-level `FOR UPDATE` requests for about 1.2 s), and write-ahead-log undo/redo recovery.

## 5. Indexing

Two indexes were added and measured with `EXPLAIN ANALYZE` on 200,000 orders and 50,000 users:

- **Composite B+ tree** on `orders(sale_id, created_at)`: 0.58 ms with the index versus 11.85 ms with a sequential scan, about **20x** faster.
- **Hash index** on `users.device_fingerprint`: in a repeat run the indexed plan took 5.4 ms against 4.1 ms for the sequential scan, i.e. *slower*.

The second result is real planner behaviour, not a defect. About 1% of a small, fully cached table matches, so the bitmap index scan's overhead can exceed the cost of one sequential pass. An index pays off with selectivity and table size, and the demo reports the measurement either way rather than tuning it to look better.

## 6. Making retries safe: idempotency

A seeded chaos test sends duplicate requests (simulated client retries) at 100% chaos. Without an `Idempotency-Key`, duplicates produced real double purchases:

| Strategy | Bought twice, before | After |
|---|---|---|
| Pessimistic | 14 | 0 |
| Optimistic | 1 | 0 |
| Redis | 14 | 0 |

The invariant (confirmed orders never exceed starting stock) is counted from the `orders` table, not from client responses, because an aborted client can leave a committed order it never saw.

## 7. The waiting room

A Redis sorted set sits in front of checkout. Buyers join, poll, and are released K at a time with a short-lived admission token. Measured at N=1,000 buyers, 60 units, 30% chaos (single replica):

| | Raw herd | Through queue |
|---|---|---|
| Peak concurrent checkouts | 1,014 | 60 to 199 |
| `FOR UPDATE` wait p95 | 1,218.9 ms | 93.7 ms |
| OCC version conflicts | 69 | 11 |
| Confirmed vs stock | 60 / 60 | 60 / 60 |

## 8. Horizontal scaling and an honest prediction

Before scaling the API to three replicas, a prediction was written down and not edited afterwards: scaling the stateless tier in front of one contended row would make pessimistic lock waits *worse*, because the queue relocates into the database rather than disappearing.

It held. With the same load generator, pessimistic lock-wait p95 rose from 41.8 ms (1 replica) to about 208 to 230 ms (3 replicas), roughly 5x, as concurrent checkouts rose from 20 to about 100. One part of the prediction was wrong: Postgres connections peaked at **51 of 100**, not near exhaustion. The error was sizing risk from the pool's configured maximum instead of from actual concurrent database work.

Two methodological findings matter more than the numbers:

1. **The first load generator was the bottleneck.** A single Python process driving 1,000 coroutines reported 25.2 s queue waits where the server measured p50 0.22 s. It was reporting its own scheduling delay. Replacing it with k6 raised achieved concurrency from about 115 to about 1,000 and overturned an earlier conclusion about thread-pool limits.
2. **A silent failure.** In the raw 1,000-buyer herd the optimistic strategy sold only **47 of 60 units** (953 version conflicts) with no error and no invariant violation. The correctness check passed while the business quietly under-sold by 22%. With the queue in front, all 60 sold.

## 9. Machine learning

**Demand forecasting** (gradient boosting) trains on the UCI Online Retail dataset (about 541k line items) to predict next-day units per SKU, which informs whether a product warrants pessimistic locking or can use OCC. It is evaluated honestly: daily quantities are winsorized at the 99.5th percentile (360 units), features are per-SKU lags and rolling means, and the split is time-based (train on the first 80% of the calendar, test on the last 20%) because a random split leaks rolling features across the boundary. Result: **MAE 21.48, R^2 0.178**, beating a trailing-28-day-mean baseline (MAE 25.11, R^2 0.155) and a "yesterday" baseline (MAE 33.12). The gain is modest, which is expected: the dataset has no flash-sale events and about 51% of SKU-days have zero sales. On a 7-day horizon a plain moving average (R^2 0.505) actually beat the model (0.272), so the model is shipped only for the next-day question. The same code scores R^2 0.941 on a synthetic control whose labels are a known formula; that figure is a pipeline sanity check and must not be quoted as forecasting accuracy.

**Bot detection** uses Isolation Forest, deliberately unsupervised because real bot labels are scarce. On a synthetic harness with known bots it caught 83 of 100 (83% recall and precision); ranked by score alone, ROC-AUC is 0.989 over five draws, so the cut at 10% contamination, not the model, costs the rest. On real checkout sessions it flags unusual sessions (very fast checkout, many accounts per device) and writes them to `flagged_orders`; it assumes a 25% scalper prior, which is a disclosed hyperparameter rather than a measurement. No precision or recall is claimed for the real-data path, since it has no ground truth.

## 10. Payments

A Stripe test-mode step sits after a confirmed order. Payment state is durable in Postgres (unique on `(order_id, attempt)`), the idempotency key includes the attempt number so a genuine retry after a decline reaches the card network, and a declined payment releases the held unit back to whichever counter reserved it (SQL or Redis), guarded so a redelivered webhook cannot release twice. This was verified with synthetic webhook payloads against real Postgres and Redis; the live Stripe round trip needs an API key and was not exercised.

## 11. Lessons

- **Correctness checks are necessary, not sufficient.** The invariant held everywhere while one strategy under-sold 22%.
- **Measure the instrument.** The load generator, not the system, produced the early scaling conclusions.
- **Scaling a stateless tier does not fix a serialized resource.** It moves the queue into the database.
- **Predictions should be written down first.** Being half wrong in public showed which assumption (configured capacity equals demand) was faulty.
- **Indexes are not free wins.** Selectivity, table size and caching decide.
- **State the ceiling of every ML number.** A high synthetic score and a low real one are both honest once framed correctly.

## 12. Limitations

- The 200,000-order dataset and sales are synthetic; the ML demand data is a retail proxy, not flash-sale data.
- One PostgreSQL instance; there is no replication or failover study.
- The Redis fast path trades strict consistency for speed by design.
- Live Stripe behaviour is unverified, and there is no automated test suite, since verification is done through the demo and chaos scripts.
- Load results come from one machine and vary run to run.

## 13. Conclusion

The system never oversold under any strategy, scale, or injected fault. The more useful outcome is the comparison: pessimistic locking is simple and always correct but serializes, OCC is cheap until a herd turns retries into a stampede, and Redis is fastest while relaxing consistency. A waiting room, idempotency keys and honest measurement matter as much as the choice of locking scheme.

*Sources: `docs/results.md`, `results/queue_notes.md`, `results/horizontal_scaling_notes.md`, `results/scaling_prediction.md`, `results/chaos_verification_report.md`, `results/payment_notes.md`, and live runs of the demo scripts.*
