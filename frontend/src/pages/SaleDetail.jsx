import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { checkout, fetchSale, resetSale, serverNow } from '../api.js'
import { getAdminMode, getAdminToken, getDemoUserId, getStrategy, money } from '../lib/demo.js'
import {
  CountdownPill, DiscountBadge, FlashBadge, PriceRow, ProductArt, StockBar,
  useServerCountdown,
} from '../components/bits.jsx'
import ResultOverlay from '../components/ResultOverlay.jsx'
import Header from '../components/Header.jsx'

const POLL_MS = 700

/** Big "3 · 2 · 1" pre-drop screen, driven entirely by the server clock. */
function DropCountdown({ ms }) {
  const secs = Math.ceil(ms / 1000)
  const finalTen = secs <= 10
  return (
    <div className="rounded-2xl bg-gradient-to-br from-neutral-900 via-neutral-900 to-black p-8 text-center text-white">
      <div className="text-[11px] font-black uppercase tracking-[0.2em] text-orange-400">
        Drop opens in
      </div>
      <div
        key={secs}
        className={`tabular animate-pop mt-3 font-black leading-none ${
          finalTen ? 'text-8xl text-red-500 sm:text-9xl' : 'text-7xl sm:text-8xl'
        }`}
      >
        {finalTen ? secs : new Date(ms).toISOString().substr(14, 5)}
      </div>
      <p className="mt-4 text-sm text-white/60">
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
  // {phase: 'queued'|'admitted'|'checking-out', position, etaS} while a buy
  // is in flight -- the waiting room is the interesting part of the demo, so
  // it gets shown rather than hidden behind a spinner.
  const [queue, setQueue] = useState(null)

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
    setQueue(null)
    try {
      const r = await checkout({
        saleId,
        userId: userIdRef.current,
        strategy: strategyRef.current,
        onProgress: setQueue,
      })
      setResult(r)
    } finally {
      setBusy(false)
      setQueue(null)
    }
  }, [busy, saleId])

  if (error) {
    return (
      <Shell>
        <div className="rounded-xl border border-red-200 bg-red-50 p-6 text-center">
          <div className="text-3xl">⚠️</div>
          <h2 className="mt-2 font-bold text-red-800">{error}</h2>
          <Link to="/" className="mt-3 inline-block text-sm font-bold text-red-700 underline">
            Back to all sales
          </Link>
        </div>
      </Shell>
    )
  }

  if (!sale) {
    return (
      <Shell>
        <div className="h-72 animate-pulse rounded-2xl bg-neutral-200" />
      </Shell>
    )
  }

  const soldOut = sale.available <= 0
  const ended = sale.has_ended
  const canBuy = !notStarted && !soldOut && !ended && !busy

  let label = 'Buy Now'
  if (busy && queue?.phase === 'queued' && queue.position > 1) label = `In line · #${queue.position}`
  else if (busy && queue?.phase === 'queued') label = 'Entering…'
  else if (busy) label = 'Placing order…'
  else if (notStarted) label = 'Opens soon'
  else if (ended) label = 'Sale ended'
  else if (soldOut) label = 'Sold out'

  return (
    <Shell>
      <div className="grid gap-7 lg:grid-cols-2">
        {/* ---------------- product art ---------------- */}
        <div className="relative overflow-hidden rounded-2xl">
          <ProductArt
            category={sale.category}
            className="h-72 w-full sm:h-[420px]"
            glyphClass="text-[8rem]"
          />
          <div className="absolute left-4 top-4 flex gap-2">
            <FlashBadge>{notStarted ? 'Drop incoming' : 'Flash sale'}</FlashBadge>
            <DiscountBadge pct={sale.discount_pct} />
          </div>
        </div>

        {/* ---------------- buy panel ---------------- */}
        <div className="flex flex-col gap-5">
          <div>
            <div className="text-[11px] font-bold uppercase tracking-[0.14em] text-neutral-400">
              {sale.category || 'gadget'}
            </div>
            <h1 className="mt-1.5 text-3xl font-black leading-tight tracking-tight sm:text-4xl">
              {sale.product}
            </h1>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <PriceRow base={sale.base_price} sale={sale.sale_price} size="lg" />
            {sale.discount_pct > 0 && (
              <span className="rounded bg-red-50 px-2 py-1 text-xs font-black text-red-600">
                Save {money(sale.base_price - sale.sale_price)}
              </span>
            )}
          </div>

          {notStarted ? (
            <DropCountdown ms={untilStart} />
          ) : (
            <div className="rounded-2xl border border-neutral-200 bg-white p-5">
              <div className="mb-4 flex items-center justify-between">
                <span className="text-[11px] font-black uppercase tracking-[0.14em] text-neutral-400">
                  Live stock
                </span>
                <CountdownPill ms={untilEnd} label="Ends in" />
              </div>

              <div
                className={`tabular text-5xl font-black tracking-tight transition-colors duration-200 ${
                  flash ? 'text-red-600' : soldOut ? 'text-neutral-300' : 'text-neutral-900'
                }`}
              >
                {sale.available}
                <span className="ml-2 text-lg font-bold text-neutral-400">
                  / {sale.total_stock} left
                </span>
              </div>

              <StockBar available={sale.available} total={sale.total_stock} className="mt-4" />

              <p className="mt-3 text-xs text-neutral-400">
                Updating live every {POLL_MS}ms — watch it drop as others buy.
              </p>
            </div>
          )}

          <button
            onClick={buy}
            disabled={!canBuy}
            className={`w-full rounded-2xl px-6 py-5 text-lg font-black uppercase tracking-wider transition active:scale-[.98] ${
              canBuy
                ? 'bg-red-600 text-white shadow-lg shadow-red-600/25 hover:bg-red-700'
                : 'cursor-not-allowed bg-neutral-200 text-neutral-400'
            }`}
          >
            {label}
          </button>

          {busy && queue?.phase === 'queued' && (
            <div className="-mt-2 rounded-xl border border-amber-200 bg-amber-50 p-3 text-center">
              <div className="text-[10px] font-black uppercase tracking-[0.14em] text-amber-600">
                Waiting room
              </div>
              <p className="mt-1 text-sm font-bold text-amber-900">
                {queue.position > 1
                  ? `You're #${queue.position} in line`
                  : 'You’re next — hold on'}
                {queue.etaS ? ` · ~${queue.etaS}s` : ''}
              </p>
              <p className="mt-1 text-[11px] text-amber-700/70">
                Buyers are released to checkout in small batches, so the
                database never sees the whole crowd at once.
              </p>
            </div>
          )}

          {notStarted && (
            <p className="-mt-2 text-center text-xs text-neutral-400">
              Synchronized to the server clock — every device unlocks together.
            </p>
          )}

          {/* Hidden admin strip -- only rendered with ?admin=1 */}
          {adminRef.current && (
            <div className="rounded-xl border border-dashed border-neutral-300 bg-neutral-50 p-3">
              <div className="mb-2 text-[10px] font-black uppercase tracking-[0.14em] text-neutral-400">
                Admin · reset stock
              </div>
              <div className="flex items-center gap-2">
                {[1, 3, 5].map((n) => (
                  <button
                    key={n}
                    onClick={() => setResetStock(n)}
                    className={`h-9 w-9 rounded-lg text-sm font-black transition ${
                      resetStock === n
                        ? 'bg-neutral-900 text-white'
                        : 'bg-white text-neutral-500 ring-1 ring-neutral-200'
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
                  className="ml-auto rounded-lg bg-neutral-900 px-4 py-2 text-xs font-black uppercase tracking-wider text-white transition hover:bg-neutral-700 disabled:opacity-50"
                >
                  {resetting ? 'Resetting…' : `Reset to ${resetStock}`}
                </button>
              </div>
            </div>
          )}

          <Link
            to="/"
            className="text-center text-xs font-bold uppercase tracking-widest text-neutral-400 hover:text-neutral-900"
          >
            ← All flash sales
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
      <Header active="shop" />
      <main className="mx-auto max-w-5xl px-4 py-6 sm:py-10">{children}</main>
    </div>
  )
}
