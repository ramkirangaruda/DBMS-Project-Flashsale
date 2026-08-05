# Observability notes — what a chaos run actually looks like

Grafana dashboard **Flash-Sale Engine — Concurrency & Chaos** (`flashsale-chaos`),
scraped from the FastAPI app's `/metrics` every 2s.

Screenshots: [`chaos_n200.png`](dashboards/chaos_n200.png) ·
[`chaos_n600.png`](dashboards/chaos_n600.png)

Both were taken immediately after a full `--compare` sweep (all three
strategies, run twice each — once without `Idempotency-Key`, once with), so a
single frame contains six runs and every signal below appears more than once.

---

## The headline question

> Are the **OCC retry spike** and the **idempotency cache hits** both clearly
> visible as *distinct* signals during the same run?

**Yes — confirmed, and measured rather than eyeballed.** Querying Prometheus
directly over the N=600 window at 2s resolution:

| | samples |
|---|---|
| OCC retries > 0 | 44 |
| Idempotency hits > 0 | 79 |
| **Both simultaneously > 0** | **41** |
| Retries > 0 while hits == 0 | 3 |
| Hits > 0 while retries == 0 | 38 |

Overlap window `18:38:29 → 18:41:57`; at `18:41:11` the two read
`retries = 0.76/s` and `hits = 3.66/s` in the same scrape.

The last two rows are what make this a real answer rather than a coincidence.
If the two counters were secretly tracking the same underlying event, they
would rise and fall together and never appear alone. They do appear alone —
38 samples with cache hits and no retries (the pessimistic and redis arms,
which have no version conflicts by construction), and 3 with retries and no
hits (the optimistic arm's *no-idempotency* half). They are independent
signals that happen to co-occur during the optimistic-with-idempotency run,
which is exactly the combination worth being able to see.

On the dashboard this reads as two separate shapes: a narrow orange bar spike
in **OCC version-conflict retries**, and a green step in **Idempotency cache
hit rate** rising at the same moment. Neither is inferable from the other.

---

## Panel by panel, during a run

**Checkout throughput by strategy** — six distinct humps at N=600, one per
arm, peaking around 30 req/s. The `sold_out` series dominates `confirmed` by
roughly 10:1; that is the demo working, not a failure. 600 buyers, 60 units.

**Checkout latency p50/p95/p99** — the clearest strategy comparison on the
board. At N=600: p50 pessimistic **750ms**, redis **347ms**, optimistic
**271ms**, with tails climbing past 2s as lock queues build. Pessimistic's
curve rises steadily through its run — that is the queue forming. Redis stays
comparatively flat because most rejections never reach PostgreSQL.

**OCC version-conflict retries** — flat zero for pessimistic and redis, two
sharp spikes for optimistic (one per arm). Worth restating on the dashboard
itself, and the panel description says so: a spike here is optimistic
concurrency control *working*, not an error rate.

**Idempotency cache hit rate** — ~0 during the no-idempotency arms, stepping
to a visible plateau during the with-idempotency arms; mean **13.7%** across
the N=600 window. The absolute number is unexciting by design: each logical
buyer sends one fresh request (a miss) and only ~17% of them are chosen to
retry (a hit), so the ceiling is inherently low. The *shape* is the signal.

**Idempotency hits vs misses (raw)** — sits beside the rate panel because a
ratio alone is ambiguous when traffic is idle. Peaks: misses **18.4/s**, hits
**3.66/s**.

**CAP divergence** — the most visually dramatic panel. Redis reads 60 while
PostgreSQL reads 0 for the same sale, and the reverse for another. This is
the documented trade-off drawn live: `/checkout/redis` never decrements
`inventory.reserved_stock`, and the SQL strategies never touch the Redis
counter. Each strategy sells down its own store, so which line falls tells
you which strategy just ran.

**Live divergence |sql − redis|** — stat tiles, orange when the two stores
disagree. Shown as an absolute value so a gap in either direction colours the
same; the timeseries beside it shows which store is ahead.

---

## Caveats worth stating

- **`redis_reservation_races_total` is a lower bound, and read 0 in these
  runs.** It counts only races the server *reports* — the 409 "still in
  flight" response. A request that loses the `SET NX` race but then
  successfully waits for the winner's result is, from outside, identical to
  an ordinary cache hit: same body, same `Idempotent-Replay: true` header.
  Counting those would mean incrementing a counter inside
  `app/idempotency.py`'s contention branch, which this task explicitly
  excluded from modification. Zero here means "no request was rejected as
  in-flight", **not** "no races occurred". If you want the true figure, that
  is a one-line addition to the middleware's `if not won:` branch.

- **Replayed responses are excluded from the latency histogram.** A cache
  replay never runs checkout, so folding it in would flatter p50 by mixing
  in sub-millisecond cache reads. Throughput counts them; latency does not.

- **Metrics are process-local.** Restarting the API resets every counter.
  Prometheus handles the reset correctly for `rate()`, but absolute totals
  restart from zero.

- **The gauges are sampled, not evented.** The CAP collector polls every 3s,
  so a divergence that opens and closes faster than that can be missed. Fine
  for runs lasting ~15s; not a guarantee of catching every transient.

- **A quiet dashboard is not a passing test.** These panels show *behaviour*,
  not correctness. The stock invariant is checked by
  `scripts/chaos_verification.py` against the `orders` table, and that
  remains the pass/fail. Both runs here held: 5/5 at N=200 and 60/60 at
  N=600, across all three strategies, with `bought_twice = 0` throughout.

---

## Reproducing

```bash
docker compose up -d                      # postgres, redis, prometheus, grafana
python -m app.main                        # API on :8010, /metrics exposed
python -m scripts.seed_storefront         # sales to sell

python -m scripts.chaos_verification --n 200 --chaos 0.3 --stock 5  --seed 1337 --compare
python -m scripts.chaos_verification --n 600 --chaos 0.5 --stock 60 --seed 99   --compare

python -m scripts.capture_dashboard results/dashboards/chaos_n600.png --window 10m
```

Grafana: <http://localhost:3000> (anonymous admin, no login) ·
Prometheus: <http://localhost:9090>

The dashboard and datasource are provisioned from `monitoring/`, so they load
automatically — there is no click-setup to redo on a fresh machine.
