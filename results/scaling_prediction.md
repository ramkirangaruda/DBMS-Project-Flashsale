# Prediction, written BEFORE building or running anything

Recorded up front so it can be wrong in public. Nothing below is edited after
the fact; the results live in `horizontal_scaling_notes.md` and are reported
against this text as written.

**Change under test:** the FastAPI app goes from one host process to three
container replicas behind nginx. PostgreSQL and Redis stay exactly as they
are — single instances, untouched.

**Single-instance baseline to beat** (from `queue_notes.md`, N=1000, 60 units,
seed 2024):

| | raw | queued |
|---|---|---|
| pessimistic lock wait p95 | 1218.9ms | 93.7ms |
| pessimistic lock wait p99 | 1831.0ms | 156.0ms |
| peak concurrent checkouts | 1014 | 60–199 |
| K-sweep concurrency | — | flat ~31–39 across K=2…50 |

---

## 1. Pessimistic lock wait: I predict it gets WORSE, not better

**Prediction: queued-arm p95 rises from 93.7ms to somewhere in 200–400ms
(roughly 2–4×). Raw-arm p95 rises from 1218.9ms to 1500–3000ms.**

The reasoning is that nothing being scaled is the thing that is scarce. Every
pessimistic checkout takes `SELECT … FOR UPDATE` on **one inventory row in one
PostgreSQL instance**. That row lock is strictly serial: exactly one
transaction holds it at a time, no matter how many API processes want it.

Lock wait for a given request is approximately *(transactions queued ahead of
it) × (time each holds the lock)*. Tripling the API tier does not change the
second term at all, and it makes the first term worse — where one process
could push at most ~40 sync handlers and 30 pooled connections at the
database, three can push ~120 and ~90. More requests get *into* PostgreSQL
simultaneously, so the lock queue gets deeper.

This is the classic outcome of scaling a stateless tier in front of a
contended resource: **the queue does not disappear, it relocates.** It moves
out of the application's thread pool, where it was cheap and invisible, and
into the database's lock manager, where it is expensive and holds a connection
open for its whole duration. Throughput on a serialized resource cannot be
improved by adding clients to it.

I expect the two non-blocking strategies (optimistic, redis) to genuinely
benefit, because their contention is not a held lock — those should improve
roughly with the added capacity.

## 2. Secondary prediction: connection exhaustion is a live risk

`app/database.py` sets `pool_size=10, max_overflow=20` — up to 30 connections
per process. Three replicas is up to **90**, plus the harness's read-only
counting session, plus three metrics collectors and three sale refreshers.
PostgreSQL's default `max_connections` is **100**.

I predict we come close enough to that ceiling to matter, and there is a real
chance of seeing `FATAL: sorry, too many clients already` under the raw arm.
If that happens it is not a bug in the scaling work — it is the actual answer
to "what is the new bottleneck".

## 3. K-sweep: I predict the plateau rises but still does not track K

The single-instance sweep was flat at ~31–39 across a 25× range of K, which I
attributed to the 40-thread pool. With three pools the ceiling should rise to
roughly **90–120**.

**But I predict concurrency still will not track K**, because the binding
constraint will have moved to PostgreSQL — the connection pool ceiling and the
row lock — rather than to admission. If the sweep is flat at a *higher* number,
that confirms the thread-pool diagnosis was right about the old ceiling while
showing it was never the only one.

The specific way this could complicate the diagnosis: if the plateau lands
near ~90 that points at connections (3 × 30); if near ~120 it points at
threads (3 × 40). Those two numbers are close enough that I may not be able to
separate them cleanly, and I should say so rather than pick whichever is
convenient.

**One confound I am flagging now, not after the fact:** the admission worker
in `app/queue.py` is a per-process thread, so three replicas run three of
them. Each pops K off the front every interval, which makes the *effective*
admission rate `3 × K / interval` rather than `K / interval`. I am not
working around this — it is the honest out-of-the-box behaviour of scaling
this design horizontally, and a rate limiter that silently triples when you
add replicas is exactly the kind of thing this phase should surface. The
sweep is reported against nominal K, with effective rate stated alongside.

## 4. /metrics scrape gap: I predict it improves but does not fully resolve

The gap appeared because one saturated process could not answer Prometheus
during the raw herd. With load split three ways and nginx absorbing connection
setup, each replica should stay responsive enough to scrape. I predict the
hole shrinks or disappears for the replicas.

I do **not** expect it to be a clean win, because scraping is per-replica now:
if any single replica saturates, that replica's series has a gap while the
others do not, which is harder to read, not easier.

## 5. What I expect the new bottleneck to be, in order

1. **PostgreSQL** — the single row lock for pessimistic, and the connection
   ceiling for everything.
2. **The load generator** — `chaos_verification.py` is itself one Python
   process driving 1000 coroutines, and it was already a co-bottleneck at
   single scale. Removing the server-side limit may simply expose it.
3. **nginx** — least likely; it is event-driven C and this load is small for
   it, provided `worker_connections` is raised above the 512 default.
