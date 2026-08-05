# Tracing notes — three traces, side by side

Distributed tracing via OpenTelemetry auto-instrumentation, exported to Jaeger.
Every span below was produced by the FastAPI / SQLAlchemy / Redis integrations
patching their own libraries — **there is not one hand-written span inside any
checkout handler**, and no checkout or idempotency logic was modified to
produce these pictures.

Screenshots: [`trace_lock.png`](traces/trace_lock.png) ·
[`trace_retry.png`](traces/trace_retry.png) ·
[`trace_cache.png`](traces/trace_cache.png)
Raw numbers: [`trace_summary.json`](traces/trace_summary.json)

Reproduce with `python -m scripts.capture_traces`.

---

## Read the server span, not the trace duration

Jaeger's headline "Duration" is the **root** span, and the root here is the
capture harness, not the server. It includes the harness's own client-side
time — and under load that is not small: in the retry trace the root reads
1107.54ms while the server accounts for 345.52ms of it. The other ~760ms is
the harness's asyncio loop, busy driving twenty concurrent background buyers,
taking its time to get the subject's request onto the wire and read the reply
back off it.

That is not a flaw in the measurement, it is the measurement being honest
about who was slow. But it means **every duration quoted below is the
`flashsale-engine` server span**, which is the same thing Prometheus measures,
so the two are comparable.

---

## Trace 1 — pessimistic, waiting on the row lock

`08eb856c9624ff136999b714c5972609` · 18 competing buyers · **server span
176.17ms**

> **176.17ms total: 58.1ms before the first query (routing, validation, the two
> response-buffering middlewares, connection checkout), 4.0ms BEGIN, 93.9ms
> waiting for the row lock, 3.5ms UPDATE, 1.0ms INSERT order, 3.0ms INSERT
> behaviour log, 4.9ms COMMIT and serialisation, ~7.6ms inter-statement gaps.**

The `SELECT ... FOR UPDATE` span is **93.95ms — 53% of the request**, and it is
27× wider than the next-widest child. Nothing named it "lock wait"; it is
simply the statement, and the statement is wide because it genuinely blocked in
PostgreSQL until the transactions ahead of it committed. Once the lock is
granted the actual work takes 5.5ms.

That is the pessimistic strategy's whole trade-off in one bar: correctness by
serialisation, paid for in queueing.

### Making it happen was not what I expected

The obvious way to deepen a lock queue is more buyers. **It does not work, and
the traces are what showed why.** At a herd of 96 the lock span was 32ms of a
359ms request — 9% — with 316ms unaccounted. At a herd of 16 it was 230ms of
329ms, 70%.

Past roughly fifteen concurrent buyers the extra load never reaches PostgreSQL
at all. `app/database.py` allows 30 pooled connections and each checkout
briefly needs *two* — `get_db()` still holds one while `log_checkout_attempt()`
opens its own — so the real ceiling is about half the pool. Beyond it,
requests queue for a connection or for a FastAPI worker thread, and both of
those waits happen **outside any span**. The request gets slower and the trace
gets *less* informative.

`scripts/capture_traces.py` therefore walks a ladder of small herds and
re-drives until the lock clears 50% of the request, rather than assuming one
run is representative. Position in the queue is genuinely random: rounds 1–4 of
this capture measured 16%, 4%, 47% and 53% at nearly identical settings.

---

## Trace 2 — optimistic, version conflict then retry

`c00363f0f949d6d095f3536b5f52a79c` · 20 competing buyers · **two server spans,
292.75ms + 52.77ms**

> **Attempt 1 — 292.75ms: 83.5ms queued before the first query, 49.3ms opening
> a new PostgreSQL connection, 5.5ms SELECT version, 43.1ms UPDATE that matched
> zero rows, 12.6ms INSERT behaviour log, ~83ms rollback and serialisation. No
> order written.**
> **Attempt 2 — 52.77ms: 13.9ms pre-query, 7.0ms SELECT, 2.5ms UPDATE, 3.0ms
> INSERT order, 7.4ms INSERT behaviour log, ~8.5ms COMMIT and serialisation.
> Order confirmed.**

The two attempts are distinct sibling subtrees, and the difference between them
is visible without reading a single number: **the failed attempt has one
INSERT, the successful one has two.** The missing INSERT is the order that was
never written because `UPDATE ... WHERE version = :v` matched nothing. The
conflict is legible as an absence.

The 49.31ms `connect` span in attempt 1 is worth pointing at too — that is the
pool exhausted and a genuinely new PostgreSQL connection being opened
mid-stampede. It is the only reason that particular cost is visible at all;
when the pool has a spare connection the same span reads 0.00ms.

### The retry is client-driven, and the trace says so

`/checkout/optimistic` does not retry. It detects the conflict, returns 409, and
the *caller* decides to try again — so the retry is a second, independent HTTP
request and would normally be a second, independent trace. There is no
server-side loop to instrument, and adding one would have meant changing
checkout behaviour to make it easier to observe.

So the harness opens a `purchase_intent` span and propagates W3C `traceparent`
across both attempts. That is ordinary distributed tracing and it is
architecturally truthful: the client really is the parent, because the client
really is what retries. **The root span in this screenshot is the harness, not
the server** — worth saying out loud, because the picture otherwise implies a
server-side retry loop that does not exist.

### Attempt 2 was fast partly because the storm had passed

52.77ms versus 292.75ms is a 5.5× gap for the same code path, and very little
of it is OCC. Attempt 1 spent 83ms queueing and another 49ms opening a
connection before it could ask PostgreSQL anything; attempt 2 was talking to
the database 14ms in, on a pooled connection. By the time the retry went out,
the herd had drained.

Reading this as "retrying is cheap" would be the wrong lesson. It was cheap
*here* because it arrived after the contention.

---

## Trace 3 — redis, served from the idempotency cache

`bc23c3e297031fe7e153c3d1c2544c22` · no contention · **server span 3.00ms**

> **3.00ms total: 1.0ms Redis GET of the cached response, ~2.0ms framework
> (routing, middleware, serialisation). No PostgreSQL. No DECR. No order
> insert.**

Four spans in the whole trace, one of which is the Redis lookup. The
idempotency middleware found `idempotency:{key}`, returned the stored response,
and the checkout handler never ran. The short-circuit is visible as an
**absence of everything else**.

### The control is in the same trace pair

The priming request — same key, same purchase, moments earlier — is
`e26e24d5412aabf4aa7b923fcc01fb8b`, and it is the honest comparison because
only one variable differs:

| Same Idempotency-Key | Server span | Child spans | What ran |
|---|---|---|---|
| First send (executed) | **32.11ms** | 9 | GET (miss) 6.2ms, SET (reservation) 2.0ms, EXISTS 2.0ms, DECRBY 1.0ms, INSERT order 3.0ms, INSERT log 1.5ms, SET (cache result) 1.5ms |
| Retry (replayed) | **3.00ms** | 1 | GET 1.0ms |

**10.7× faster, and structurally different** — not a lucky fast run, a
different amount of work. This is the same mechanism the previous phase counted
as `idempotency_cache_hits_total`; here it is the individual request rather
than the rate.

---

## The three side by side

| Trace | Server span | vs cache hit |
|---|---|---|
| Optimistic, both attempts | 345.52ms | **115×** |
| Optimistic, conflicting attempt | 292.75ms | **98×** |
| Pessimistic, lock wait | 176.17ms | **59×** |
| Redis, idempotency cache hit | **3.00ms** | 1× |

Opened side by side in Jaeger the cache-hit trace barely registers as a bar.
That is the point: two of these requests contend for a shared resource and one
of them declines to participate.

The comparison is *not* "Redis is 59× faster than pessimistic locking" — these
are three different amounts of work, not three implementations of the same
work. The lock trace bought a unit under contention; the cache trace recognised
a purchase it had already completed and re-read the answer.

---

## Are these traces typical, or did I screenshot the flattering ones?

Prometheus percentiles over the 10-minute window each capture ran in
(`checkout_latency_seconds`, server-side, the same measurement the traces
show). That window also contains a 120-buyer `chaos_verification --compare`
run, so these are heavily contended numbers:

| Strategy | p50 | p95 | p99 | samples |
|---|---|---|---|---|
| pessimistic | 344.9ms | 827.5ms | 965.5ms | 277 |
| optimistic | 354.0ms | 891.1ms | 978.2ms | 271 |
| redis | 414.6ms | 918.0ms | 983.6ms | 211 |

**The lock trace is roughly half the median.** 176.17ms against a pessimistic
p50 of 344.9ms — the captured request was comfortably in the quicker half of
the traffic that produced it. It looks dramatic, but it is a mild example: the
p99 was 965.5ms, five and a half times its total. If anything this trace
*understates* how bad the queue gets.

**The conflicting attempt is close to typical; its retry is not.** 292.75ms
sits just below the optimistic p50 of 354.0ms and well below the p95 of
891.1ms — an ordinary contended request. The 52.77ms retry is far below p50 and
should not be read as typical; it is what a request costs when nothing is in
its way.

**The cache hit is genuinely fast, but the percentile is the wrong yardstick
for it.** 3.00ms against a redis p50 of 414.6ms is a 138× gap, and quoting that
number would be misleading: the p50 is dominated by a 120-buyer stampede while
the cache hit was measured on an idle system, so most of that ratio is the
absence of load, not the presence of a cache. The same-key control isolates the
one variable that matters — **32.11ms executed versus 3.00ms replayed, 10.7×**
— and that is the number worth carrying into the report.

(Note also that `app/metrics.py` deliberately excludes replayed responses from
the latency histogram, since a replay never ran checkout. So the redis
percentiles above describe *executed* checkouts only; the cache hit is not
hiding inside them, dragging them down.)

### Against the previous phase's chaos numbers

`results/observability_notes.md` reported, at N=600 with chaos injected: p50
pessimistic **750ms**, redis **347ms**, optimistic **271ms**. The ordering here
is different — redis is now the *slowest* p50 rather than the middle one —
because these windows mix a 120-buyer run with small capture herds, whereas
that one was a sustained 600-buyer stampede where the Redis path spends most of
its time cheaply rejecting sold-out requests.

**Latency numbers from different load levels are not comparable, and the traces
are examples rather than a distribution.** The traces show where time goes
inside one request; the percentiles show how often. Neither substitutes for the
other, and the honest use of the pair is the one made above: to check whether
the example that got screenshotted was a fair one.

---

## What tracing could not see

- **The residual is real time and is not small.** Uncontended, a checkout's
  server span is 25–32ms while its child spans account for only 9–17ms. That
  fixed ~15–22ms gap is FastAPI routing and validation, the idempotency and
  metrics middlewares each buffering the response body, JSON serialisation, and
  `COMMIT` — which SQLAlchemy's instrumentation does not span at all, because
  it hooks cursor execution and a commit is a connection-level call.

- **Queueing is invisible, and it is often the largest term.** In the lock
  trace, 58.1ms passed between the server span opening and the first SQL
  statement. In the retry trace, 83.5ms. That is time spent waiting for a
  worker thread and a pooled connection, and it appears in the trace only as a
  gap. Auto-instrumentation traces *calls*; waiting to be allowed to make a
  call is not a call.

  This is the single most useful thing the traces taught: the unattributed time
  is where the scaling limit actually lives, and past the pool ceiling it grows
  faster than the lock wait does.

- **Traces are sampled by chance, not by severity.** There is no tail-based
  sampling here; every request is traced and these three were picked by the
  capture script. Jaeger's storage is in-memory (see `docker-compose.yml`), so
  **traces are lost when the container restarts** — the PNGs are the durable
  artefact, not the container.

- **Two spans deep is where the semantics stop.** The SQL spans carry
  `db.statement`, so the `FOR UPDATE` is identifiable, but nothing records
  *which* row, *which* transaction held the lock, or how deep the queue was.
  For that you would need PostgreSQL's own `pg_locks`, not the client's view.

---

## Reproducing

```bash
docker compose up -d                  # + jaeger on 16686 (OTLP on 4318)
python -m app.main                    # tracing initialises on startup
python -m scripts.seed_storefront     # needs at least 3 active sales

python -m scripts.capture_traces                  # all three + screenshots
python -m scripts.capture_traces --only cache     # just the deterministic one
python -m scripts.capture_traces --no-screenshot  # numbers only
```

Screenshots additionally need `python -m playwright install chromium`.

Jaeger UI: <http://localhost:16686> — pick service `flashsale-capture-harness`
to list the captured purchase intents, or `flashsale-engine` for raw traffic.

Turn tracing off entirely with `OTEL_SDK_DISABLED=true`. Nothing else in the
system depends on it, and if Jaeger is not running the exporter fails quietly
in a background thread rather than affecting checkout.
