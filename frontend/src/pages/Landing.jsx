import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowRight, Lightning, ShoppingCartSimple } from '@phosphor-icons/react'
import { fetchSales, serverNow } from '../api.js'
import {
  CountdownPill, DiscountBadge, FlashBadge, PriceRow, ProductArt, StarRating, StockBar,
  WishlistButton, useServerCountdown,
} from '../components/bits.jsx'
import Header from '../components/Header.jsx'
import { seededRating } from '../lib/demo.js'

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
      <ProductArt category={sale.category} className="h-[320px] w-full sm:h-[420px]" glyphClass="text-[9rem] opacity-90" />

      <div className="absolute inset-0 bg-gradient-to-t from-black/80 via-black/25 to-transparent sm:bg-gradient-to-r sm:from-black/80 sm:via-black/45 sm:to-transparent" />

      <div className="absolute inset-0 flex flex-col justify-end gap-4 p-6 sm:justify-center sm:p-12">
        <div className="flex flex-wrap items-center gap-2">
          <FlashBadge>{upcoming ? 'Drop incoming' : 'Flash sale'}</FlashBadge>
          <DiscountBadge pct={sale.discount_pct} />
        </div>

        <h2 className="max-w-lg text-3xl font-semibold leading-tight tracking-tight text-white sm:text-5xl">
          {sale.product}
        </h2>

        <StarRating {...seededRating(sale.sale_id)} dark />

        <div className="flex flex-wrap items-center gap-4">
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
        <ProductArt category={sale.category} className="h-40 w-full" glyphClass="text-5xl" />
        <div className="absolute left-3 top-3 flex gap-1.5">
          <DiscountBadge pct={sale.discount_pct} />
        </div>
        <WishlistButton saleId={sale.sale_id} className="absolute right-3 top-3" />
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
        <h3 className="text-sm font-bold leading-snug tracking-tight">{sale.product}</h3>
        <StarRating {...seededRating(sale.sale_id)} />
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

/** Row of category filter chips above the product grid -- every real
 * ecommerce homepage has a way to narrow the grid down, and with 12
 * categories in the catalogue now, scanning the whole thing to find one
 * kind of product stops being reasonable. */
function CategoryChips({ categories, active, onChange }) {
  return (
    <div className="-mx-4 flex gap-2 overflow-x-auto px-4 pb-1 sm:mx-0 sm:flex-wrap sm:px-0">
      {categories.map((c) => (
        <button
          key={c}
          onClick={() => onChange(c)}
          className={`shrink-0 rounded-full px-3.5 py-1.5 text-xs font-bold capitalize tracking-wide transition ${
            active === c
              ? 'bg-neutral-900 text-white'
              : 'bg-white text-neutral-500 ring-1 ring-neutral-200 hover:text-neutral-900'
          }`}
        >
          {c}
        </button>
      ))}
    </div>
  )
}

export default function Landing() {
  const [sales, setSales] = useState(null)
  const [error, setError] = useState(null)
  const [category, setCategory] = useState('all')

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

  const categories = useMemo(
    () => ['all', ...new Set(rest.map((s) => s.category).filter(Boolean))],
    [rest],
  )
  const visible = category === 'all' ? rest : rest.filter((s) => s.category === category)

  return (
    <div className="min-h-screen">
      <Header active="shop" />

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
          <section className="mt-10">
            <div className="mb-4 flex items-baseline justify-between">
              <h2 className="text-xl font-black tracking-tight">More deals</h2>
              <span className="text-xs font-bold uppercase tracking-widest text-neutral-400">
                {visible.length} of {rest.length}
              </span>
            </div>

            {categories.length > 2 && (
              <CategoryChips categories={categories} active={category} onChange={setCategory} />
            )}

            {visible.length === 0 ? (
              <p className="mt-6 text-center text-sm text-neutral-400">
                Nothing live in that category right now.
              </p>
            ) : (
              <div className="mt-4 grid grid-cols-2 gap-4 lg:grid-cols-4">
                {visible.map((s) => (
                  <ProductCard key={s.sale_id} sale={s} />
                ))}
              </div>
            )}
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
