import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { fetchSales, serverNow } from '../api.js'
import {
  CountdownPill, DiscountBadge, FlashBadge, PriceRow, ProductArt, StockBar,
  useServerCountdown,
} from '../components/bits.jsx'

/**
 * Ranks sales for the hero. "Most urgent" = closest to selling out, or
 * starting soonest if it hasn't opened yet -- an upcoming drop always wins,
 * because that is the one people need to be on the page for.
 */
function rankForHero(sales) {
  const now = serverNow()
  return [...sales].sort((a, b) => {
    const aUpcoming = new Date(a.start_time).getTime() > now
    const bUpcoming = new Date(b.start_time).getTime() > now
    if (aUpcoming !== bUpcoming) return aUpcoming ? -1 : 1
    if (aUpcoming && bUpcoming) {
      return new Date(a.start_time) - new Date(b.start_time)
    }
    const frac = (s) => (s.total_stock > 0 ? s.available / s.total_stock : 0)
    return frac(a) - frac(b)
  })
}

function Hero({ sale }) {
  const navigate = useNavigate()
  const startMs = new Date(sale.start_time).getTime()
  const upcoming = startMs > serverNow()
  const target = upcoming ? sale.start_time : sale.end_time
  const remaining = useServerCountdown(target)

  return (
    <button
      onClick={() => navigate(`/sale/${sale.sale_id}`)}
      className="group relative block w-full overflow-hidden rounded-2xl text-left shadow-xl transition active:scale-[.995]"
    >
      <ProductArt category={sale.category} className="h-[300px] w-full sm:h-[380px]" glyphClass="text-[9rem] opacity-90" />

      <div className="absolute inset-0 bg-gradient-to-r from-black/85 via-black/55 to-transparent" />

      <div className="absolute inset-0 flex flex-col justify-center gap-4 p-7 sm:p-12">
        <div className="flex flex-wrap items-center gap-2">
          <FlashBadge>{upcoming ? 'Drop incoming' : 'Flash sale'}</FlashBadge>
          <DiscountBadge pct={sale.discount_pct} />
        </div>

        <h2 className="max-w-xl text-3xl font-black leading-[1.05] tracking-tight text-white sm:text-5xl">
          {sale.product}
        </h2>

        <div className="flex flex-wrap items-center gap-4">
          <div className="text-white">
            <PriceRow base={sale.base_price} sale={sale.sale_price} size="lg" />
          </div>
          <CountdownPill ms={remaining} label={upcoming ? 'Opens in' : 'Ends in'} />
        </div>

        <div className="max-w-xs">
          <div className="mb-1.5 flex items-baseline justify-between text-white/80">
            <span className="text-xs font-bold uppercase tracking-wider">
              {sale.sold_out ? 'Sold out' : `Only ${sale.available} left`}
            </span>
            <span className="tabular text-xs">{sale.available}/{sale.total_stock}</span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-white/25">
            <div
              className="h-full rounded-full bg-white transition-[width] duration-500"
              style={{
                width: `${sale.total_stock ? (sale.available / sale.total_stock) * 100 : 0}%`,
              }}
            />
          </div>
        </div>

        <span className="mt-1 inline-flex w-fit items-center gap-2 rounded-xl bg-white px-6 py-3 text-sm font-black uppercase tracking-wider text-neutral-900 transition group-hover:gap-3">
          {upcoming ? 'Get ready' : 'Shop now'} <span aria-hidden>→</span>
        </span>
      </div>
    </button>
  )
}

function ProductCard({ sale }) {
  const startMs = new Date(sale.start_time).getTime()
  const upcoming = startMs > serverNow()
  const remaining = useServerCountdown(upcoming ? sale.start_time : sale.end_time)

  return (
    <Link
      to={`/sale/${sale.sale_id}`}
      className="group flex flex-col overflow-hidden rounded-xl border border-neutral-200 bg-white shadow-sm transition hover:-translate-y-0.5 hover:shadow-lg"
    >
      <div className="relative">
        <ProductArt category={sale.category} className="h-40 w-full" glyphClass="text-5xl" />
        <div className="absolute left-3 top-3 flex gap-1.5">
          <DiscountBadge pct={sale.discount_pct} />
        </div>
        {sale.sold_out && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/65">
            <span className="rounded bg-white px-3 py-1.5 text-xs font-black uppercase tracking-widest">
              Sold out
            </span>
          </div>
        )}
      </div>

      <div className="flex flex-1 flex-col gap-3 p-4">
        <div className="text-[10px] font-bold uppercase tracking-[0.14em] text-neutral-400">
          {sale.category || 'gadget'}
        </div>
        <h3 className="text-sm font-bold leading-snug tracking-tight">{sale.product}</h3>
        <PriceRow base={sale.base_price} sale={sale.sale_price} />
        <div className="mt-auto space-y-3">
          <StockBar available={sale.available} total={sale.total_stock} />
          <CountdownPill
            ms={remaining}
            label={upcoming ? 'Opens' : 'Ends'}
            className="w-full justify-center text-xs"
          />
        </div>
      </div>
    </Link>
  )
}

export default function Landing() {
  const [sales, setSales] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    const load = () =>
      fetchSales()
        .then((d) => alive && (setSales(d.sales), setError(null)))
        .catch((e) => alive && setError(e.message))
    load()
    // Slow poll so the homepage stays roughly fresh without hammering.
    const id = setInterval(load, 4000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [])

  const ranked = sales ? rankForHero(sales) : []
  const hero = ranked[0]
  const rest = ranked.slice(1)

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-30 border-b border-neutral-800 bg-neutral-900">
        <div className="mx-auto flex h-14 max-w-6xl items-center gap-6 px-4">
          <Link to="/" className="text-lg font-black uppercase tracking-tight text-white">
            Gadget<span className="text-orange-500">Z</span>
          </Link>
          <nav className="hidden gap-5 text-[11px] font-bold uppercase tracking-[0.12em] text-white/70 sm:flex">
            <span className="text-white">Flash Sales</span>
            <span>New</span>
            <span>Deals</span>
          </nav>
          <div className="ml-auto flex items-center gap-2 text-[11px] font-bold uppercase tracking-[0.12em] text-white/70">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
            Live
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-4 py-6 sm:py-10">
        {error && (
          <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            <b>Can't reach the API.</b> {error}
            <div className="mt-1 text-red-600/80">
              Start it with <code className="font-mono">uvicorn app.main:app --port 8010</code>.
            </div>
          </div>
        )}

        {!sales && !error && (
          <div className="h-[300px] animate-pulse rounded-2xl bg-neutral-200 sm:h-[380px]" />
        )}

        {sales && sales.length === 0 && (
          <div className="rounded-xl border border-neutral-200 bg-white p-8 text-center">
            <div className="text-4xl">🛒</div>
            <h2 className="mt-3 text-lg font-bold">No active flash sales</h2>
            <p className="mt-1 text-sm text-neutral-500">
              Seed some with{' '}
              <code className="font-mono text-xs">python -m scripts.seed_storefront</code>
            </p>
          </div>
        )}

        {hero && (
          <section>
            <Hero sale={hero} />
          </section>
        )}

        {rest.length > 0 && (
          <section className="mt-10">
            <div className="mb-4 flex items-baseline justify-between">
              <h2 className="text-xl font-black tracking-tight">More deals</h2>
              <span className="text-xs font-bold uppercase tracking-widest text-neutral-400">
                {rest.length} live
              </span>
            </div>
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
              {rest.map((s) => (
                <ProductCard key={s.sale_id} sale={s} />
              ))}
            </div>
          </section>
        )}
      </main>

      <footer className="mx-auto max-w-6xl px-4 pb-10 pt-4 text-xs text-neutral-400">
        Flash-Sale Inventory &amp; Oversell Prevention Engine — live storefront demo.
      </footer>
    </div>
  )
}
