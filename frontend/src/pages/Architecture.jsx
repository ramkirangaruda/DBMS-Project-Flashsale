import Header from '../components/Header.jsx'

/* Simple, static system diagram. Colours: orange = request path, sky = data
 * stores, violet = ML / batch, neutral = observability. */

function Box({ x, y, w, h, title, sub, tone = 'neutral' }) {
  const tones = {
    neutral: 'fill-neutral-800 stroke-neutral-600',
    orange: 'fill-orange-500/15 stroke-orange-500',
    sky: 'fill-sky-500/15 stroke-sky-500',
    violet: 'fill-violet-500/15 stroke-violet-500',
  }
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx="10" strokeWidth="1.5" className={tones[tone]} />
      <text x={x + w / 2} y={y + (sub ? h / 2 - 3 : h / 2 + 4)} textAnchor="middle" className="fill-white text-[13px] font-bold">
        {title}
      </text>
      {sub && (
        <text x={x + w / 2} y={y + h / 2 + 14} textAnchor="middle" className="fill-neutral-400 text-[10px]">
          {sub}
        </text>
      )}
    </g>
  )
}

function Arrow({ d, dashed = false, tone = 'orange' }) {
  const stroke = { orange: 'stroke-orange-500', sky: 'stroke-sky-400', violet: 'stroke-violet-400', neutral: 'stroke-neutral-500' }[tone]
  return (
    <path
      d={d}
      fill="none"
      strokeWidth="1.6"
      strokeDasharray={dashed ? '5 4' : undefined}
      className={stroke}
      markerEnd="url(#arrow)"
    />
  )
}

export default function Architecture() {
  return (
    <div className="min-h-screen">
      <Header active="architecture" />
      <main className="mx-auto max-w-6xl px-4 py-8 sm:py-12">
        <h1 className="text-3xl font-black tracking-tight">System architecture</h1>
        <p className="mt-2 max-w-2xl text-sm text-neutral-400">
          Every buyer request passes through a waiting room, an admission gate and an idempotency
          cache before one of three concurrency-control strategies touches the database.
        </p>

        <div className="mt-8 overflow-x-auto rounded-2xl border border-neutral-800 bg-neutral-900 p-4">
          <svg viewBox="0 0 900 560" className="mx-auto w-full min-w-[760px]" role="img" aria-label="Architecture diagram">
            <defs>
              <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M0 0L10 5L0 10z" className="fill-neutral-300" />
              </marker>
            </defs>

            {/* column headings */}
            <text x="60" y="24" className="fill-neutral-500 text-[10px] font-bold uppercase tracking-widest">Client</text>
            <text x="250" y="24" className="fill-neutral-500 text-[10px] font-bold uppercase tracking-widest">API tier</text>
            <text x="640" y="24" className="fill-neutral-500 text-[10px] font-bold uppercase tracking-widest">Data</text>

            {/* client + load balancer */}
            <Box x={20} y={190} w={130} h={64} title="Buyers" sub="React storefront" />
            <Box x={20} y={300} w={130} h={64} title="nginx" sub="load balancer :8080" tone="orange" />

            {/* API replicas with middleware chain */}
            <rect x="230" y="40" width="330" height="330" rx="14" strokeWidth="1.5" strokeDasharray="6 4" className="fill-transparent stroke-neutral-700" />
            <text x="245" y="62" className="fill-neutral-400 text-[11px] font-bold">FastAPI x3 replicas</text>
            <Box x={250} y={76} w={290} h={36} title="Tracing  (OpenTelemetry)" tone="neutral" />
            <Box x={250} y={124} w={290} h={36} title="Admission gate  (waiting-room token)" tone="orange" />
            <Box x={250} y={172} w={290} h={36} title="Metrics  (Prometheus)" tone="neutral" />
            <Box x={250} y={220} w={290} h={36} title="Idempotency-Key cache" tone="orange" />
            <Box x={250} y={268} w={140} h={84} title="Checkout" sub="pessimistic / OCC" tone="orange" />
            <Box x={400} y={268} w={140} h={84} title="Checkout" sub="Redis DECR" tone="orange" />

            {/* data stores */}
            <Box x={640} y={90} w={220} h={92} title="PostgreSQL 16" sub="source of truth + CHECK constraints" tone="sky" />
            <Box x={640} y={230} w={220} h={92} title="Redis 7" sub="queue, idempotency, fast-path stock" tone="sky" />

            {/* external + ML + observability */}
            <Box x={230} y={420} w={150} h={56} title="Stripe (test mode)" sub="payments + webhook" />
            <Box x={400} y={420} w={150} h={56} title="Prometheus / Grafana" sub="+ Jaeger traces" />
            <Box x={640} y={380} w={220} h={56} title="ML batch jobs" sub="forecast + Isolation Forest" tone="violet" />

            {/* request path */}
            <Arrow d="M85 254 L85 300" />
            <Arrow d="M150 332 L230 332" />
            <Arrow d="M395 112 L395 124" />
            <Arrow d="M395 160 L395 172" />
            <Arrow d="M395 208 L395 220" />
            <Arrow d="M320 256 L320 268" />
            <Arrow d="M470 256 L470 268" />

            {/* to data */}
            <Arrow d="M350 352 L350 392 L610 392 L610 136 L640 136" tone="sky" dashed />
            <Arrow d="M540 310 L640 280" tone="sky" dashed />
            <Arrow d="M540 142 L575 142 L575 255 L640 255" tone="sky" dashed />

            {/* payments, observability, ML */}
            <Arrow d="M275 370 L275 420" tone="neutral" dashed />
            <Arrow d="M475 420 L475 372" tone="neutral" dashed />
            <Arrow d="M860 408 C 892 408 892 136 860 136" tone="violet" dashed />

            {/* legend */}
            <g transform="translate(20 500)">
              <line x1="0" y1="0" x2="26" y2="0" strokeWidth="1.6" className="stroke-orange-500" />
              <text x="34" y="4" className="fill-neutral-400 text-[10px]">request path</text>
              <line x1="130" y1="0" x2="156" y2="0" strokeWidth="1.6" strokeDasharray="5 4" className="stroke-sky-400" />
              <text x="164" y="4" className="fill-neutral-400 text-[10px]">data access</text>
              <line x1="250" y1="0" x2="276" y2="0" strokeWidth="1.6" strokeDasharray="5 4" className="stroke-violet-400" />
              <text x="284" y="4" className="fill-neutral-400 text-[10px]">ML reads orders / sessions</text>
            </g>
          </svg>
        </div>

        <ol className="mt-8 grid gap-4 text-sm text-neutral-300 sm:grid-cols-2">
          <li><b className="text-white">1. Waiting room.</b> Buyers join a Redis queue and are released K at a time with a short-lived admission token.</li>
          <li><b className="text-white">2. Idempotency.</b> A retried request replays the cached response instead of buying twice.</li>
          <li><b className="text-white">3. Three strategies.</b> Row lock, version check, or atomic Redis counter, all correct, with different trade-offs.</li>
          <li><b className="text-white">4. Database backstop.</b> A CHECK constraint stops <code>reserved_stock</code> exceeding <code>total_stock</code>.</li>
          <li><b className="text-white">5. ML.</b> Demand forecasting and Isolation Forest bot detection read orders and sessions in batch.</li>
          <li><b className="text-white">6. Observability.</b> Prometheus, Grafana and Jaeger watch every layer.</li>
        </ol>
      </main>
    </div>
  )
}
