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
    demo_10_multigranularity.py   table-level pause vs row-level checkouts, pg_locks + IS/IX/S/X mapping (Section 8.5)
  ml/
    demand_forecast.py        GradientBoosting demand model, trained on real
                               data by default (Section 10.1) -- see below
    bot_detection.py          Isolation Forest bot detection (Section 10.2).
                               score_sessions(db) is the real-data path --
                               scores actual UserBehaviorLog rows and writes
                               FlaggedOrder / Order.status; train_and_evaluate()
                               is still the synthetic demo/eval harness.
scripts/
  seed.py                creates tables + seeds a scarce-stock flash sale under
                          FIXED demo UUIDs (stable across reseeds); owns only
                          its own canonical rows + 30 demo users
  seed_large.py          bulk-seeds ~200k orders (COPY, not the ORM) onto that
                          same sale for demo_8_indexing.py; purely additive,
                          owns only its @indexdemo.example users' rows
  run_bot_scoring.py     batch job: scores real UserBehaviorLog rows via
                          bot_detection.score_sessions() -- run after a burst
                          of checkout traffic, see Section 10.2 below
data/
  online_retail.xlsx      UCI "Online Retail" dataset -- NOT committed (data/*.xlsx
                           is gitignored, it's ~23MB); download it yourself, see below
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

No environment variables needed for the standard setup: `docker-compose.yml`
publishes Postgres on **5435** and Redis on **6390** (deliberately off the
stock 5432/6379 so it can't collide with — or silently connect to — another
Postgres/Redis already running on your machine), and `app/database.py` /
`app/main.py` default to those same ports. Override with `POSTGRES_HOST`,
`POSTGRES_PORT`, `POSTGRES_PASSWORD`, `REDIS_HOST`, `REDIS_PORT` if your
setup differs.

To train `demand_forecast.py` on real data (the default; see Section 10.1
below), download the UCI "Online Retail" dataset and save it exactly here:

```bash
mkdir -p data
curl -L -o data/online_retail.xlsx \
  "https://archive.ics.uci.edu/ml/machine-learning-databases/00352/Online%20Retail.xlsx"
```

If you skip this, `demand_forecast.py` still runs -- it prints a warning
and falls back to its synthetic demo generator instead.

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
python -m app.demos.demo_10_multigranularity   # expect: checkouts visibly blocked, pg_locks shows the wait
python -m app.ml.demand_forecast          # trains on real data if data/online_retail.xlsx exists,
                                           # else falls back to synthetic with a printed warning
python -m app.ml.bot_detection            # expect: ~80%+ recall catching synthetic bots (synthetic eval demo)
python -m scripts.run_bot_scoring         # scores the REAL sessions demo_5_benchmark just logged --
                                           # expect: at least one flagged session tied to a real order
```

**`seed.py` and `seed_large.py` are both safe to run, in either order, any
number of times, without wiping each other's data.** Every demo resets its
own test data too, so the whole set can be run in any order or repeatedly
for a live demo. The one hard requirement:

- **`demo_8_indexing.py` needs `scripts/seed_large.py` to have been run at
  least once** — it has nothing to measure against otherwise. It says so
  rather than failing obscurely.
- `scripts/seed_large.py` needs `scripts/seed.py` to have been run at least
  once, since its bulk orders attach to the canonical sale. It checks and
  tells you. Beyond that first run, order doesn't matter.

### How they stay out of each other's way

Both scripts write to the same tables, so each owns a disjoint slice and
touches nothing else. The rule is **a row belongs to whoever owns its
`user_id`**:

| Script | Owns |
|---|---|
| `scripts/seed.py` | the fixed-UUID canonical rows, the 30 demo users (`user0..29@example.com`), and any orders/items/logs/flags belonging to **those users** |
| `scripts/seed_large.py` | its ~50k synthetic users (`@indexdemo.example`) and everything belonging to **those users** |

`seed_large.py`'s bulk orders deliberately target the same `DEMO_SALE_ID`
as everything else, so `demo_8_indexing` measures its indexes against the
very sale the other demos use. That's precisely why nothing in this project
deletes by `sale_id` — the two scripts share a sale, so a sale-scoped
`DELETE` would cross the boundary. Re-running either script replaces only
its own slice.

### The demo IDs are constants

`scripts/seed.py` uses hardcoded UUIDs for the canonical entities, so a
`sale_id` or `user_id` pasted into Swagger/Postman **stays valid across
reseeds**:

```
product_id   = 11111111-1111-1111-1111-111111111111
sale_id      = 22222222-2222-2222-2222-222222222222
inventory_id = 33333333-3333-3333-3333-333333333333
user_ids     = d0000000-0000-4000-8000-000000000000 ... -000000000029
```

Re-running `seed.py` upserts these rather than erroring on a duplicate key,
restores stock to 5, and clears the sale's Redis counter so the
`/checkout/redis` fast path isn't left thinking it's sold out. Bulk and
synthetic rows still use random UUIDs — only the handful of entities a
human actually copies needs to be stable.

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

## Section 10.1 -- demand_forecast.py now trains on real data

`data/online_retail.xlsx` is the UCI "Online Retail" dataset (~541k line
items, a UK-based online gift retailer, Dec 2010-Dec 2011) -- the same
dataset Kaggle mirrors. `load_real_data()` cleans it (drops cancelled
orders, missing `CustomerID`, non-positive `Quantity`/`UnitPrice`),
restricts to the 500 best-selling SKUs so each item has enough trading
history for a stable rolling feature, and reframes it as: predict a SKU's
units sold on the next calendar day from price, an implied discount,
item popularity, and calendar effects.

Sample result actually produced by `python -m app.ml.demand_forecast`
against the real dataset:

```
Training rows: 132,616   Test rows: 33,154
Mean Absolute Error: 21.84 units (next_day_qty)
R^2 score: 0.082

Feature importances:
  pre_period_demand            0.426
  day_of_week                  0.286
  category_popularity          0.188
  base_price                   0.044
  discount_pct                 0.038
  is_weekend                   0.016
```

Be upfront about this in your report: R^2 = 0.082 is real. But don't stop
at "real data is noisy" -- the script measures *why*. Every run prints a
sanity control that retrains the same model code on the synthetic
generator, and `--framings` runs a full comparison:

```bash
python -m app.ml.demand_forecast --framings
```

```
Framing                                  Kind                  R^2        MAE       rows
----------------------------------------------------------------------------------------
next-day units (SHIPPED framing)         forecast            0.082      21.84    165,770
next-7-day total units                   forecast            0.270      94.32    160,281
same-day units (NOWCAST, diagnostic)     not a forecast      0.319      17.23    166,269
next-week units (weekly panel)           forecast            0.259      90.16     23,205
same-week units (NOWCAST, diagnostic)    not a forecast      0.484      75.26     23,704
SYNTHETIC generator (control)            control             0.941       4.06      2,000
```

The honest Section 10.1 story is a data-framing mismatch, not a modelling
failure:

- **The pipeline works.** Identical code scores 0.941 on synthetic data.
- **But 0.941 is a ceiling by construction, not an achievement** -- those
  labels *are* a known formula of those features plus Gaussian noise, so
  recovering it only proves the training/eval path is sound. Use it as a
  control; never quote it as this project's forecasting accuracy.
- **UCI Online Retail has no flash-sale events at all.** It's a year of
  ordinary invoice lines, so "units in the first 60 seconds" isn't
  answerable from it -- next-day units is the nearest substitute, and that
  substitution is what caps the score.
- **~51% of SKU-days have zero sales**, so the daily target is a
  spike-and-zero series dominated by arrival timing. Coarsening the
  horizon roughly triples R^2 (0.270 for a 7-day total, 0.259 next-week).
- **The nowcast rows aren't shippable models** (predicting today needs
  today's sales) -- they're diagnostics showing the features do carry
  signal. It's the step *forward in time* this dataset resists.

R^2 isn't comparable across those rows (different variance denominators),
so read it as which questions the data can answer, not a leaderboard.

**Feature name mapping (real vs. synthetic) -- describe this honestly in
your report, since the two datasets don't have identical columns:**

| Synthetic (`generate_synthetic_fallback_data`) | Real (`load_real_data`) | Real proxy for what it represents |
|---|---|---|
| `base_price` | `base_price` | mean `UnitPrice` for that SKU on that day |
| `discount_pct` | `discount_pct` | how far today's price sits below that SKU's own median price, clipped at 0 -- the raw data has no discount field, so a price dip below the item's normal price is the honest proxy |
| `category_popularity` | `category_popularity` | SKU's total-units-sold rank across the whole dataset, bucketed 1 (niche)-5 (bestseller), instead of an assigned category id |
| `day_of_week` / `is_weekend` | `day_of_week` / `is_weekend` | identical -- derived from the date either way |
| `wishlist_count_pre_sale` | `pre_period_demand` | trailing 7-day unit sales for that SKU (excluding today) -- the real analogue of a pre-event demand signal, measured in actual past sales instead of wishlist adds, since the dataset has no wishlist data |
| `orders_first_60s` (target) | `next_day_qty` (target) | next-day granularity, not next-60-seconds -- the dataset's timestamps support next-day aggregation reliably; per-SKU purchase counts at hourly/sub-minute granularity are almost all zero and too sparse to model |

## Section 10.2 -- bot detection now scores real checkout sessions

The full path is wired end to end: **checkout API → UserBehaviorLog →
scoring → FlaggedOrder → `/flagged-orders`**.

1. `app/main.py`'s three checkout endpoints (`/checkout/pessimistic`,
   `/checkout/optimistic`, `/checkout/redis`) now accept optional
   `page_load_time`, `checkout_time`, and `ip_address` query params
   (synthesized with sensible defaults if the client doesn't send them),
   and insert a `UserBehaviorLog` row for **every** attempt -- confirmed,
   rejected, or errored -- on its own DB connection, so a rolled-back
   checkout doesn't erase its own log row.
2. `app/ml/bot_detection.py`'s `score_sessions(db)` pulls real
   `UserBehaviorLog` rows (joined to `User` for `accounts_per_device`,
   bucketed by IP + minute for `requests_per_ip_per_min`), runs Isolation
   Forest, and for anything flagged writes a `FlaggedOrder` row
   (`review_status='pending'`) and flips that `Order.status` to
   `'flagged'` -- only for sessions tied to a real, successful order.

   **Disclose the `contamination` value in your report.** The two code
   paths use two constants that are different *kinds* of number:
   `SYNTHETIC_HARNESS_BOT_FRACTION = 0.1` is known by construction (the
   generator makes exactly 100 bots in 1000 sessions), while
   `ASSUMED_REAL_SCALPER_RATE = 0.25` is an **assumed prior on scalper
   prevalence -- not fitted to labels, because no ground-truth labels
   exist for the real path.** That absence is the whole reason this is
   unsupervised. It's a hyperparameter to disclose, not a result, and
   every real-path flag count is conditional on it -- which is why the
   scoring pass prints it. Don't justify it by counting the seeded
   shared-fingerprint cluster: those are known bots only because
   `seed.py` made them, and leaning on that would smuggle labels back in.
3. `scripts/run_bot_scoring.py` runs that scoring pass as an explicit
   batch job. It's deliberately **not** inline in the checkout request:
   Isolation Forest needs a real batch of sessions to compare against
   (there's no meaningful "is this one session anomalous" in isolation),
   and `accounts_per_device`/`requests_per_ip_per_min` are both computed
   over sets of rows, not derivable from a single in-flight request
   without re-scanning `users`/`user_behavior_logs` on every checkout.
4. `demo_5_benchmark.py` now also logs a `UserBehaviorLog` row per
   simulated buyer, reusing `scripts/seed.py`'s existing shared-device-
   fingerprint cluster (its first 6 seeded users) as bot-like buyers:
   near-zero page-load-to-checkout gap and one shared IP, versus normal
   randomized browsing time and distinct IPs for everyone else.

Verified live, end to end: after running `demo_5_benchmark.py` (40
buyers, 6 from the bot cluster) followed by `run_bot_scoring.py`, the
scoring pass found 10 anomalous sessions in that batch, 2 tied to a real
successful order:

```
Flagged sessions:
                            order_id  checkout_latency_ms  session_duration_ms  accounts_per_device  requests_per_ip_per_min  anomaly_score
88e24e0f-c0c2-4ec0-960f-9bc06a0eb44c              12730.0                12730                    1                        2       0.072565
b501a886-c22f-4168-935f-4dfcfaf19b03                289.0                  289                    6                       12       0.009310
```

The second row is exactly the bot-cluster signature —
`accounts_per_device=6` and `requests_per_ip_per_min=12` (6 accounts
sharing one fingerprint, all hitting checkout from the one shared bot IP
within the same minute), with a 289ms session. Both orders' `status`
flipped to `flagged` in the database, and `GET /flagged-orders` returned
both — confirmed by curling the endpoint directly against a running
`uvicorn` instance. Checkouts hit directly against `/checkout/pessimistic`,
`/checkout/optimistic` and `/checkout/redis` (not through the benchmark
script) were also confirmed to write their own `UserBehaviorLog` rows, so
both paths into the log table work.

**Read the output honestly, though:** Isolation Forest is unsupervised, so
"anomalous" means *unusual within this batch in any direction*, not
"definitely a bot". The first row above is a 12.7-second session from a
single-account device — a slow human, i.e. a false positive — and which
specific sessions get flagged varies from run to run. What is stable is
that the pass writes real `FlaggedOrder` rows tied to real `Order` ids
(verified over 5 consecutive demo_5 → run_bot_scoring cycles: 1, 2, 2, 1,
1 rows). Don't claim a precision/recall number for this real-data path in
your report — there are no ground-truth bot labels for it. The 83% recall
figure belongs only to the synthetic harness, which has labels by
construction.

## Running the API

```bash
uvicorn app.main:app --reload --port 8000
```

Visit `http://localhost:8000/docs` for interactive Swagger docs. The
`sale_id` and `user_id` to paste in are the constants above — they don't
change when you re-seed, so a saved request keeps working:

```
sale_id = 22222222-2222-2222-2222-222222222222
user_id = d0000000-0000-4000-8000-000000000000
```

Each checkout endpoint
also takes optional `page_load_time`, `checkout_time`, and `ip_address`
query params if you want to hand-craft a bot-like session to test the
Section 10.2 scoring path (see above) -- otherwise it synthesizes
plausible values itself. After a batch of checkouts, run
`python -m scripts.run_bot_scoring` and then hit `GET /flagged-orders`.

## What's still worth adding before submission

- Section 10.2 is fully wired now: checkout endpoints log real
  `UserBehaviorLog` rows, `bot_detection.score_sessions()` scores them and
  writes `FlaggedOrder`/`Order.status`, and `scripts/run_bot_scoring.py`
  runs it as a batch job -- see the Section 10.2 writeup above for a real
  verified result
- Section 10.1 is fully covered now: `demand_forecast.py` trains on the
  real UCI "Online Retail" dataset by default via `load_real_data()`, with
  the synthetic generator kept only as a no-dataset fallback -- see the
  Section 10.1 writeup above for real metrics and the feature-name mapping
- Section 6 is fully covered now: `scripts/seed.py` creates the composite
  B+ tree index on `Order(sale_id, created_at)` and the hash index on
  `User.device_fingerprint`, `scripts/seed_large.py` bulk-loads ~200k
  orders to make them worth measuring, and `demo_8_indexing.py` proves
  each one is used via before/after `EXPLAIN ANALYZE`
