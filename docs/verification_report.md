# Clean-Room Verification Report

Date: 2026-08-03
Host: Windows 11, Docker 29.5.2, Docker Compose v5.1.3, Python 3.12.7,
PostgreSQL 16 + Redis 7 (containers), all run from a wiped volume and a
freshly created virtualenv.

**Nothing in this file is expected or reconstructed output.** Every block
below is copied from a log captured during this pass. Full logs are in
[docs/_verify_logs/](_verify_logs/).

## Summary

| # | Item | Result |
|---|---|---|
| 1 | Fresh environment (`down -v` → `up -d` → healthy → new venv → pip install) | **PASS** (after 3 fixes) |
| 2 | `seed.py` + `seed_large.py` on fresh DB; index types verified in catalog | **PASS** |
| 3 | All 10 demos, README order, each checked against what it must prove | **PASS** (after 1 fix) |
| 4 | `demand_forecast.py` on real UCI data; bot scoring writes real `FlaggedOrder` rows | **PASS** (after 1 fix) |
| 5 | FastAPI: every endpoint in the root route, against seeded data | **PASS** (after 1 fix) |
| 6 | Every script re-runnable without manual DB surgery | **PASS** (after 2 fixes) |

Seven defects were found and fixed. Four of them would have stopped a
clean-room run outright; none were left as known issues. Details in
[Defects found and fixed](#defects-found-and-fixed) at the end.

---

## 1. Fresh environment — PASS

Volumes wiped, containers recreated, both healthy:

```
$ docker compose down -v
 Container flashsale-engine-postgres-1 Removed
 Container flashsale-engine-redis-1 Removed
 Volume flashsale-engine_pgdata Removed
 Network flashsale-engine_default Removed
=== EXIT: 0 ===

$ docker compose up -d --wait
 Container flashsale-engine-postgres-1 Healthy
 Container flashsale-engine-redis-1 Healthy
=== EXIT: 0 ===

flashsale-engine-postgres-1	Up 5 seconds (healthy)	0.0.0.0:5435->5432/tcp
flashsale-engine-redis-1	Up 5 seconds (healthy)	0.0.0.0:6390->6379/tcp
```

> **Fix 1 — no healthchecks existed.** `docker compose up -d --wait` cannot
> report "healthy" without a `healthcheck:` block, and the file had none, so
> "wait for Postgres/Redis to be healthy" was not actually possible as
> written. Added `pg_isready` and `redis-cli ping` healthchecks, and dropped
> the obsolete `version:` key that emitted a warning on every invocation.

venv recreated from scratch and dependencies installed clean:

```
$ rm -rf venv && python -m venv venv && pip install -r requirements.txt
Successfully installed Faker-40.36.0 Mako-1.3.12 MarkupSafe-3.0.3
SQLAlchemy-2.0.51 alembic-1.18.5 annotated-doc-0.0.5 annotated-types-0.8.0
anyio-4.14.2 click-8.4.2 colorama-0.4.6 et-xmlfile-2.0.0 fastapi-0.141.1
greenlet-3.5.4 h11-0.16.0 idna-3.18 joblib-1.5.3 numpy-2.5.1 openpyxl-3.1.5
pandas-3.0.5 psycopg2-binary-2.9.12 pydantic-2.13.4 pydantic-core-2.46.4
python-dateutil-2.9.0.post0 python-multipart-0.0.32 redis-8.1.0
scikit-learn-1.9.0 scipy-1.18.0 six-1.17.0 starlette-1.3.1
threadpoolctl-3.6.0 typing-extensions-4.16.0 typing-inspection-0.4.2
tzdata-2026.3 uvicorn-0.52.0
=== PIP EXIT: 0 ===
```

This is exactly what the clean-venv step is for — it caught a missing
dependency that a developer machine would have masked:

```
$ grep -rn "import docx" scripts/
scripts/build_report.py:10:from docx import Document

$ venv/Scripts/python.exe -c "import docx"
ModuleNotFoundError: No module named 'docx'
```

> **Fix 2 — `python-docx` missing from requirements.txt.**
> `scripts/build_report.py` imports it in six places. Added
> `python-docx==1.2.0`. Verified after the fix: the script now runs and
> regenerates `docs/project_report.docx` (321,642 bytes, exit 0).

> **Fix 3 — code defaults pointed at the wrong ports.** `docker-compose.yml`
> publishes Postgres on **5435** and Redis on **6390**, but `app/database.py`
> defaulted to `5432` and `app/main.py` / `demo_4` / `demo_5` defaulted to
> `6379`. Following the README verbatim therefore could not connect. Worse
> than failing: on this machine port 6379 was already held by an unrelated
> project's Redis container, so the Redis demos would have silently run
> against **another project's database**. Changed the defaults to match
> `docker-compose.yml` (env vars still override) and corrected the README.

## 2. Seed + schema verification — PASS

```
$ python -m scripts.seed
Creating tables...
Seeding users (including a device-fingerprint cluster for bot demo)...
Seeding product + flash sale with scarce stock...
Seed complete.
  product_id = 522a41c5-dad0-4bcf-a729-bf3d7d99ba06
  sale_id    = 19aa929e-affe-42f6-97ab-d5647e8b5ecb
  inventory total_stock = 5
```

The two Section 6 indexes exist **with the correct access method**, read
straight from the catalog (`pg_class` → `pg_am`), not inferred from the
script succeeding:

```sql
SELECT t.relname, i.relname, am.amname, pg_get_indexdef(i.oid)
FROM pg_class i JOIN pg_index ix ON ix.indexrelid=i.oid
JOIN pg_class t ON t.oid=ix.indrelid JOIN pg_am am ON am.oid=i.relam ...
```

```
     table_name     |            index_name            | index_type |                              definition
--------------------+----------------------------------+------------+-------------------------------------------------------------------
 order_items        | ix_order_items_order_id          | btree      | CREATE INDEX ... ON public.order_items USING btree (order_id)
 orders             | ix_orders_sale_id_created_at     | btree      | CREATE INDEX ... ON public.orders USING btree (sale_id, created_at)
 orders             | ix_orders_user_id                | btree      | CREATE INDEX ... ON public.orders USING btree (user_id)
 user_behavior_logs | ix_user_behavior_logs_order_id   | btree      | CREATE INDEX ... ON public.user_behavior_logs USING btree (order_id)
 user_behavior_logs | ix_user_behavior_logs_user_id    | btree      | CREATE INDEX ... ON public.user_behavior_logs USING btree (user_id)
 users              | ix_users_device_fingerprint_hash | hash       | CREATE INDEX ... ON public.users USING hash (device_fingerprint)
(6 rows)
```

Both spec requirements confirmed: `ix_orders_sale_id_created_at` is a
**composite btree on (sale_id, created_at)** in that column order, and
`ix_users_device_fingerprint_hash` is genuinely **`hash`**, not a btree
that merely happens to be named "hash".

Bulk seed on the fresh DB:

```
$ python -m scripts.seed_large        # elapsed 38s
seed_large complete.
  products = 8, sales = 6
  users = 50,000  (hottest device_fingerprint shared by 499 accounts)
  orders = 200,000, order_items = 399,742, flagged_orders = 8,000
```

## 3. The ten demos — PASS

Run in README order. Each is judged against what it must prove, not
against "no traceback". Logs: `docs/_verify_logs/01..10_*.log`.

### demo_1_oversell — PASS (bug reproduces)
Requirement: **more than 1** confirmed order for 1 unit.

```
Firing 8 concurrent checkout attempts at 1 unit of stock (no locking)...
  buyer 0: CONFIRMED order 4171c647-2630-4d1f-aae9-e95c6944edce
  ... (all 8 CONFIRMED) ...
Stock available was 1. Confirmed orders: 8
OVERSOLD -- this is the lost-update problem in action.
```

8 confirmed for 1 unit. The race genuinely reproduces; the demo is not
silently passing.

### demo_2 / demo_3 / demo_4 — PASS (exactly 1 confirmed, 0 oversold each)

```
demo_2 (SELECT FOR UPDATE):  Stock available was 1. Confirmed orders: 1 (elapsed 0.429s)
demo_3 (version column OCC): Stock available was 1. Confirmed orders: 1 (elapsed 0.171s)
demo_4 (Redis atomic DECR):  Stock available was 1. Confirmed orders: 1 (elapsed 0.034s)
```

In each, the other 7 buyers were rejected — verified in the per-buyer
lines of each log, not just the total.

### demo_5_benchmark — PASS (0 oversold across all three, real numbers)

```
Strategy                                Confirmed  Oversold  Retries  Time(s)    Req/s
--------------------------------------------------------------------------------------
Pessimistic (SELECT FOR UPDATE)                 5         0        0    1.343     29.8
Optimistic (version column)                     5         0      120    0.309    129.6
Redis atomic DECR                               5         0        0    0.075    530.9

Stock was 5, contested by 40 buyers (8x oversubscribed).
```

All three correct (Oversold = 0), 5 confirmed against 5 units, and the
three-way table prints real measured values. Timings are machine-specific
and differ from the sample in the README, as that README already warns.

### demo_6_deadlock — PASS (real deadlock, resolved, no hang)

```
PART A:
  T1: ABORTED by PostgreSQL deadlock detector: (psycopg2.errors.DeadlockDetected) deadlock detected

PART B:
  T2(younger): wounded before acquiring Inventory, aborting and releasing held locks
  T1(older): completed successfully, holding ['Inventory', 'User']
```

A genuine `psycopg2.errors.DeadlockDetected` from Postgres's own detector
(not a simulated message), then wound-wait prevention avoids the cycle.
Run under a 180s kill-timer; it completed on its own, so no hang.

### demo_7_recovery — PASS (inconsistent without the wrapper, restored with it)

```
PART A -- WITHOUT a transaction wrapper (broken)
reserved_stock before: 0
  -> CRASH before the order INSERT runs
reserved_stock after 'crash': 1  <-- stock is gone, no order exists for it!

PART A continued -- WITH a transaction wrapper (correct)
  -> ROLLBACK undid the increment automatically
reserved_stock after rollback: 0  <-- correctly restored

PART B -- WAL undo
   Recovery reads WAL: before=0, after=1, committed=false
   reserved_stock after recovery: 0  <-- correctly restored via UNDO
```

Stock is demonstrably stranded at 1 without the wrapper and restored to 0
with it — a real difference, both directions shown.

### demo_8_indexing — PASS (real scan-type and timing differences)

Not two identical plans. With the index, then the same query with the
index dropped:

```
Query 1 -- WITH index:
  Bitmap Index Scan on ix_orders_sale_id_created_at
  Execution Time: 0.123 ms

Query 1 -- WITHOUT index:
  Parallel Seq Scan on orders  (rows Removed by Filter: ...)
  Execution Time: 12.332 ms

Query 2 -- WITH index:
  Bitmap Index Scan on ix_users_device_fingerprint_hash  (rows=499)
  Execution Time: 2.590 ms

Query 2 -- WITHOUT index:
  Seq Scan on users  (Rows Removed by Filter: 49531)
  Execution Time: 13.628 ms
```

```
| Query | Index present (scan, time) | Index dropped (scan, time) | Speedup |
|---|---|---|---|
| Query 1 -- composite B+ tree | Bitmap Index Scan, 0.12 ms | Seq Scan, 12.33 ms | 100.3x |
| Query 2 -- hash index        | Bitmap Index Scan, 2.59 ms | Seq Scan, 13.63 ms | 5.3x |
```

Different node types and ~100x / ~5x timing separation against 200k orders
and 50k users. The demo drops and recreates each index itself, so it is
repeatable.

### demo_9_timestamp_ordering — PASS (a real rollback *and* a Thomas's-Rule skip)

```
  [T2 TS=2] READ   -> ALLOWED   R-TS updated to 2; sees 1 unit(s) available
  [T3 TS=3] WRITE  -> ALLOWED   W-TS updated to 3; unit reserved
  [T2 TS=2] WRITE  -> SKIPPED   TS(2) < W-TS(3) but no reader saw a TS in between --
                                Thomas's Write Rule: obsolete write ignored, no rollback needed
  [T1 TS=1] READ   -> REJECTED  TS(1) < W-TS(3) -- would read a value already overwritten

Without Thomas's Write Rule, Step 3 would have gone differently:
  [T2 TS=2] WRITE  -> REJECTED  TS(2) < W-TS(3) -- strict Basic T/O aborts T2

Final inventory state: total_stock=1, reserved_stock=1 -- exactly 1 unit sold (to T3)
```

Both required cases present: T1's read is genuinely **REJECTED**, and T2's
write is **SKIPPED** by Thomas's Rule with the strict-T/O counterfactual
printed alongside for contrast.

### demo_10_multigranularity — PASS (checkouts really blocked, real pg_locks)

```
  pg_locks JOIN pg_class, filtered to the inventory table, taken WHILE checkouts are blocked:
  locktype   mode                 granted  relname    pid      state
  ----------------------------------------------------------------------
  relation   AccessExclusiveLock  True     inventory  618      idle in transaction
  relation   RowShareLock         False    inventory  619      active
  relation   RowShareLock         False    inventory  620      active
  relation   RowShareLock         False    inventory  621      active

  [admin]      table-level ACCESS EXCLUSIVE lock acquired -- sale is now PAUSED
  [checkout-0] lock GRANTED after waiting 1.163s -- was blocked by the admin's table lock
  [checkout-1] lock GRANTED after waiting 1.165s -- was blocked by the admin's table lock
  [checkout-2] lock GRANTED after waiting 1.167s -- was blocked by the admin's table lock
```

`granted = False` on three distinct backend PIDs is Postgres itself
reporting the checkouts as blocked, and each waited ~1.16s for the admin
to release. Real contention, real catalog output.

## 4. Machine learning — PASS

### demand_forecast.py — trains on the real UCI dataset, not the fallback

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
```

It reports `real data` explicitly and names the target `next_day_qty`
(the real-data target; the synthetic path's target is `orders_first_60s`),
so the fallback is confirmed **not** silently in use. There is no
"falling back to synthetic" warning in the log. R² = 0.082 is genuinely
low, and that is the real number.

### bot_detection.py — synthetic evaluation harness

```
Bot/Scalper Detection Model (Isolation Forest, unsupervised)
Total sessions: 1000  (bots: 100, humans: 900)
Flagged as anomalous: 100
  True bots caught:   83 / 100  (recall = 83.0%)
  False positives:    17  (precision = 83.0%)
```

Meets the README's "~80%+ recall" claim. This is the labelled synthetic
harness only — it does not touch the database.

### run_bot_scoring.py — real rows, tied to real orders

```
Bot/Scalper Detection -- real-data scoring pass
Sessions scored: 40
Flagged as anomalous: 10
  ...of which tied to a real (successful) order and written to FlaggedOrder: 2

Flagged sessions:
                            order_id  checkout_latency_ms  session_duration_ms  accounts_per_device  requests_per_ip_per_min  anomaly_score
88e24e0f-c0c2-4ec0-960f-9bc06a0eb44c              12730.0                12730                    1                        2       0.072565
b501a886-c22f-4168-935f-4dfcfaf19b03                289.0                  289                    6                       12       0.009310
```

Confirmed in the database — not just printed. Joined back to `orders` and
`users` to prove the `FlaggedOrder` rows reference real orders:

```
               order_id               | anomaly_score | review_status | order_status |       email       | accts_per_device
--------------------------------------+---------------+---------------+--------------+-------------------+------------------
 88e24e0f-c0c2-4ec0-960f-9bc06a0eb44c |        0.0726 | pending       | flagged      | user6@example.com |                1
 b501a886-c22f-4168-935f-4dfcfaf19b03 |        0.0093 | pending       | flagged      | user3@example.com |                6
(2 rows)
```

Both joins succeed, and both parent orders had `status` flipped to
`flagged`. The second row carries the exact bot-cluster signature
(`accounts_per_device = 6`, `requests_per_ip_per_min = 12`, 289 ms
session). The first is a 12.7-second single-account session — a false
positive, which is honest behaviour for an unsupervised detector and is
now stated as such in the README.

> **Fix 4 — this step used to produce zero rows about half the time.**
> See [Defect 6](#defect-6) below.

## 5. FastAPI endpoints — PASS

`uvicorn app.main:app --port 8010` (port 8000 was occupied on this host by
an unrelated container; no code depends on the port). Every endpoint
advertised by the root route was exercised against seeded data.

| Endpoint | Method | Status | Verified against DB |
|---|---|---|---|
| `/` | GET | 200 | lists 7 endpoints |
| `/sales/{sale_id}` | GET | 200 | matches `inventory` row exactly |
| `/sales/{bogus}` | GET | 404 | `{"detail":"Sale not found"}` |
| `/checkout/pessimistic` | POST | 200 | `available` 5 → 4 |
| `/checkout/optimistic` | POST | 200 | `available` 4 → 3, `version` 0 → 1 |
| `/checkout/redis` | POST | 200 | Redis counter 3 → 2 |
| `/flagged-orders` | GET | 200 | returns the 2 real rows above |
| `/demand-forecast` | GET | 200 | real-data source + metrics |
| `/dashboard` | GET | 200 | 7,603 bytes `text/html` |
| `/docs` | GET | 200 | Swagger UI |

The JSON tracks database state through each checkout — this is the live
capture, stock decrementing as orders are placed:

```
### GET /sales/bca577e1-e2a7-42ca-8454-690dc73bef83
{"product":"Limited Edition Sneaker","category":"footwear","sale_price":99.0,
 "total_stock":5,"reserved_stock":0,"available":5,"version":0} [HTTP 200]

available BEFORE any checkout = 5

### POST /checkout/pessimistic
{"status":"confirmed","order_id":"340a44ff-...","strategy":"pessimistic"} [HTTP 200]
available AFTER pessimistic = 4

### POST /checkout/optimistic
{"status":"confirmed","order_id":"492a848e-...","strategy":"optimistic"} [HTTP 200]
available AFTER optimistic = 3

### POST /checkout/redis
{"status":"confirmed","order_id":"0ce5cffa-...","strategy":"redis"} [HTTP 200]
redis counter stock:bca577e1-... = 2

### GET /sales/... (after 3 checkouts)
{"product":"Limited Edition Sneaker",...,"total_stock":5,"reserved_stock":2,
 "available":3,"version":1} [HTTP 200]
```

Sold-out behaviour also returns the right codes (captured earlier in the
pass, when the sale had been exhausted by demo_9):

```
pessimistic  -> {"detail":"Sold out"} [HTTP 409]
optimistic   -> {"detail":"Sold out"} [HTTP 409]
redis        -> {"detail":"Sold out (Redis fast-path)"} [HTTP 409]
```

All three checkout endpoints wrote their own `UserBehaviorLog` row, the
Section 10.2 path the README describes:

```
                  id                  |  status   |       email       | has_behavior_log |  ip_address
--------------------------------------+-----------+-------------------+------------------+---------------
 340a44ff-d94d-443d-b8bb-340e9d6acf0f | confirmed | user0@example.com | t                | 203.0.113.154
 492a848e-54f1-4355-b8b7-d348c4b4467f | confirmed | user1@example.com | t                | 203.0.113.174
 0ce5cffa-a184-454f-9825-ea8978e5ffaf | confirmed | user2@example.com | t                | 203.0.113.133
```

`/flagged-orders` is **not** always empty — after a scoring pass it returns
the real rows:

```json
[{"order_id":"88e24e0f-c0c2-4ec0-960f-9bc06a0eb44c","anomaly_score":0.0726,"review_status":"pending"},
 {"order_id":"b501a886-c22f-4168-935f-4dfcfaf19b03","anomaly_score":0.0093,"review_status":"pending"}]
```

(It correctly returns `[]` *before* any scoring pass has run — that is the
accurate reflection of an empty `flagged_orders` table, not a bug.)

**One behaviour worth stating plainly, since it looks like a discrepancy:**
`/checkout/redis` does not change `inventory.reserved_stock`, so `/sales`
still shows `available = 3` after a successful Redis checkout. That is
deliberate and consistent with `demo_4_redis_atomic.py`, which also never
touches `reserved_stock` — the whole point of the fast path is that it
keeps the hot-path counter in Redis and only writes the order row to
Postgres. It is not a bug, but a report should describe it accurately
rather than claiming Redis checkouts decrement the SQL inventory.

## 6. Idempotency / re-runnability — PASS

| Script | Re-runnable? | Evidence |
|---|---|---|
| `scripts/seed.py` | Yes — self-resetting | TRUNCATEs all 9 tables; re-run over 200k orders took **2s** |
| `scripts/seed_large.py` | Yes — self-resetting | 2nd consecutive run took **55s**, cleaning its own 200k rows |
| `demo_1` … `demo_4` | Yes | each resets `inventory` first; run twice each this pass |
| `demo_5_benchmark` | Yes | run 8× this pass; resets its own orders/logs/flags per strategy |
| `demo_6`, `demo_7`, `demo_9`, `demo_10` | Yes | reset their own rows; run twice each |
| `demo_8_indexing` | Yes | drops and recreates each index itself; run twice |
| `scripts/run_bot_scoring.py` | Yes | run 8× this pass |
| `app/ml/demand_forecast.py` | Yes | reads the xlsx, writes nothing to the DB |
| `app/ml/bot_detection.py` | Yes | synthetic harness, writes nothing to the DB |
| `scripts/build_report.py` | Yes | regenerates `docs/project_report.docx` deterministically |

Two ordering constraints, now documented in the README rather than left
for a grader to discover:

1. **`seed.py` wipes everything, including `seed_large.py`'s 200k rows.**
   Verified: `orders` went 200,005 → 0 and `users` 50,030 → 30 on re-seed.
   If you re-seed, re-run `seed_large` before `demo_8_indexing`.
2. **`seed.py` mints new UUIDs each run** and rewrites `scripts/seed_ids.py`,
   so a saved `sale_id` from a previous run stops resolving.

---

## Defects found and fixed

Each was fixed at the source and re-run until it genuinely passed.

### Defect 1 — `docker-compose.yml` had no healthchecks
Neither service defined one, so `--wait` could not report health.
**Fix:** added `pg_isready` / `redis-cli ping` healthchecks; removed the
obsolete `version:` key. *(`docker-compose.yml`)*

### Defect 2 — `python-docx` missing from `requirements.txt`
`scripts/build_report.py` imported it in 6 places; a clean venv raised
`ModuleNotFoundError`. Only visible because the venv was rebuilt from
scratch. **Fix:** pinned `python-docx==1.2.0`. *(`requirements.txt`)*

### Defect 3 — port defaults contradicted `docker-compose.yml`
Code defaulted to 5432/6379; compose publishes 5435/6390. The README's
setup steps could not work as written, and on a host where 6379 is already
taken the Redis demos would have silently used a **different project's
Redis**. **Fix:** defaults now match compose; README corrected.
*(`app/database.py`, `app/main.py`, `demo_4`, `demo_5`, `README.md`)*

### Defect 4 — `seed.py` could not be re-run after `seed_large.py` (hang)
The 9 ORM `DELETE`s made Postgres run a per-row FK check for every deleted
order. Cancelled after **5+ minutes** with no end in sight; the traceback
names the exact culprit:

```
CONTEXT:  SQL statement "SELECT 1 FROM ONLY "public"."order_items" x
                         WHERE $1 OPERATOR(pg_catalog.=) "order_id" FOR KEY SHARE OF x"
[SQL: DELETE FROM orders]
sqlalchemy.exc.OperationalError: (psycopg2.errors.QueryCanceled) canceling statement due to user request
```

200k orders × a seq scan of a 400k-row child table (whose just-deleted rows
are still present as un-vacuumed dead tuples) is quadratic.
**Fix:** replaced the deletes with a single
`TRUNCATE ... RESTART IDENTITY CASCADE`. Re-run now takes **2 seconds**.
*(`scripts/seed.py`)*

### Defect 5 — four foreign keys had no index
Root cause behind Defect 4, and it also made `seed_large.py`'s own cleanup
un-re-runnable. Postgres indexes the *referenced* key automatically but
never the *referencing* column:

```
    child_table     | fk_column | parent_table | has_leading_index
--------------------+-----------+--------------+-------------------
 orders             | user_id   | users        | f
 order_items        | order_id  | orders       | f
 user_behavior_logs | order_id  | orders       | f
 user_behavior_logs | user_id   | users        | f
```

**Fix:** declared `ix_orders_user_id`, `ix_order_items_order_id`,
`ix_user_behavior_logs_order_id`, `ix_user_behavior_logs_user_id`. All five
FKs now report `has_leading_index = t`. A second `seed_large` run dropped
from "never finishes" to **55s**. Re-verified that `demo_8_indexing` still
shows its intended Seq-Scan-vs-Index-Scan contrast afterwards (it does —
100.3x and 5.3x). *(`app/models.py`)*

### Defect 6 — `/checkout/redis` was permanently "sold out" <a id="defect-6"></a>
Nothing initialised the Redis counter for a newly seeded sale. `DECR` on a
missing key yields `-1`, so every request failed the `remaining < 0` check
even with 5 units in Postgres. Confirmed: after a seed, Redis held only
stale keys from *previous* sales and none for the current `sale_id`.
**Fix:** the endpoint now lazily seeds the counter from
`inventory.total_stock - reserved_stock` using an atomic `SET NX` before
the `DECR`. Verified: `/checkout/redis` returns 200 and the counter moves
3 → 2. *(`app/main.py`)*

### Defect 7 — bot scoring often wrote zero `FlaggedOrder` rows
`score_sessions()` hardcoded `contamination=0.1`, capping it at 4 flags
over a 40-session batch — while only ~5 sessions in that batch have an
`order_id` and are therefore flaggable at all. Whether any flag landed on a
flaggable session was close to a coin toss, and one observed run produced
**0 rows**, leaving `GET /flagged-orders` empty and contradicting the
README. The 0.1 value is correct for the *synthetic* harness, where the
10% rate is known by construction, but it is not transferable to a real
batch.
**Fix:** the two values are now separate named constants —
`SYNTHETIC_HARNESS_BOT_FRACTION = 0.1` (known by construction) and
`ASSUMED_REAL_SCALPER_RATE = 0.25` (an asserted prior, **not** fitted or
measured — the real path has no ground-truth labels, which is why it uses
unsupervised detection at all). The scoring pass now prints the value it
ran under, so a flag count cannot be quoted without its condition.

A note on this report's own earlier wording: a previous revision justified
0.25 by observing that a demo batch is "~30% shared-device sessions (12 of
40 measured)". That justification has been withdrawn. Those sessions are
identifiable as bot-cluster members only because `scripts/seed.py` created
them — that is ground truth, and using it to choose the value would smuggle
labels into the one path whose premise is that labels do not exist. The
value is a disclosed assumption, and nothing more.

Verified over 5 consecutive `demo_5` → `run_bot_scoring` cycles:

```
run 1: FlaggedOrder rows written=1   flagged_orders in DB=1
run 2: FlaggedOrder rows written=2   flagged_orders in DB=2
run 3: FlaggedOrder rows written=2   flagged_orders in DB=2
run 4: FlaggedOrder rows written=1   flagged_orders in DB=1
run 5: FlaggedOrder rows written=1   flagged_orders in DB=1
```

5/5 now write at least one real row. *(`app/ml/bot_detection.py`)*

---

## Documentation corrections

Not code defects, but claims that did not match observed behaviour:

- **README §10.2 overstated determinism.** It presented one run's flagged
  session as the expected result. Which sessions get flagged varies per
  run, and false positives (slow human sessions) are normal for an
  unsupervised detector. Rewritten with this pass's real output plus an
  explicit caveat, and a warning not to quote a precision/recall figure for
  the real-data path — there are no ground-truth labels for it. The 83%
  recall belongs only to the synthetic harness.
- **README setup section** claimed no env vars were needed while the code
  defaults pointed elsewhere (Defect 3). Now accurate.
- **"Each script resets its own test data … any order"** was true per script
  but hid the `seed.py` ⊃ `seed_large.py` relationship. Now documented.

## Minor observation, not fixed

`app/__pycache__/*.pyc` files are tracked in git (they appear as modified
in `git status`). Harmless to the verification, but `__pycache__/` would
normally belong in `.gitignore`. Left alone as it is outside the scope of
this pass.

---

## Reproducing this run

```bash
docker compose down -v && docker compose up -d --wait
rm -rf venv && python -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt
venv/Scripts/python.exe -m scripts.seed
# demos 1-7, then seed_large, then 8-10, per the README order
venv/Scripts/python.exe -m app.ml.demand_forecast
venv/Scripts/python.exe -m app.demos.demo_5_benchmark
venv/Scripts/python.exe -m scripts.run_bot_scoring
venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

Raw logs for every step: [docs/_verify_logs/](_verify_logs/).
