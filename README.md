# Flash-Sale Inventory & Oversell Prevention Engine

A working implementation of the DBMS course project design doc: concurrency
control, indexing, recovery, NoSQL, and ML — all runnable and verified end
to end (every demo below was actually executed against a live PostgreSQL +
Redis instance while this was built, not just written).

## What's here

```
app/
  database.py          SQLAlchemy engine/session setup
  models.py             ER schema: User, Product, FlashSaleEvent, Inventory,
                         Order, OrderItem, UserBehaviorLog, FlaggedOrder,
                         StockAuditLog. Section 6 indexes are declared as
                         explicit Index() entries in __table_args__ (not
                         column-level index=True): a composite B+ tree index
                         on Order(sale_id, created_at) for "orders for this
                         sale in the last N seconds", and a hash index
                         (postgresql_using="hash") on
                         User.device_fingerprint for exact-match lookups
                         only.
  main.py                FastAPI app (checkout endpoints, sale info, flagged orders)
  demos/
    demo_1_oversell.py        the lost-update problem, unguarded (Section 8.1)
    demo_2_pessimistic.py     SELECT FOR UPDATE fix (Section 8.2)
    demo_3_optimistic.py      version-column OCC fix (Section 8.2)
    demo_4_redis_atomic.py    Redis atomic DECR fast path (Section 9)
    demo_5_benchmark.py       THE centerpiece: 3-way comparison table (Section 8.2)
    demo_6_deadlock.py        real Postgres deadlock + wound-wait prevention (Section 8.4)
    demo_7_recovery.py        ACID + WAL undo/redo demo (Section 7)
    demo_8_indexing.py        EXPLAIN ANALYZE before/after each Section 6 index (Section 6)
    demo_9_timestamp_ordering.py  Basic T/O + Thomas's Write Rule, app-level protocol (Section 8.3)
  ml/
    demand_forecast.py        GradientBoosting demand model (Section 10.1)
    bot_detection.py          Isolation Forest bot detection (Section 10.2)
scripts/
  seed.py                creates tables + seeds a scarce-stock flash sale
  seed_large.py          bulk-seeds ~200k orders (COPY, not the ORM) for demo_8_indexing.py
docker-compose.yml       Postgres + Redis for your own machine
requirements.txt
```

## Setup (on your own machine)

```bash
docker compose up -d          # starts Postgres + Redis
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m scripts.seed         # creates tables, seeds demo data
```

Set environment variables if your Postgres isn't on localhost defaults
(`POSTGRES_HOST`, `POSTGRES_PASSWORD`, etc. — see `app/database.py`).

## Running the demos, in the order they map to your report

```bash
python -m app.demos.demo_1_oversell       # expect: MORE than 1 confirmed order for 1 unit
python -m app.demos.demo_2_pessimistic    # expect: exactly 1 confirmed, slower
python -m app.demos.demo_3_optimistic     # expect: exactly 1 confirmed, retry count shown
python -m app.demos.demo_4_redis_atomic   # expect: exactly 1 confirmed, fastest
python -m app.demos.demo_5_benchmark      # THE comparison table for your report
python -m app.demos.demo_6_deadlock       # expect: a real Postgres deadlock, then wound-wait avoiding one
python -m app.demos.demo_7_recovery       # expect: broken-without-transaction vs correct-with-transaction
python -m scripts.seed_large              # one-time: bulk-seeds ~200k orders for demo_8 (takes a minute or two)
python -m app.demos.demo_8_indexing       # expect: Seq Scan without each index, Index/Bitmap/Hash Scan with it
python -m app.demos.demo_9_timestamp_ordering  # expect: one rollback, one Thomas's-Rule skip, one confirmed
python -m app.ml.demand_forecast          # expect: R^2 > 0.9, feature importances
python -m app.ml.bot_detection            # expect: ~80%+ recall catching synthetic bots
```

Each script resets its own test data, so they can be run in any order or
repeatedly for a live demo.

## Sample result actually produced by demo_5_benchmark.py

(40 buyers competing for 5 units, ~30ms simulated work per request)

| Strategy | Confirmed | Oversold | Retries | Time(s) | Req/s |
|---|---|---|---|---|---|
| Pessimistic (SELECT FOR UPDATE) | 5 | 0 | 0 | 1.467 | 27.3 |
| Optimistic (version column) | 5 | 0 | 72 | 0.489 | 81.9 |
| Redis atomic DECR | 5 | 0 | 0 | 0.043 | 938.1 |

All three are correct (0 oversold). The difference is entirely in *how*
they get there: pessimistic locking serializes and queues (slowest but
simplest to reason about), OCC is faster but burns 72 retries under this
much contention (the counter-intuitive result worth highlighting in your
report), and Redis is by far the fastest because most "sold out"
rejections never touch PostgreSQL at all. Paste this table directly into
Section 8.2 of your report — regenerate it yourself for your own numbers,
since exact timings depend on your machine.

## Running the API

```bash
uvicorn app.main:app --reload --port 8000
```

Visit `http://localhost:8000/docs` for interactive Swagger docs. Get a
`sale_id` and `user_id` from `scripts/seed_ids.py` (generated after you run
the seed script) to try the checkout endpoints.

## What's still worth adding before submission

- Wire the `FlaggedOrder` table to actually receive output from
  `bot_detection.py` (currently the ML scripts run standalone for clarity —
  connecting them to live `UserBehaviorLog` rows is a natural next step
  and a good thing to show progress on across your build weeks)
- Swap `demand_forecast.py`'s synthetic data generator for a real dataset
  if you want to strengthen Section 10.1 further (Kaggle's "Online Retail
  Dataset" is a good fit; the model code doesn't need to change, just the
  data loader)
- Section 6 is fully covered now: `scripts/seed.py` creates the composite
  B+ tree index on `Order(sale_id, created_at)` and the hash index on
  `User.device_fingerprint`, `scripts/seed_large.py` bulk-loads ~200k
  orders to make them worth measuring, and `demo_8_indexing.py` proves
  each one is used via before/after `EXPLAIN ANALYZE`
