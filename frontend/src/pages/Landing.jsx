import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowRight, Lightning, ShoppingCartSimple } from '@phosphor-icons/react'
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
      className="group relative block w-full overflow-hidden rounded-[28px] text-left shadow-pop transition active:scale-[.995]"
    >
      <ProductArt category={sale.category} className="h-[320px] w-full sm:h-[420px]" iconSize={140} />

      <div className="absolute inset-0 bg-gradient-to-t from-black/80 via-black/25 to-transparent sm:bg-gradient-to-r sm:from-black/80 sm:via-black/45 sm:to-transparent" />

      <div className="absolute inset-0 flex flex-col justify-end gap-4 p-6 sm:justify-center sm:p-12">
        <div className="flex flex-wrap items-center gap-2">
          <FlashBadge>{upcoming ? 'Drop incoming' : 'Flash sale'}</FlashBadge>
          <DiscountBadge pct={sale.discount_pct} />
        </div>

        <h2 className="max-w-lg text-3xl font-semibold leading-tight tracking-tight text-white sm:text-5xl">
          {sale.product}
        </h2>

        <div className="flex flex-wrap items-center gap-3">
          <div className="text-white">
            <PriceRow base={sale.base_price} sale={sale.sale_price} size="lg" />
          </div>
          <CountdownPill ms={remaining} label={upcoming ? 'Opens in' : 'Ends in'} />
        </div>

        <div className="max-w-xs">
          <div className="mb-1.5 flex items-baseline justify-between text-white/75">
            <span className="text-xs font-medium">
              {sale.sold_out ? 'Sold out' : `${sale.available} left`}
            </span>
            <span className="tabular text-xs">{sale.available}/{sale.total_stock}</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-white/20">
            <div
              className="h-full rounded-full bg-white transition-[width] duration-500"
              style={{
                width: `${sale.total_stock ? (sale.available / sale.total_stock) * 100 : 0}%`,
              }}
            />
          </div>
        </div>

        <span className="mt-1 inline-flex w-fit items-center gap-2 rounded-full bg-white px-5 py-3 text-sm font-semibold text-neutral-900 transition group-hover:gap-3">
          {upcoming ? 'Get ready' : 'Shop now'}
          <ArrowRight weight="bold" size={16} />
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
      className="group flex flex-col overflow-hidden rounded-2xl border border-hairline bg-surface shadow-card transition hover:-translate-y-0.5"
    >
      <div className="relative">
        <ProductArt category={sale.category} className="h-40 w-full" iconSize={48} />
        <div className="absolute left-3 top-3 flex gap-1.5">
          <DiscountBadge pct={sale.discount_pct} />
        </div>
        {sale.sold_out && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/60">
            <span className="rounded-full bg-white px-3 py-1.5 text-xs font-semibold text-neutral-900">
              Sold out
            </span>
          </div>
        )}
      </div>

      <div className="flex flex-1 flex-col gap-2.5 p-4">
        <div className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted">
          {sale.category || 'gadget'}
        </div>
        <h3 className="text-sm font-semibold leading-snug tracking-tight text-ink">{sale.product}</h3>
        <PriceRow base={sale.base_price} sale={sale.sale_price} />
        <div className="mt-auto space-y-3 pt-1">
          <StockBar available={sale.available} total={sale.total_stock} />
          <CountdownPill
            ms={remaining}
            label={upcoming ? 'Opens' : 'Ends'}
            className="w-full justify-center"
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
      <header className="sticky top-0 z-30 border-b border-hairline/80 bg-page/85 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-6xl items-center gap-6 px-4 sm:px-6">
          <Link to="/" className="flex items-center gap-1.5 text-base font-semibold tracking-tight text-ink">
            Gadget<span className="text-accent">Z</span>
          </Link>
          <nav className="hidden gap-6 text-sm text-ink-2 sm:flex">
            <span className="font-medium text-ink">Flash sales</span>
            <span>New</span>
            <span>Deals</span>
          </nav>
          <div className="ml-auto flex items-center gap-2 text-xs font-medium text-ink-2">
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-pulse-ring rounded-full bg-success" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-success" />
            </span>
            Live
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6 sm:py-12">
        {error && (
          <div className="rounded-2xl border border-danger/25 bg-danger/5 p-4 text-sm text-danger">
            <b className="font-semibold">Can't reach the API.</b> {error}
            <div className="mt-1 text-danger/80">
              Start it with <code className="font-mono text-xs">uvicorn app.main:app --port 8010</code>.
            </div>
          </div>
        )}

        {!sales && !error && (
          <div className="h-[320px] animate-pulse rounded-[28px] bg-surface-2 sm:h-[420px]" />
        )}

        {sales && sales.length === 0 && (
          <div className="rounded-2xl border border-hairline bg-surface p-10 text-center">
            <ShoppingCartSimple weight="light" size={40} className="mx-auto text-muted" />
            <h2 className="mt-4 text-lg font-semibold text-ink">No active flash sales</h2>
            <p className="mt-1.5 text-sm text-ink-2">
              Seed some with{' '}
              <code className="font-mono text-xs text-ink">python -m scripts.seed_storefront</code>
            </p>
          </div>
        )}

        {hero && (
          <section>
            <Hero sale={hero} />
          </section>
        )}

        {rest.length > 0 && (
          <section className="mt-12">
            <div className="mb-5 flex items-baseline justify-between">
              <h2 className="text-xl font-semibold tracking-tight text-ink">More deals</h2>
              <span className="text-xs font-medium text-muted">{rest.length} live</span>
            </div>
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
              {rest.map((s) => (
                <ProductCard key={s.sale_id} sale={s} />
              ))}
            </div>
          </section>
        )}
      </main>

      <footer className="mx-auto flex max-w-6xl items-center gap-2 px-4 pb-10 pt-4 text-xs text-muted sm:px-6">
        <Lightning weight="fill" size={13} className="text-accent" />
        Flash-Sale Inventory and Oversell Prevention Engine. Live storefront demo.
      </footer>
    </div>
  )
}
