import { useEffect, useState } from 'react'
import { serverNow } from '../api.js'
import { categoryStyle, formatDuration, isWishlisted, money, toggleWishlist } from '../lib/demo.js'

/* ---------------- Countdown driven by the SERVER clock ---------------- */

/**
 * Ticks 10x/sec off serverNow(), not Date.now(), so every device counts down
 * to the same instant. Returns ms remaining (never negative).
 */
export function useServerCountdown(targetIso) {
  const target = targetIso ? new Date(targetIso).getTime() : null
  const [remaining, setRemaining] = useState(() =>
    target ? Math.max(0, target - serverNow()) : 0,
  )

  useEffect(() => {
    if (!target) return
    const tick = () => setRemaining(Math.max(0, target - serverNow()))
    tick()
    const id = setInterval(tick, 100)
    return () => clearInterval(id)
  }, [target])

  return remaining
}

export function FlashBadge({ children = 'Flash Sale', className = '' }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded bg-red-600 px-2.5 py-1 text-[10px] font-black uppercase tracking-[0.12em] text-white ${className}`}
    >
      <span className="relative flex h-1.5 w-1.5">
        <span className="absolute inline-flex h-full w-full animate-pulse-ring rounded-full bg-white" />
        <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-white" />
      </span>
      {children}
    </span>
  )
}

export function DiscountBadge({ pct }) {
  if (!pct || pct <= 0) return null
  return (
    <span className="rounded bg-neutral-900 px-2 py-1 text-[10px] font-black uppercase tracking-[0.1em] text-white">
      -{pct}%
    </span>
  )
}

/** Countdown pill. Turns red + urgent under a minute. */
export function CountdownPill({ ms, label, className = '' }) {
  const urgent = ms > 0 && ms < 60_000
  return (
    <span
      className={`inline-flex items-baseline gap-2 rounded-lg px-3 py-1.5 tabular ${
        urgent ? 'bg-red-600 text-white' : 'bg-neutral-900/85 text-white'
      } ${className}`}
    >
      {label && (
        <span className="text-[10px] font-bold uppercase tracking-[0.12em] opacity-75">{label}</span>
      )}
      <span className={`font-black ${urgent ? 'animate-pulse' : ''}`}>{formatDuration(ms)}</span>
    </span>
  )
}

/** Stock progress bar — fills red as the item runs out. */
export function StockBar({ available, total, className = '' }) {
  const pct = total > 0 ? Math.max(0, Math.min(100, (available / total) * 100)) : 0
  const low = pct <= 34
  const gone = available <= 0
  return (
    <div className={className}>
      <div className="mb-1.5 flex items-baseline justify-between">
        <span className="text-xs font-bold uppercase tracking-wider text-neutral-500">
          {gone ? 'Sold out' : `Only ${available} left`}
        </span>
        <span className="tabular text-xs text-neutral-400">
          {available}/{total}
        </span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-neutral-200">
        <div
          className={`h-full rounded-full transition-[width] duration-500 ease-out ${
            gone ? 'bg-neutral-400' : low ? 'bg-red-600' : 'bg-neutral-900'
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

/** Heart toggle -- see the wishlist helpers in lib/demo.js for what backs it. */
export function WishlistButton({ saleId, className = '' }) {
  const [on, setOn] = useState(() => isWishlisted(saleId))

  return (
    <button
      type="button"
      aria-label={on ? 'Remove from wishlist' : 'Add to wishlist'}
      aria-pressed={on}
      onClick={(e) => {
        e.preventDefault()
        e.stopPropagation()
        toggleWishlist(saleId)
        setOn((v) => !v)
      }}
      className={`grid h-8 w-8 place-items-center rounded-full bg-black/40 backdrop-blur transition hover:bg-black/60 active:scale-90 ${className}`}
    >
      <svg
        viewBox="0 0 20 20"
        className={`h-4 w-4 transition ${on ? 'fill-red-500 stroke-red-500' : 'fill-none stroke-white'}`}
        strokeWidth="1.8"
      >
        <path
          d="M10 17s-6.5-4.02-6.5-8.5A3.75 3.75 0 0110 6a3.75 3.75 0 016.5 2.5C16.5 12.98 10 17 10 17z"
          strokeLinejoin="round"
        />
      </svg>
    </button>
  )
}

/** Gradient + glyph stand-in for product photography. */
export function ProductArt({ category, className = '', glyphClass = 'text-6xl' }) {
  const { from, to, glyph } = categoryStyle(category)
  return (
    <div
      className={`relative flex items-center justify-center overflow-hidden bg-gradient-to-br ${from} ${to} ${className}`}
    >
      <div className="absolute inset-0 opacity-25 [background:radial-gradient(circle_at_30%_20%,white,transparent_55%)]" />
      <span className={`relative drop-shadow-lg ${glyphClass}`}>{glyph}</span>
    </div>
  )
}

/** Star rating + review count -- see seededRating() in lib/demo.js for where the numbers come from. */
export function StarRating({ stars, count, className = '', dark = false }) {
  const pct = Math.round((stars / 5) * 100)
  return (
    <div className={`flex items-center gap-1.5 ${className}`}>
      <div className="relative text-xs leading-none tracking-[1px]">
        <span className={dark ? 'text-white/25' : 'text-neutral-300'}>★★★★★</span>
        <span
          className="absolute inset-0 overflow-hidden whitespace-nowrap text-amber-400"
          style={{ width: `${pct}%` }}
        >
          ★★★★★
        </span>
      </div>
      <span className={`text-[11px] font-bold ${dark ? 'text-white/70' : 'text-neutral-500'}`}>
        {stars.toFixed(1)}
        {count != null && <span className="font-medium opacity-70"> · {count.toLocaleString()}</span>}
      </span>
    </div>
  )
}

const TRUST_ITEMS = [
  { glyph: '🚚', label: 'Delivered in 2-4 days' },
  { glyph: '↩️', label: '7-day easy returns' },
  { glyph: '🔒', label: 'Secure checkout' },
]

/** Row of trust-signal chips -- delivery/returns/payment. Every real
 * ecommerce PDP has this row; it costs nothing and reads as real polish. */
export function TrustBadges({ className = '' }) {
  return (
    <div className={`flex flex-wrap gap-x-4 gap-y-1.5 text-[11px] font-semibold text-neutral-500 ${className}`}>
      {TRUST_ITEMS.map((t) => (
        <span key={t.label} className="inline-flex items-center gap-1.5">
          <span aria-hidden>{t.glyph}</span>
          {t.label}
        </span>
      ))}
    </div>
  )
}

export function PriceRow({ base, sale, size = 'md' }) {
  const big = size === 'lg'
  return (
    <div className="flex items-baseline gap-2">
      <span className={`font-black tracking-tight ${big ? 'text-3xl' : 'text-xl'}`}>{money(sale)}</span>
      {base > sale && (
        <span className={`text-neutral-400 line-through ${big ? 'text-lg' : 'text-sm'}`}>
          {money(base)}
        </span>
      )}
    </div>
  )
}
