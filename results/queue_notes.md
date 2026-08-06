# Waiting room — does putting a queue in front of checkout actually help?

A Redis sorted set now sits between buyers and `/checkout/*`. Buyers join at
`POST /queue/join/{sale_id}`, poll `GET /queue/status/{sale_id}`, and are
released K at a time by a background worker; checkout refuses anyone without a
live admission token. This is what Ticketmaster and SNKRS-style drops do.

The question is whether it pays for itself, measured rather than assumed.

Reports: [`queue_raw_n1000.md`](queue_raw_n1000.md) ·
[`queue_queued_n1000.md`](queue_queued_n1000.md)
Raw collections: [`queue_comparison_raw.json`](queue_comparison_raw.json) ·
[`queue_comparison_queued.json`](queue_comparison_queued.json)

```bash
# raw arm  (pre-queue front door, same binary, gate switched off)
ADMISSION_ENFORCED=false python -m app.main
python -m scripts.chaos_verification --n 1000 --chaos 0.3 --stock 60 --seed 2024 --no-queue
python -m scripts.queue_comparison --label raw

# queued arm
python -m app.main
python -m scripts.chaos_verification --n 1000 --chaos 0.3 --stock 60 --seed 2024
python -m scripts.queue_comparison --label queued
python -m scripts.queue_comparison --report
```

Both arms: **N=1000 buyers per strategy, 60 units of stock, 30% chaos, seed
2024**, all three strategies, idempotency on. One variable differs — whether
the herd goes through the front door or straight at checkout.

---

## The payoff, in one table

| | raw herd | through queue | |
|---|---|---|---|
| **Peak concurrent checkout requests** | **1014** | **60 – 199** | 5–17× fewer |
| `SELECT … FOR UPDATE` wait p95 | 1218.9ms | **93.7ms** | −92% |
| `SELECT … FOR UPDATE` wait p99 | 1831.0ms | **156.0ms** | −91% |
| `SELECT … FOR UPDATE` wait max | 2045.5ms | **205.7ms** | −90% |
| OCC version conflicts (optimistic) | 69 | **11** | −84% |
| checkout latency p50 — optimistic | 4116.7ms | **45.7ms** | −99% |
| checkout latency p50 — redis | 4668.4ms | **45.3ms** | −99% |
| checkout latency p50 — pessimistic | 4284.5ms | **2368.1ms** | −45% |
| **Confirmed orders vs stock** | 60 / 60 | 60 / 60 | invariant holds |

Lock waits and version conflicts are the two things the queue was supposed to
reduce, and both fell by an order of magnitude. That is the result.

---

## 1. How admission rate shapes checkout concurrency

The brief asked to *"confirm the checkout endpoints never see more than ~K
concurrent requests"*. **That is not what happens, and it is worth being
precise about why, because the reason is more useful than the claim.**

Sweeping K with everything else fixed (N=300, redis strategy, ample stock so
every request does full work):

| K per 500ms | admission rate | peak concurrent checkouts |
|---|---|---|
| 2 | 4/s | 31 |
| 5 | 10/s | 32 |
| 10 | 20/s | 39 |
| 25 | 50/s | 34 |
| 50 | 100/s | 36 |

**A 25× change in admission rate moved concurrency not at all.** It sits at
~35 throughout. K is not the thing setting it.

Two mechanisms explain this, and both are real:

**Admission bounds a rate; concurrency is a rate times a duration.** By
Little's Law, requests in flight = admission rate × how long each admitted
buyer occupies checkout. K alone cannot bound concurrency without also
bounding service time. A buyer admitted at 10/s who takes 3 seconds inside
checkout leaves 30 in flight, not 10.

**Downstream of admission there is a smaller bottleneck: FastAPI's worker
thread pool.** The checkout handlers are sync `def`, so each occupies one of
40 pooled threads for its whole duration. Concurrency saturates there
regardless of what admission does, which is exactly why the sweep is flat at
~35. Raising K past the pool's capacity buys nothing; lowering it below does
not reduce concurrency either, because the system is closed-loop — slower
admission means less contention means shorter service times, and the product
stays put.

So the accurate claim is not *"checkout never sees more than K"*. It is:

> **Checkout-side concurrency becomes independent of crowd size.** Raw, it
> tracked the herd exactly — 1000 buyers produced 1014 simultaneous requests.
> Queued, it settled at 60–199 whether 300 or 1000 people showed up. The
> server's exposure is now a function of its own capacity, not of how many
> people turned up, and that is the property that keeps it standing.

The peak-in-flight figures here are measured **client-side**, by sweep line
over each request's send and completion time
(`StrategyRun.max_checkout_concurrency`). The server also publishes
`checkout_in_flight`, but Prometheus samples it every 2 seconds and — during
the raw arm — the `/metrics` scrape itself timed out under the load, so the
server-side series has a hole precisely where the peak was. A monitoring
endpoint that goes blind exactly when the system is in trouble is its own
small lesson.

---

## 2. Did lock waits and OCC retries actually drop?

Yes, and these are the numbers that justify the whole exercise.

**Pessimistic lock wait**, measured directly from the `SELECT … FOR UPDATE`
span durations of the runs' own traffic in Jaeger — the statement blocks
inside PostgreSQL until the row lock is granted, so the span width *is* the
wait. 273 spans sampled raw, 237 queued.

| | raw | queued |
|---|---|---|
| p50 | 20.2ms | **7.2ms** (−64%) |
| p95 | 1218.9ms | **93.7ms** (−92%) |
| p99 | 1831.0ms | **156.0ms** (−91%) |
| max | 2045.5ms | **205.7ms** (−90%) |

The tail is where it matters. Raw, the unluckiest buyer waited **2.0 seconds**
for a row lock; queued, **206ms**. The queue did not make the lock faster — it
made the line shorter.

**OCC version conflicts: 69 → 11, down 84%.** A version conflict happens when
two transactions read the same `version` and both try to write it. Spreading
the writers out in time makes the read-modify-write windows overlap less
often, so fewer collide. Note this is the count of *wasted* work: 58 buyers
who previously did a full round trip only to be told to start again now
simply succeed first time.

**Checkout latency collapsed for the two non-blocking strategies** — optimistic
p50 4116.7ms → 45.7ms, redis 4668.4ms → 45.3ms, both −99%. Under the raw herd
these requests spent almost all their time queueing for a worker thread behind
1000 peers; that queue is gone.

**Pessimistic latency improved far less: p50 −45%, p95 essentially unchanged
(9281.6ms → 9392.0ms).** Worth stating plainly rather than burying, because it
is informative: pessimistic holds the row lock for its entire transaction, so
even a modest number of concurrent buyers still serialise. The queue moved that
strategy's bottleneck from "1000 requests fighting" to "40 threads fighting",
and 40 is still more than one row lock can absorb. Its p95 is now dominated by
thread-pool queueing, not lock contention — which is precisely what the 92%
drop in lock wait alongside a flat p95 is telling us.

---

## 3. Does the invariant still hold?

**Yes. 60 confirmed orders against 60 units, in all three strategies, in both
arms, with `bought_twice = 0` throughout.**

The queue is admission control, not a correctness mechanism, and it was
important that it not be mistaken for one. Nothing about it prevents an
oversell — if the locking were wrong, admitting buyers ten at a time would
make the bug rarer and harder to find, not absent. The invariant is still
enforced where it always was, inside the checkout transaction, and still
verified the same way: by counting rows in the `orders` table, not by trusting
client responses.

---

## Three defects the measurements found

None of these were visible by reading the code. All three came from numbers
that did not match the configuration, and all three are fixed.

**The waiting room was competing with checkout for worker threads.** The queue
endpoints were written as ordinary sync `def`, which FastAPI runs in the same
40-thread pool as the checkout handlers — and a contended checkout holds a
thread for seconds. So `/queue/status` polls stacked up behind the very
requests the queue existed to throttle. Buyers were admitted promptly
(server-side wait p50 0.3s) but did not *learn* of it for tens of seconds, and
**627 buyers who had queued correctly arrived holding an expired token and were
turned away with a 403**. Fixed by making the request path fully async on a
`redis.asyncio` client, so it costs no thread at all.

**The admission window started at the wrong moment.** The 30s TTL ran from the
instant the server granted admission, so a buyer punished by a slow reply could
receive a token that was already dead. `GET /queue/status` now refreshes the TTL
when it *tells* a buyer they are in — the window means "you have 30 seconds to
shop", counted from being told. Refusals fell 627 → 130.

**The admission worker blocked on the database it was protecting.** The worker
refreshed its set of active sale ids inline every 5 seconds, using the same
SQLAlchemy pool the checkout handlers were exhausting — so once ~30 checkouts
were in flight, admission simply stopped until a connection came free. The
symptom was unmissable once looked at: K = 2, 5, 10, 25 produced wall times of
90.5s, 93.4s, 90.3s, 93.0s. A 12× range of configured admission rate, and the
rate limiter was not limiting at any of them. Fixed by splitting the refresh
into its own thread so the admit loop touches nothing but Redis.

There was a fourth, smaller one: the loop did `sleep(interval)` *after* its
work, making the true period `interval + work` — the effective rate came out at
12.5/s against a configured 20/s. It now advances an absolute deadline, which
measures 19.9/s against 20/s.

> The common thread is worth naming: **an admission controller must not share a
> scarce resource with the thing it is admitting people to.** Threads, then
> connections, then its own clock. Each time, the throttle stopped working
> exactly when the load it was built for arrived.

---

## What this costs, and what is still wrong

**Buyers wait.** Server-side queue wait was p50 0.4s / p95 6.2s; buyers
*observed* p50 5–64s because they only learn on their next poll. The queue
converts contention into waiting — it does not remove work, it reorders it. A
shopper who would have got a 4-second checkout now gets a fast checkout after
a wait. Whether that is better is a product question, not a technical one; the
honest technical statement is that total time-to-answer did not improve for the
median buyer, only its predictability and the load it imposed on the database.

**130 buyers were still refused at the door** (out of ~1780 admitted, 7%),
down from 627 but not zero. These are tokens that expired between notification
and use, at a load where both the server and the load generator are
single-process Python and saturated. It is a real limitation, not a rounding
error.

**The single-process deployment is the ceiling on all of this.** One uvicorn
worker with 1000 concurrent connections is saturated no matter what the front
door does — hence the flat K sweep, the timed-out `/metrics` scrape, and the
harness's own event loop being a co-bottleneck. The admission worker is an
in-process thread, so its rate degrades under exactly the load it is meant to
control; a real deployment runs it as a separate service for that reason.

**The estimate is a straight line, not a prediction.** `estimated_wait_s` is
just `position / K × interval`. It assumes nobody abandons and the rate holds,
and the K sweep shows the rate does not always hold. It is honest enough for
deciding how long a client should sleep between polls, which is the job it
actually has.

---

## Dashboard

Four new panels were appended to the existing Grafana board (`flashsale-chaos`)
as a **Waiting room** row — queue depth per sale, joins vs admits vs refusals,
time spent waiting, and checkout requests in flight. The seven pre-existing
panels are byte-identical; the diff on `flashsale.json` is 275 added lines and
0 removed.

`current_queue_depth` shows the shape most clearly: a near-vertical rise as the
herd arrives, then a straight-line decay whose slope is the admission rate.
