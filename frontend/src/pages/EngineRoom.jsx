import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import Header from '../components/Header.jsx'
import { fetchDemandForecast, fetchFlaggedOrders, fetchQueueConfig } from '../api.js'
import {
  LostUpdateDiagram, PessimisticDiagram, OptimisticDiagram, RedisDiagram,
  BenchmarkChart, DeadlockDiagram, TimestampDiagram, GranularityDiagram,
  IndexDiagram, RecoveryDiagram, QueueDiagram, IntelligenceDiagram,
} from '../components/engine/Diagrams.jsx'

/**
 * The Engine Room -- a guided tour of every concurrency-control, indexing,
 * recovery and ML mechanism this system actually runs, told as a story
 * rather than a design-doc section list.
 *
 * Every diagram here illustrates a REAL code path in app/ (see the `code`
 * field on each entry below), not an abstract textbook drawing -- the
 * numbers in the benchmark section are demo_5_benchmark.py's actual output,
 * and the queue/forecast cards below fetch this deployment's own live
 * numbers rather than hardcoding copy that could drift from what the API
 * actually returns.
 */
const CHAPTERS = [
  {
    id: 'problem',
    kicker: 'Chapter 0 · The problem',
    title: 'Two buyers, one unit, zero coordination',
    body: [
      "Every engine in this room exists to answer one question: what happens when two buyers tap Buy Now on the same last unit at the same instant? Without anything guarding the row, the answer is simple and wrong.",
      'Both requests read the same starting stock. Both decide there’s enough. Both write. The database never sees a conflict, because from where it’s standing, there wasn’t one — two independent, individually valid statements just happened to land on the same row.',
    ],
    code: 'app/demos/demo_1_oversell.py',
    Diagram: LostUpdateDiagram,
  },
  {
    id: 'pessimistic',
    kicker: 'Chapter 1 · Pessimistic locking',
    title: 'Assume the collision, and lock first',
    body: [
      'SELECT ... FOR UPDATE takes a row lock the instant a checkout begins, before it even checks whether stock is available. Every other transaction wanting that same row simply cannot proceed — not "shouldn’t", cannot — until the lock holder commits or rolls back.',
      'It’s the most conservative answer there is: correctness by physically ruling the collision out. The cost shows up as latency, not bugs — buyer B is not wrong, just waiting.',
    ],
    code: 'app/demos/demo_2_pessimistic.py · app/main.py:checkout_pessimistic',
    Diagram: PessimisticDiagram,
  },
  {
    id: 'optimistic',
    kicker: 'Chapter 2 · Optimistic concurrency',
    title: 'Assume no collision, and check on the way out',
    body: [
      'No lock is taken up front. Every buyer reads the current version number, does their work, and writes back with UPDATE ... WHERE version = <the one I read>. If nobody else got there first, the row count is 1 and the version advances. If someone did, the row count is 0 — not an error, just a signal to retry.',
      'Under real contention this burns retries (72 of them, in the benchmark below) — but each one is a single fast UPDATE, not a held lock, so throughput stays high even while the retry count climbs.',
    ],
    code: 'app/demos/demo_3_optimistic.py · app/main.py:checkout_optimistic',
    Diagram: OptimisticDiagram,
  },
  {
    id: 'redis',
    kicker: 'Chapter 3 · The Redis fast path',
    title: 'Skip the database entirely, on purpose',
    body: [
      'DECR on a Redis key is atomic by construction — there’s no read-then-write gap for two buyers to race inside. Most "sold out" rejections never reach PostgreSQL at all; only the units that actually sell insert an order row.',
      'The trade is explicit, not accidental: Redis owns stock on this path, PostgreSQL stays the source of truth for the order record, and the two are allowed to diverge (AP over CP, deliberately — see the README’s Section 9 note).',
    ],
    code: 'app/demos/demo_4_redis_atomic.py · app/main.py:checkout_redis',
    Diagram: RedisDiagram,
  },
  {
    id: 'benchmark',
    kicker: 'Chapter 4 · The showdown',
    title: 'Same 5 units, same 40 buyers, three very different afternoons',
    body: [
      'This is the actual table demo_5_benchmark.py produced, pitting all three strategies against the same scarce-stock sale. All three are correct — zero oversold, every time — which is the point: correctness was never really in question. Speed was.',
      'Pessimistic serializes and queues. Optimistic is faster but pays in retries. Redis wins by not asking PostgreSQL at all until it already knows the answer is yes.',
    ],
    code: 'app/demos/demo_5_benchmark.py',
    Diagram: BenchmarkChart,
  },
  {
    id: 'deadlock',
    kicker: 'Chapter 5 · Deadlocks',
    title: 'When locking the row isn’t enough on its own',
    body: [
      'Lock two rows in opposite orders from two transactions and you don’t get a race — you get a standoff. T1 holds R1 and wants R2; T2 holds R2 and wants R1. Neither can proceed, and neither ever will on its own.',
      'PostgreSQL detects the cycle in its wait-for graph and aborts one participant (wound-wait picks the younger transaction) so the other can finish. The demo forces a real deadlock, then shows the same code path avoiding one.',
    ],
    code: 'app/demos/demo_6_deadlock.py',
    Diagram: DeadlockDiagram,
  },
  {
    id: 'timestamp',
    kicker: 'Chapter 6 · Timestamp ordering',
    title: 'Decide the order up front, not when the conflict shows up',
    body: [
      'Every transaction is stamped with a timestamp the moment it starts. Basic T/O then enforces a simple rule: if a write would violate that order (an older transaction trying to write after a younger one already did), roll it back.',
      'Thomas’s Write Rule refines that: if the late write would be immediately overwritten anyway — it’s already obsolete — there’s no need to punish anyone. Just skip it silently and let both transactions finish.',
    ],
    code: 'app/demos/demo_9_timestamp_ordering.py',
    Diagram: TimestampDiagram,
  },
  {
    id: 'granularity',
    kicker: 'Chapter 7 · Multigranularity locking',
    title: 'One lock table, four scopes, no contradictions',
    body: [
      'A checkout only ever needs one row. But an admin pausing an entire sale needs the whole table. Multigranularity locking lets both happen safely at once: intent locks (IS/IX) climb the tree first, announcing "something below me is locked" without actually locking everything below.',
      'pg_locks shows exactly this hierarchy during the demo — IS on the database, IX on the table, X on the one row a checkout actually touches.',
    ],
    code: 'app/demos/demo_10_multigranularity.py',
    Diagram: GranularityDiagram,
  },
  {
    id: 'indexing',
    kicker: 'Chapter 8 · Indexing',
    title: 'The same query, radically different amounts of work',
    body: [
      'Without an index, "orders for this sale in the last 5 seconds" means reading every row in the table and throwing most of them away. ix_orders_sale_id_created_at is a composite B+ tree built exactly for that shape of query — equality on sale_id, then a range on created_at, in one index.',
      'EXPLAIN ANALYZE against ~200k seeded rows is the honest before/after: Seq Scan disappears, an Index Scan (or Bitmap/Hash Scan, depending on the query) takes its place.',
    ],
    code: 'app/demos/demo_8_indexing.py',
    Diagram: IndexDiagram,
  },
  {
    id: 'recovery',
    kicker: 'Chapter 9 · Recovery',
    title: 'What "durable" actually means when the power goes',
    body: [
      'A transaction that crashes mid-write can’t just vanish and it can’t just stay half-applied. The write-ahead log is what makes ACID’s "D" real: every change is recorded before it’s applied, so a crash has something to replay against.',
      'On restart, committed work still in the log gets redone; uncommitted work gets undone. The demo simulates exactly that split and shows the database land in a consistent state either way.',
    ],
    code: 'app/demos/demo_7_recovery.py',
    Diagram: RecoveryDiagram,
  },
  {
    id: 'queue',
    kicker: 'Chapter 10 · The virtual waiting room',
    title: 'Absorb the stampede before it ever reaches a lock',
    body: [
      'Every engine above assumes checkout sees the crowd. The waiting room questions that assumption: buyers join a Redis sorted set (one cheap ZADD, no PostgreSQL involved) and are released to checkout a handful at a time.',
      'This is the SNKRS/Ticketmaster trick, and the reason isn’t politeness — a lock queue 1,000 deep and one 10 deep are different systems. The numbers below are this deployment’s own live admission rate.',
    ],
    code: 'app/queue.py · app/admission.py',
    Diagram: QueueDiagram,
  },
  {
    id: 'intelligence',
    kicker: 'Chapter 11 · The intelligence layer',
    title: 'What the data says after the sale, not during it',
    body: [
      'Two models watch this system without ever standing in checkout’s way. A gradient-boosted regressor forecasts next-period demand per SKU, feeding the decision of which lock strategy a future sale should even use.',
      'An Isolation Forest scores checkout sessions for anomalies — shared device fingerprints, impossible session speeds — and flags them for review. It never blocks an order; it just tells someone where to look.',
    ],
    code: 'app/ml/demand_forecast.py · app/ml/bot_detection.py',
    Diagram: IntelligenceDiagram,
  },
]

function LiveStat({ label, value, hint }) {
  return (
    <div className="rounded-xl border border-white/10 bg-white/[0.03] px-4 py-3">
      <div className="text-[10px] font-bold uppercase tracking-[0.14em] text-white/40">{label}</div>
      <div className="mt-1 text-xl font-black tabular text-white">{value}</div>
      {hint && <div className="mt-0.5 text-[11px] text-white/40">{hint}</div>}
    </div>
  )
}

function LiveNumbers() {
  const [queue, setQueue] = useState(null)
  const [forecast, setForecast] = useState(null)
  const [flagged, setFlagged] = useState(null)

  useEffect(() => {
    let alive = true
    fetchQueueConfig().then((d) => alive && setQueue(d)).catch(() => {})
    fetchDemandForecast().then((d) => alive && setForecast(d)).catch(() => {})
    fetchFlaggedOrders().then((d) => alive && setFlagged(d)).catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <LiveStat
        label="Admission rate"
        value={queue ? `${queue.admission_rate_per_s}/s` : '…'}
        hint={queue ? `${queue.admit_batch} every ${queue.admit_interval_s * 1000}ms` : 'live from /queue/config'}
      />
      <LiveStat
        label="Shopping window"
        value={queue ? `${queue.admission_ttl_s}s` : '…'}
        hint="per admission grant"
      />
      <LiveStat
        label="Forecast MAE"
        value={forecast ? forecast.mae : '…'}
        hint={forecast ? forecast.source.replace(' (real data)', '') : 'live from /demand-forecast'}
      />
      <LiveStat
        label="Flagged sessions"
        value={flagged ? flagged.length : '…'}
        hint="live from /flagged-orders"
      />
    </div>
  )
}

function Chapter({ chapter, index }) {
  const { kicker, title, body, code, Diagram } = chapter
  const reversed = index % 2 === 1
  return (
    <section className="animate-rise grid items-center gap-8 py-14 lg:grid-cols-2 lg:gap-14">
      <div className={reversed ? 'lg:order-2' : ''}>
        <div className="text-[11px] font-black uppercase tracking-[0.16em] text-orange-400">{kicker}</div>
        <h2 className="mt-2 text-2xl font-black leading-tight tracking-tight text-white sm:text-3xl">{title}</h2>
        <div className="mt-4 space-y-3 text-[15px] leading-relaxed text-white/65">
          {body.map((p, i) => (
            <p key={i}>{p}</p>
          ))}
        </div>
        <div className="mt-5 inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1.5 font-mono text-[11px] text-white/45">
          <span className="text-emerald-400">$</span> {code}
        </div>
      </div>

      <div className={reversed ? 'lg:order-1' : ''}>
        <div className="aspect-[400/220] w-full overflow-hidden rounded-2xl border border-white/10 bg-gradient-to-b from-white/[0.04] to-transparent p-3">
          <Diagram />
        </div>
      </div>
    </section>
  )
}

export default function EngineRoom() {
  return (
    <div className="min-h-screen bg-neutral-950">
      <Header active="engines" />

      <main className="mx-auto max-w-5xl px-4">
        <section className="py-14 text-center sm:py-20">
          <div className="text-[11px] font-black uppercase tracking-[0.2em] text-orange-400">
            Behind every Buy Now
          </div>
          <h1 className="mx-auto mt-3 max-w-3xl text-4xl font-black leading-[1.08] tracking-tight text-white sm:text-6xl">
            The Engine Room
          </h1>
          <p className="mx-auto mt-5 max-w-xl text-[15px] leading-relaxed text-white/60">
            Twelve mechanisms, one flash sale. This is the tour of what actually
            runs underneath GadgetZ every time stock hits zero and two people
            were both fast enough to matter.
          </p>
          <div className="mx-auto mt-8 max-w-2xl">
            <LiveNumbers />
          </div>
        </section>

        <div className="divide-y divide-white/5">
          {CHAPTERS.map((c, i) => (
            <Chapter key={c.id} chapter={c} index={i} />
          ))}
        </div>

        <section className="border-t border-white/5 py-16 text-center">
          <h2 className="text-2xl font-black tracking-tight text-white">Go watch it happen</h2>
          <p className="mx-auto mt-3 max-w-md text-sm text-white/55">
            Every diagram above is a real code path, not a mockup. Open a drop
            and watch the stock counter, or open two tabs and race yourself.
          </p>
          <Link
            to="/"
            className="mt-6 inline-flex items-center gap-2 rounded-xl bg-white px-6 py-3 text-sm font-black uppercase tracking-wider text-neutral-900 transition hover:gap-3"
          >
            Back to the shop <span aria-hidden>→</span>
          </Link>
        </section>
      </main>
    </div>
  )
}
