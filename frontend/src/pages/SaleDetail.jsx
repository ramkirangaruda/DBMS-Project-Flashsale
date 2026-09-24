import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, WarningCircle } from '@phosphor-icons/react'
import { checkout, fetchSale, resetSale, serverNow } from '../api.js'
import { getAdminMode, getAdminToken, getDemoUserId, getStrategy, money } from '../lib/demo.js'
import {
  CountdownPill, DiscountBadge, FlashBadge, PriceRow, ProductArt, StockBar,
  useServerCountdown,
} from '../components/bits.jsx'
import ResultOverlay from '../components/ResultOverlay.jsx'

const POLL_MS = 700

/** Big "3 . 2 . 1" pre-drop screen, driven entirely by the server clock. */
function DropCountdown({ ms }) {
  const secs = Math.ceil(ms / 1000)
  const finalTen = secs <= 10
  return (
    <div className="rounded-2xl bg-ink p-8 text-center text-page">
      <div className="text-xs font-medium uppercase tracking-[0.14em] text-accent">
        Drop opens in
      </div>
      <div
        key={secs}
        className={`tabular animate-pop mt-3 font-semibold leading-none ${
          finalTen ? 'text-8xl text-accent sm:text-9xl' : 'text-6xl sm:text-7xl'
        }`}
      >
        {finalTen ? secs : new Date(ms).toISOString().substr(14, 5)}
      </div>
      <p className="mt-4 text-sm text-page/60">
        Buy Now unlocks for everyone at the same instant.
      </p>
    </div>
  )
}

export default function SaleDetail() {
  const { saleId } = useParams()
  const [sale, setSale] = useState(null)
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [flash, setFlash] = useState(false)

  // Read once on mount and never surfaced in the UI.
  const strategyRef = useRef(getStrategy())
  const userIdRef = useRef(getDemoUserId())
  const adminRef = useRef(getAdminMode())
  const tokenRef = useRef(getAdminToken())
  const prevAvailable = useRef(null)
  const [resetting, setResetting] = useState(false)
  const [resetStock, setResetStock] = useState(1)

  /* ---- live stock poll (~700ms) so the number visibly ticks down ---- */
  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const d = await fetchSale(saleId)
        if (!alive) return
        if (prevAvailable.current !== null && d.available !== prevAvailable.current) {
          setFlash(true)
          setTimeout(() => alive && setFlash(false), 400)
        }
        prevAvailable.current = d.available
        setSale(d)
        setError(null)
      } catch (e) {
        if (alive) setError(e.message)
      }
    }
    load()
    const id = setInterval(load, POLL_MS)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [saleId])

  const startMs = sale?.start_time ? new Date(sale.start_time).getTime() : null
  const notStarted = startMs !== null && startMs > serverNow()
  const untilStart = useServerCountdown(sale?.start_time)
  const untilEnd = useServerCountdown(sale?.end_time)

  // Re-render the moment the drop opens so the button un-disables itself
  // without waiting for the next poll.
  const [, forceTick] = useState(0)
  useEffect(() => {
    if (!notStarted) return
    const id = setInterval(() => forceTick((n) => n + 1), 100)
    return () => clearInterval(id)
  }, [notStarted])

  const buy = useCallback(async () => {
    if (busy) return
    setBusy(true)
    try {
      const r = await checkout({
        saleId,
        userId: userIdRef.current,
        strategy: strategyRef.current,
      })
      setResult(r)
    } finally {
      setBusy(false)
    }
  }, [busy, saleId])

  if (error) {
    return (
      <Shell>
        <div className="rounded-2xl border border-danger/25 bg-danger/5 p-8 text-center">
          <WarningCircle weight="light" size={36} className="mx-auto text-danger" />
          <h2 className="mt-3 font-semibold text-danger">{error}</h2>
          <Link to="/" className="mt-3 inline-block text-sm font-medium text-danger underline underline-offset-2">
            Back to all sales
          </Link>
        </div>
      </Shell>
    )
  }

  if (!sale) {
    return (
      <Shell>
        <div className="h-72 animate-pulse rounded-2xl bg-surface-2" />
      </Shell>
    )
  }

  const soldOut = sale.available <= 0
  const ended = sale.has_ended
  const canBuy = !notStarted && !soldOut && !ended && !busy

  let label = 'Buy now'
  if (busy) label = 'Placing order...'
  else if (notStarted) label = 'Opens soon'
  else if (ended) label = 'Sale ended'
  else if (soldOut) label = 'Sold out'

  return (
    <Shell>
      <div className="grid gap-8 lg:grid-cols-2">
        {/* ---------------- product art ---------------- */}
        <div className="relative overflow-hidden rounded-[28px]">
          <ProductArt
            category={sale.category}
            className="h-72 w-full sm:h-[440px]"
            iconSize={120}
          />
          <div className="absolute left-4 top-4 flex gap-2">
            <FlashBadge>{notStarted ? 'Drop incoming' : 'Flash sale'}</FlashBadge>
            <DiscountBadge pct={sale.discount_pct} />
          </div>
        </div>

        {/* ---------------- buy panel ---------------- */}
        <div className="flex flex-col gap-6">
          <div>
            <div className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted">
              {sale.category || 'gadget'}
            </div>
            <h1 className="mt-1.5 text-3xl font-semibold leading-tight tracking-tight text-ink sm:text-4xl">
              {sale.product}
            </h1>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <PriceRow base={sale.base_price} sale={sale.sale_price} size="lg" />
            {sale.discount_pct > 0 && (
              <span className="rounded-full bg-accent/10 px-2.5 py-1 text-xs font-semibold text-accent">
                Save {money(sale.base_price - sale.sale_price)}
              </span>
            )}
          </div>

          {notStarted ? (
            <DropCountdown ms={untilStart} />
          ) : (
            <div className="rounded-2xl border border-hairline bg-surface p-5">
              <div className="mb-4 flex items-center justify-between">
                <span className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted">
                  Live stock
                </span>
                <CountdownPill ms={untilEnd} label="Ends in" />
              </div>

              <div
                className={`tabular text-5xl font-semibold tracking-tight transition-colors duration-200 ${
                  flash ? 'text-accent' : soldOut ? 'text-muted' : 'text-ink'
                }`}
              >
                {sale.available}
                <span className="ml-2 text-lg font-medium text-muted">
                  / {sale.total_stock} left
                </span>
              </div>

              <StockBar available={sale.available} total={sale.total_stock} className="mt-4" />

              <p className="mt-3 text-xs text-muted">
                Updating live every {POLL_MS}ms. Watch it drop as others buy.
              </p>
            </div>
          )}

          <button
            onClick={buy}
            disabled={!canBuy}
            className={`w-full rounded-full px-6 py-4 text-base font-semibold transition active:scale-[.98] ${
              canBuy
                ? 'bg-ink text-page hover:opacity-90'
                : 'cursor-not-allowed bg-surface-2 text-muted'
            }`}
          >
            {label}
          </button>

          {notStarted && (
            <p className="-mt-3 text-center text-xs text-muted">
              Synchronized to the server clock. Every device unlocks together.
            </p>
          )}

          {/* Hidden admin strip -- only rendered with ?admin=1 */}
          {adminRef.current && (
            <div className="rounded-2xl border border-dashed border-hairline bg-surface-2 p-3.5">
              <div className="mb-2.5 text-[11px] font-medium uppercase tracking-[0.08em] text-muted">
                Admin: reset stock
              </div>
              <div className="flex items-center gap-2">
                {[1, 3, 5].map((n) => (
                  <button
                    key={n}
                    onClick={() => setResetStock(n)}
                    className={`h-9 w-9 rounded-full text-sm font-semibold transition ${
                      resetStock === n
                        ? 'bg-ink text-page'
                        : 'bg-surface text-ink-2 ring-1 ring-inset ring-hairline'
                    }`}
                  >
                    {n}
                  </button>
                ))}
                <button
                  disabled={resetting}
                  onClick={async () => {
                    setResetting(true)
                    try {
                      await resetSale(saleId, resetStock, tokenRef.current)
                      setResult(null)
                    } catch (e) {
                      alert(e.message)
                    } finally {
                      setResetting(false)
                    }
                  }}
                  className="ml-auto rounded-full bg-ink px-4 py-2 text-xs font-semibold text-page transition hover:opacity-90 disabled:opacity-50"
                >
                  {resetting ? 'Resetting...' : `Reset to ${resetStock}`}
                </button>
              </div>
            </div>
          )}

          <Link
            to="/"
            className="inline-flex items-center justify-center gap-1.5 text-sm font-medium text-muted transition hover:text-ink"
          >
            <ArrowLeft weight="bold" size={14} />
            All flash sales
          </Link>
        </div>
      </div>

      <ResultOverlay
        result={result}
        onClose={() => setResult(null)}
        onRetry={() => {
          setResult(null)
          buy()
        }}
      />
    </Shell>
  )
}

function Shell({ children }) {
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-30 border-b border-hairline/80 bg-page/85 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-5xl items-center px-4 sm:px-6">
          <Link to="/" className="flex items-center gap-1.5 text-base font-semibold tracking-tight text-ink">
            Gadget<span className="text-accent">Z</span>
          </Link>
          <div className="ml-auto flex items-center gap-2 text-xs font-medium text-ink-2">
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-pulse-ring rounded-full bg-success" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-success" />
            </span>
            Live
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-8 sm:px-6 sm:py-12">{children}</main>
    </div>
  )
}
