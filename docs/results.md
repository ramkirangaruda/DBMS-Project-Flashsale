# Results Log -- Real Captured Output

Every code block in this file is the actual stdout of a real run against a
fresh `docker compose up` PostgreSQL 16 + Redis 7 instance on this machine
(2026-08-02). Nothing here is hand-written or back-filled from the README's
sample numbers -- where a number in this file happens to match the README,
that's because the demo is deterministic/near-deterministic, not because it
was copied. Raw untrimmed log files backing every section below live in
`docs/_raw_logs/`.

## Environment setup and fixes applied

Standing this up end-to-end surfaced two real bugs, both fixed in this pass:

1. **Redis port 6379 was already bound** by an unrelated project's container
   on this machine (`kreis-redis`). Remapped this project's Redis to host
   port **6390** in `docker-compose.yml`, and added `REDIS_HOST`/`REDIS_PORT`
   env-var support (mirroring the existing Postgres pattern in
   `app/database.py`) to `app/main.py`, `app/demos/demo_4_redis_atomic.py`,
   and `app/demos/demo_5_benchmark.py`, which previously hardcoded
   `localhost:6379`.
2. **`openpyxl` was missing from the venv** despite being pinned in
   `requirements.txt` -- `app/ml/demand_forecast.py` crashed on
   `pd.read_excel()` for the real UCI dataset with
   `ModuleNotFoundError: No module named 'openpyxl'`. Fixed with
   `pip install openpyxl==3.1.5`.

All commands below were run with:
```
POSTGRES_PORT=5435 REDIS_PORT=6390 venv/Scripts/python.exe -m <module>
```
(`POSTGRES_PORT=5435` because `docker-compose.yml` maps Postgres to host
port 5435; `REDIS_PORT=6390` per the fix above.)

Every demo from `demo_1` through `demo_10`, plus both ML scripts and the
bot-scoring batch job, ran to completion with **zero code changes** required
to the demo/ML logic itself -- only the two environment issues above.

```
$ docker compose down -v && docker compose up -d
 Container flashsale-engine-postgres-1 Started
 Container flashsale-engine-redis-1 Started
NAME                          IMAGE         PORTS
flashsale-engine-postgres-1   postgres:16   0.0.0.0:5435->5432/tcp
flashsale-engine-redis-1      redis:7       0.0.0.0:6390->6379/tcp

$ python -m scripts.seed
Creating tables...
Seeding users (including a device-fingerprint cluster for bot demo)...
Seeding product + flash sale with scarce stock...
Seed complete.
  product_id = 9ca7cd20-605d-4b01-ae13-207cf7c0c457
  sale_id    = a26cb7f8-3f90-4e2c-b365-4d7d2c9ef1cd
  inventory total_stock = 5
  sample user_ids = ['d5d73cc5-33e2-4745-9222-54e87a7306db', 'a4fcb3f1-8dd3-47eb-b0be-e5e4c52068db', 'f136baec-5a3a-4623-b082-20c8a046884c']
```

---

## Section 8.1 -- The Oversell Problem (`demo_1_oversell.py`)

No locking, 8 concurrent buyers, 1 unit of stock:

```
Firing 8 concurrent checkout attempts at 1 unit of stock (no locking)...

  buyer 0: CONFIRMED order 04ad1774-ee47-453a-9b6f-701745db1d2a
  buyer 1: CONFIRMED order 9426e121-ed5f-44b9-a754-31a93ff904de
  buyer 2: CONFIRMED order 5f18313e-24a2-4b4a-8626-61ab375c2e32
  buyer 3: CONFIRMED order bcd7ddf1-4c38-4818-8051-4a3aefda1bff
  buyer 4: CONFIRMED order b6eb221e-4b15-49dd-a4c4-6b1aa4f90287
  buyer 5: CONFIRMED order 2bed0fc4-946d-4565-9f97-a0c7cadc9fcb
  buyer 6: CONFIRMED order a6d24f46-8a46-432d-a835-b270ac217a3b
  buyer 7: CONFIRMED order a2c54419-3499-4d1a-9142-d10b87e6e372

Stock available was 1. Confirmed orders: 8
OVERSOLD -- this is the lost-update problem in action.
```

**8 confirmed orders against 1 unit of stock** -- a real, reproduced oversell.

---

## Section 8.2 -- Concurrency Control Strategies

### Pessimistic locking (`demo_2_pessimistic.py`, `SELECT ... FOR UPDATE`)

```
Firing 8 concurrent checkout attempts at 1 unit of stock (SELECT FOR UPDATE)...

  buyer 0: CONFIRMED order 18b03462-62a7-4623-a774-0531ca85f488
  buyer 1: REJECTED (no stock)
  buyer 2: REJECTED (no stock)
  buyer 3: REJECTED (no stock)
  buyer 4: REJECTED (no stock)
  buyer 5: REJECTED (no stock)
  buyer 6: REJECTED (no stock)
  buyer 7: REJECTED (no stock)

Stock available was 1. Confirmed orders: 1 (elapsed 0.427s)
CORRECT -- exactly 1 confirmed, no oversell, because FOR UPDATE serialized access.
Note the elapsed time: locking serializes the 8 attempts, so they queue up.
```

### Optimistic concurrency control (`demo_3_optimistic.py`, version column)

```
Firing 8 concurrent checkout attempts at 1 unit of stock (optimistic/version-based)...

  buyer 0: CONFIRMED order 022629ee-20e5-4552-a494-94f2607c26f0  (retries: 0)
  buyer 1: REJECTED (no stock)  (retries: 1)
  buyer 2: REJECTED (no stock)  (retries: 1)
  buyer 3: REJECTED (no stock)  (retries: 1)
  buyer 4: REJECTED (no stock)  (retries: 1)
  buyer 5: REJECTED (no stock)  (retries: 1)
  buyer 6: REJECTED (no stock)  (retries: 1)
  buyer 7: REJECTED (no stock)  (retries: 1)

Stock available was 1. Confirmed orders: 1 (elapsed 0.155s)
Total retries across all buyers: 7
CORRECT -- exactly 1 confirmed. But notice the retry count: under this
much contention, OCC burns CPU/DB round-trips retrying instead of queueing.
```

### Redis atomic DECR (`demo_4_redis_atomic.py`)

```
Firing 8 concurrent checkout attempts at 1 unit of stock (Redis atomic DECR)...

  buyer 0: CONFIRMED order 45d1062b-073a-442e-9f76-537ecf0baa1c
  buyer 1: REJECTED (no stock, Redis fast-path)
  buyer 2: REJECTED (no stock, Redis fast-path)
  buyer 3: REJECTED (no stock, Redis fast-path)
  buyer 4: REJECTED (no stock, Redis fast-path)
  buyer 5: REJECTED (no stock, Redis fast-path)
  buyer 6: REJECTED (no stock, Redis fast-path)
  buyer 7: REJECTED (no stock, Redis fast-path)

Stock available was 1. Confirmed orders: 1 (elapsed 0.035s)
CORRECT -- exactly 1 confirmed. Notice this was the fastest of the three
approaches, because most 'sold out' rejections never touched PostgreSQL at all.
```

### Three-way benchmark (`demo_5_benchmark.py`) -- centerpiece result

40 concurrent buyers, 5 units of stock (8x oversubscribed), ~30ms simulated
work per request. This is the specific run whose orders/logs were carried
forward into the Section 10.2 bot-scoring pass below.

```
Benchmark: 40 concurrent buyers competing for 5 units, ~30ms simulated work per request.
6 of the buyer identities are the bot-cluster users from scripts/seed.py
(shared device_fingerprint) -- logged with bot-like session timing so
scripts/run_bot_scoring.py has real anomalies to catch after this runs.

Strategy                                Confirmed  Oversold  Retries  Time(s)    Req/s
--------------------------------------------------------------------------------------
Pessimistic (SELECT FOR UPDATE)                 5         0        0    1.399     28.6
Optimistic (version column)                     5         0      107    0.528     75.7
Redis atomic DECR                               5         0        0    0.103    388.2

Stock was 5, contested by 40 buyers (8x oversubscribed).
All three strategies should show 0 oversold (all correct) -- the difference
is HOW they achieve correctness: queueing (pessimistic), retrying (optimistic),
or avoiding the DB entirely on the hot path (Redis). This table is the
centerpiece result for the report -- paste it directly into Section 8.2.
```

| Strategy | Confirmed | Oversold | Retries | Time(s) | Req/s |
|---|---|---|---|---|---|
| Pessimistic (SELECT FOR UPDATE) | 5 | 0 | 0 | 1.399 | 28.6 |
| Optimistic (version column) | 5 | 0 | 107 | 0.528 | 75.7 |
| Redis atomic DECR | 5 | 0 | 0 | 0.103 | 388.2 |

Note: this run's exact numbers (elapsed times, retry counts) vary slightly
run-to-run since it's a real multi-threaded benchmark against a live DB --
the qualitative ordering (pessimistic slowest/most predictable, OCC fast but
retry-heavy under contention, Redis fastest) held in every run observed.

---

## Section 8.3 -- Timestamp Ordering (`demo_9_timestamp_ordering.py`)

Basic Timestamp Ordering extended with Thomas's Write Rule, 3 overlapping
checkouts racing for the last unit of stock:

```
==============================================================================
Section 8.3 -- Timestamp Ordering (Basic T/O + Thomas's Write Rule)
==============================================================================
Scenario: 3 overlapping checkouts race for the LAST unit of stock.
T1, T2, T3 get logical timestamps in start order (1, 2, 3), but network
delay means they don't execute in that order -- exactly the mismatch
between issue-order and arrival-order that T/O exists to police.

Timestamps assigned at start: T1=1  T2=2  T3=3

Step 1: T2 checks stock (TO-gated read).
  [T2 TS=2] READ   -> ALLOWED   R-TS updated to 2; sees 1 unit(s) available

Step 2: T3 goes straight to checkout -- it's working off stock info from
        an earlier page load, not a fresh TO-gated read -- and writes directly.
  [T3 TS=3] WRITE  -> ALLOWED   W-TS updated to 3; unit reserved

Step 3: T2 now tries to complete its checkout (write), based on the
        availability it saw back in Step 1 -- but T3 has since sold the unit.
  [T2 TS=2] WRITE  -> SKIPPED   TS(2) < W-TS(3) but no reader saw a TS in between -- Thomas's Write Rule: obsolete write ignored, no rollback needed

Step 4: T1 finally arrives (it was the slowest) and tries to check stock.
  [T1 TS=1] READ   -> REJECTED  TS(1) < W-TS(3) -- would read a value already overwritten by a later txn

==============================================================================
Without Thomas's Write Rule, Step 3 would have gone differently:
==============================================================================
  [T2 TS=2] WRITE  -> REJECTED  TS(2) < W-TS(3) -- strict Basic T/O aborts T2
  T2 would have to roll back its ENTIRE transaction and retry from scratch,
  even though its write could never have had any effect once T3 committed.
  Thomas's Write Rule recognizes that (no reader's TS fell between T2's and
  T3's) and just drops the stale write instead of paying for a full abort.

Final inventory state: total_stock=1, reserved_stock=1 -- exactly 1 unit sold (to T3), no oversell, and T2 avoided a wasted rollback.
```

Trace table (as printed by the script):

| Txn | TS | Op | R-TS (after) | W-TS (after) | Decision | Why |
|---|---|---|---|---|---|---|
| T2 | 2 | READ | 2 | 0 | ALLOWED | R-TS updated to 2; sees 1 unit(s) available |
| T3 | 3 | WRITE | 2 | 3 | ALLOWED | W-TS updated to 3; unit reserved |
| T2 | 2 | WRITE | 2 | 3 | SKIPPED | TS(2) < W-TS(3) but no reader saw a TS in between -- Thomas's Write Rule: obsolete write ignored, no rollback needed |
| T1 | 1 | READ | 2 | 3 | REJECTED | TS(1) < W-TS(3) -- would read a value already overwritten by a later txn |

---

## Section 8.4 -- Deadlock Handling (`demo_6_deadlock.py`)

Part A: PostgreSQL's own deadlock detector. Part B: application-level
wound-wait prevention.

```
======================================================================
PART A: Deliberately causing a deadlock (opposite lock order)
======================================================================
  T1: locked Inventory
  T2: locked User
  T1: requesting User lock...
  T2: requesting Inventory lock...
  T2: got Inventory lock (should not normally reach here in the deadlock case)
  T1: ABORTED by PostgreSQL deadlock detector: (psycopg2.errors.DeadlockDetected) deadlock detected

PostgreSQL's built-in deadlock detector found the wait-for cycle
(T1 waits for T2, T2 waits for T1) and aborted one side automatically.
This is the 'detect after the fact' approach.

======================================================================
PART B: Wound-wait prevention (application-level, no DB deadlock needed)
======================================================================
  T1(older): acquired Inventory
  T2(younger): acquired User
  T1(older): acquired User
  T2(younger): wounded before acquiring Inventory, aborting and releasing held locks
  T1(older): completed successfully, holding ['Inventory', 'User']

Because T1 is older, T2 was wounded the moment their lock requests
conflicted -- no cycle ever formed, so there was nothing to detect.
This is the 'prevent before it happens' approach.
```

---

## Section 8.5 -- Multi-Granularity Locking (`demo_10_multigranularity.py`)

Admin "pause the sale" (table-level `ACCESS EXCLUSIVE`) vs. 3 ordinary
checkouts taking row-level `FOR UPDATE` locks:

```
==============================================================================
Section 8.5 -- Multi-Granularity Locking
==============================================================================
Admin fires a table-level ACCESS EXCLUSIVE lock ('pause the sale') while
3 ordinary checkouts try to take row-level FOR UPDATE locks.

While the admin lock is held, here's what Postgres itself reports as blocked:

  pg_locks JOIN pg_class, filtered to the inventory table, taken WHILE checkouts are blocked:
  locktype   mode                 granted  relname    pid      state
  ----------------------------------------------------------------------
  relation   AccessExclusiveLock  True     inventory  168      idle in transaction
  relation   RowShareLock         False    inventory  169      active
  relation   RowShareLock         False    inventory  170      active
  relation   RowShareLock         False    inventory  171      active

Event log (chronological):
  [admin]      table-level ACCESS EXCLUSIVE lock acquired -- sale is now PAUSED
  [checkout-2] requesting row-level FOR UPDATE lock on inventory...
  [checkout-0] requesting row-level FOR UPDATE lock on inventory...
  [checkout-1] requesting row-level FOR UPDATE lock on inventory...
  [admin]      released the lock after 1.511s -- sale is RESUMED
  [checkout-0] lock GRANTED after waiting 1.182s -- was blocked by the admin's table lock
  [checkout-2] lock GRANTED after waiting 1.185s -- was blocked by the admin's table lock
  [checkout-1] lock GRANTED after waiting 1.185s -- was blocked by the admin's table lock

  Postgres doesn't literally have locks named IS/IX/S/SIX/X -- but its table lock
  modes map directly onto that textbook multi-granularity hierarchy:

  Textbook                     Meaning                                         Closest Postgres mode(s)
  ----------------------------------------------------------------------------------------------------
  IS  (Intention Share)        intends to read some rows below                 ACCESS SHARE / ROW SHARE
  IX  (Intention Exclusive)    intends to write some rows below                ROW EXCLUSIVE
  S   (Share)                  locks the whole object for reading              SHARE
  SIX (Share + Intention X)    reads the whole object, will write some rows    SHARE ROW EXCLUSIVE
  X   (Exclusive)              locks the whole object exclusively              EXCLUSIVE / ACCESS EXCLUSIVE
```

`pg_locks` confirms all 3 checkouts' implicit `ROW SHARE` (intention) locks
sat ungranted (`granted=False`) while the admin held `AccessExclusiveLock`,
and were all granted together the instant the admin transaction committed.

---

## Section 7 -- Transaction Processing & Recovery (`demo_7_recovery.py`)

Part A: ACID atomicity, with and without a transaction wrapper. Part B: a
simplified write-ahead log (`StockAuditLog`), immediate-update undo recovery.

```
======================================================================
PART A: ACID atomicity -- WITHOUT a transaction wrapper (broken)
======================================================================
reserved_stock before: 0
  -> reserved_stock incremented and committed
  -> CRASH before the order INSERT runs
reserved_stock after 'crash': 1  <-- stock is gone, no order exists for it!
This is exactly the atomicity violation Section 7 warns about.

======================================================================
PART A continued: WITH a transaction wrapper (correct)
======================================================================
reserved_stock before: 0
  -> reserved_stock incremented (NOT yet committed)
  -> CRASH before the order INSERT runs
  -> ROLLBACK undid the increment automatically
reserved_stock after rollback: 0  <-- correctly restored

======================================================================
PART B: Write-ahead log (StockAuditLog) -- deferred vs immediate update
======================================================================
WAL entry written (committed='false'): before=0, after=1

-- Scenario 1: DEFERRED UPDATE --
   The actual data page was never touched before commit, so if we
   crash now, there's nothing to undo. On restart, recovery only
   needs to REDO transactions whose WAL entry says committed='true'
   but whose data change didn't make it to disk yet.

-- Scenario 2: IMMEDIATE UPDATE --
   The actual data page IS updated before commit (for performance).
   If we crash before commit, recovery must UNDO using before_value
   from the WAL. If we crash after commit but before the data page
   is flushed, recovery must REDO using after_value.

   Data page updated to 1 immediately (before commit)...
   CRASH -- transaction never committed.
   Recovery reads WAL: before=0, after=1, committed=false
   Since committed='false' -> UNDO -> restore reserved_stock to 0
   reserved_stock after recovery: 0  <-- correctly restored via UNDO
```

---

## Section 6 -- Indexing (`scripts/seed_large.py` + `demo_8_indexing.py`)

Bulk-seeded dataset used for this section:

```
seed_large complete.
  products = 8, sales = 6
  users = 50,000  (hottest device_fingerprint shared by 499 accounts)
  orders = 200,000, order_items = 399,742, flagged_orders = 8,000
  primary_sale_id (used by demo_8_indexing.py) = e17f71e0-daf7-4b12-bdc9-d6d819f4e62e
```

### Query 1 -- composite B+ tree index on `orders(sale_id, created_at)`

WITH index (`Bitmap Index Scan`):
```
Sort  (cost=1534.59..1536.29 rows=681 width=44) (actual time=0.077..0.078 rows=1 loops=1)
  ->  Bitmap Heap Scan on orders  (cost=27.40..1502.55 rows=681 width=44) (actual time=0.013..0.014 rows=1 loops=1)
        ->  Bitmap Index Scan on ix_orders_sale_id_created_at  (cost=0.00..27.23 rows=681 width=0) (actual time=0.008..0.008 rows=1 loops=1)
Execution Time: 0.148 ms
```

WITHOUT index (`Seq Scan`, index dropped):
```
Gather Merge  (cost=5063.15..5109.38 rows=402 width=44) (actual time=10.360..14.604 rows=1 loops=1)
  ->  Sort ...
        ->  Parallel Seq Scan on orders  (cost=0.00..4045.75 rows=402 width=44) (actual time=2.907..7.507 rows=0 loops=2)
              Rows Removed by Filter: 100002
Execution Time: 14.631 ms
```

### Query 2 -- hash index on `users(device_fingerprint)`

WITH index (`Bitmap Index Scan`): Execution Time: **1.289 ms**
WITHOUT index (`Seq Scan`, 49,531 rows removed by filter): Execution Time: **5.002 ms**

### Section 6 summary table (as printed by the script)

| Query | Index present (scan, time) | Index dropped (scan, time) | Speedup |
|---|---|---|---|
| Query 1 -- all orders for sale in the last 60s (composite B+ tree) | Bitmap Index Scan, 0.15 ms | Seq Scan, 14.63 ms | 98.9x |
| Query 2 -- accounts sharing a device_fingerprint (hash index) | Bitmap Index Scan, 1.29 ms | Seq Scan, 5.00 ms | 3.9x |

### Case study -- 3-way join, selection pushed down before the join

```
Query result:
            clean: 198071 units
    confirmed_bot: 2932 units
          cleared: 2714 units
          pending: 2598 units
```
Confirmed from the `EXPLAIN` plan: the `sale_id` + `created_at` filter is
applied directly on the `orders` scan node (a `Parallel Bitmap Heap Scan`
using `ix_orders_sale_id_created_at`) *before* the hash joins to
`order_items` and `flagged_orders` run -- the selection-before-join
heuristic. Full `EXPLAIN (ANALYZE, BUFFERS)` output for all three queries is
in `docs/_raw_logs/08b_demo8_indexing.log`.

---

## Section 9 -- NoSQL Component (Redis)

Covered by the Redis atomic DECR results already shown under Section 8.2
above (`demo_4_redis_atomic.py` and the "Redis atomic DECR" row of the
`demo_5_benchmark.py` table): 1 confirmed order out of 8 buyers for 1 unit
of stock, correctly, and the fastest of the three strategies in the 40-buyer
benchmark (388.2 req/s vs. 28.6 for pessimistic and 75.7 for optimistic)
because most "sold out" rejections never reach PostgreSQL at all.

The compensation path (Redis `DECR` succeeds, Postgres insert fails ->
`INCR` to give the unit back) is implemented in
`app/demos/demo_4_redis_atomic.py` and `app/main.py`'s `/checkout/redis`
endpoint but was not independently exercised in this pass (it requires
forcing a Postgres insert failure after a successful `DECR`, which none of
these runs triggered) -- noted here rather than claiming a number that
wasn't actually observed.

---

## Section 10.1 -- Demand Forecasting (`app/ml/demand_forecast.py`)

Trained on the real UCI "Online Retail" dataset (`data/online_retail.xlsx`,
~541k line items), after fixing the missing `openpyxl` dependency noted
above:

```
Loading real data from C:\Users\ramki\flashsale-engine\data\online_retail.xlsx ...
Demand Forecasting Model
==================================================
Data source: UCI Online Retail dataset (real data)
Training rows: 132,616   Test rows: 33,154
Mean Absolute Error: 21.84 units (next_day_qty)
R^2 score: 0.082

Feature importances:
  pre_period_demand            0.428
  day_of_week                  0.282
  category_popularity          0.188
  base_price                   0.043
  discount_pct                 0.038
  is_weekend                   0.021

Example predictions vs actual (first 5 test rows):
  predicted=67.1   actual=60.0
  predicted=79.0   actual=0.0
  predicted=1.5   actual=0.0
  predicted=27.6   actual=10.0
  predicted=18.1   actual=0.0

Use case: predicted next-period demand feeds the safety-stock buffer --
a SKU predicted at high volume should get pessimistic locking
(correctness under heavy contention); a SKU predicted at low volume
can safely use OCC (lower overhead, low contention).
```

---

## Section 10.2 -- Bot/Scalper Detection (`app/ml/bot_detection.py`)

### Synthetic evaluation harness (`train_and_evaluate()`)

Ground-truth labels exist only to score this synthetic demo -- never shown
to the (unsupervised) Isolation Forest model itself:

```
Bot/Scalper Detection Model (Isolation Forest, unsupervised)
============================================================
Total sessions: 1000  (bots: 100, humans: 900)
Flagged as anomalous: 100
  True bots caught:   83 / 100  (recall = 83.0%)
  False positives:    17  (precision = 83.0%)

Sample flagged sessions:
 checkout_latency_ms  session_duration_ms  accounts_per_device  requests_per_ip_per_min  is_actually_bot
          194.985997           530.844130                   10                       33                1
           61.959658           140.771243                   30                       35                1
        13724.651118         64139.417416                    2                        3                0
          500.000000          1237.186546                    2                        1                0
          181.580464           181.731965                   20                       20                1
         8609.415831         82115.665267                    2                        0                0

Why unsupervised: real bot-purchase labels are scarce in practice,
so Isolation Forest learns what 'normal' looks like and flags deviation,
rather than requiring a labeled training set we don't actually have.
```

### Real-data scoring pass (`score_sessions(db)`, via `scripts/run_bot_scoring.py`)

Run immediately after the `demo_5_benchmark.py` run captured in Section 8.2
above, scoring the real `UserBehaviorLog` rows that run produced (40
sessions, 6 from the shared-device-fingerprint bot cluster):

```
Bot/Scalper Detection -- real-data scoring pass
============================================================
Sessions scored: 40
Flagged as anomalous: 4
  ...of which tied to a real (successful) order and written to FlaggedOrder: 1

Flagged sessions:
                            order_id  checkout_latency_ms  session_duration_ms  accounts_per_device  requests_per_ip_per_min  anomaly_score
bb9853f5-1135-4b3f-ab12-18a9596ebd04                 93.0                   93                    6                       12       0.022157
```

`accounts_per_device=6` and `requests_per_ip_per_min=12` is exactly the
seeded bot-cluster signature (6 users sharing one `device_fingerprint`, all
checking out from the shared bot IP within the same minute). That order's
`status` flipped to `'flagged'` in the database.

**Run-to-run variance, honestly reported:** across 6 back-to-back
`demo_5_benchmark.py` + `run_bot_scoring.py` cycles run during this session,
the number of anomalous sessions tied to a *successful* order varied --
0 tied orders in 3 of the 6 runs, 1 in two runs, and 2 in one run (full logs:
`docs/_raw_logs/11` through `docs/_raw_logs/19`). This is expected, not a
bug: Isolation Forest flags the 4 most statistically unusual sessions out of
40 each time, and which of the 6 bot-cluster buyers actually won one of the
5 available units (out of 40 competing) is itself random per run. The result
captured above is from the run whose output is authoritative for this report
(`docs/_raw_logs/18_demo5_final_for_report.log` and
`docs/_raw_logs/19_run_bot_scoring_final.log`), and matches what the live API
returned (next section) since it's the run left in the database afterward.

### Live `GET /flagged-orders` response

`uvicorn app.main:app` started against the same database state immediately
after the run above, queried with `curl`:

```
$ curl -s http://127.0.0.1:8010/flagged-orders
```

First 5 of 50 rows returned (full raw response in
`docs/_raw_logs/21_flagged_orders_response.json`):

```json
[
  {
    "order_id": "bb9853f5-1135-4b3f-ab12-18a9596ebd04",
    "anomaly_score": 0.0222,
    "review_status": "pending"
  },
  {
    "order_id": "bdedd844-e41a-4708-ab56-06165d37fff9",
    "anomaly_score": 0.8806,
    "review_status": "pending"
  },
  {
    "order_id": "e54b4e5d-8fa7-4e61-a3bb-45725eb4d308",
    "anomaly_score": 0.5524,
    "review_status": "pending"
  },
  {
    "order_id": "b738f053-c3c7-457a-8f5e-463052324e9e",
    "anomaly_score": 0.892,
    "review_status": "pending"
  },
  {
    "order_id": "a6dd2cb1-534b-486c-b656-0eeb679d0aad",
    "anomaly_score": 0.8094,
    "review_status": "pending"
  }
]
```

The top row (`order_id bb9853f5-...`) is exactly the order the scoring pass
above just flagged, confirming the full path is wired end to end: checkout
-> `UserBehaviorLog` -> `score_sessions()` -> `FlaggedOrder` ->
`GET /flagged-orders`. (The remaining rows are older `flagged_orders`
entries from `scripts/seed_large.py`'s synthetic tagged data, which the
endpoint's `ORDER BY flagged_at DESC LIMIT 50` sorts after anything freshly
flagged.)

---

## Raw log index

| File | Contents |
|---|---|
| `00_seed.log` | `scripts.seed` initial run |
| `01_demo1_oversell.log` | Section 8.1 |
| `02_demo2_pessimistic.log` | Section 8.2 (pessimistic) |
| `03_demo3_optimistic.log` | Section 8.2 (OCC) |
| `04_demo4_redis.log` | Section 8.2 / 9 (Redis) |
| `05_demo5_benchmark.log`, `18_demo5_final_for_report.log` | Section 8.2 centerpiece table |
| `06_demo6_deadlock.log` | Section 8.4 |
| `07_demo7_recovery.log` | Section 7 |
| `08a_seed_large.log`, `08b_demo8_indexing.log` | Section 6 |
| `09_demo9_timestamp_ordering.log` | Section 8.3 |
| `10_demo10_multigranularity.log` | Section 8.5 |
| `11`-`17` (`demo5_retry*`, `run_bot_scoring_retry*`) | Section 10.2 variance runs (see honesty note above) |
| `19_run_bot_scoring_final.log` | Section 10.2 authoritative scoring pass |
| `20_uvicorn.log`, `21_flagged_orders_response.json` | Section 10.2 live API capture |
| `23_demand_forecast.log` | Section 10.1 |
| `24_bot_detection_synthetic.log` | Section 10.2 synthetic eval |
