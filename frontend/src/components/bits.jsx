import { useEffect, useState } from 'react'
import {
  Camera, DeviceMobile, GameController, Headphones, Lightning,
  MusicNotes, Sneaker, Watch,
} from '@phosphor-icons/react'
import { serverNow } from '../api.js'
import { categoryStyle, formatDuration, money } from '../lib/demo.js'

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

/* ---------------- badges & pills ---------------- */

export function FlashBadge({ children = 'Flash sale', className = '' }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full bg-ink px-2.5 py-1 text-[11px] font-semibold text-page ${className}`}
    >
      <span className="relative flex h-1.5 w-1.5">
        <span className="absolute inline-flex h-full w-full animate-pulse-ring rounded-full bg-accent" />
        <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent" />
      </span>
      {children}
    </span>
  )
}

export function DiscountBadge({ pct }) {
  if (!pct || pct <= 0) return null
  return (
    <span className="rounded-full bg-accent px-2.5 py-1 text-[11px] font-semibold text-accent-ink">
      {pct}% off
    </span>
  )
}

/** Countdown pill. Shifts to the accent, then to danger, as time runs out. */
export function CountdownPill({ ms, label, className = '' }) {
  const critical = ms > 0 && ms < 15_000
  const urgent = ms > 0 && ms < 60_000

  const tone = critical
    ? 'bg-danger text-danger-ink'
    : urgent
      ? 'bg-accent text-accent-ink'
      : 'bg-surface-2 text-ink'

  return (
    <span
      className={`tabular inline-flex items-baseline gap-2 rounded-full px-3 py-1.5 text-sm ${tone} ${className}`}
    >
      {label && (
        <span className="text-[11px] font-medium opacity-70">{label}</span>
      )}
      <span className={`font-semibold ${critical ? 'animate-pulse' : ''}`}>
        {formatDuration(ms)}
      </span>
    </span>
  )
}

/** Stock progress bar. Fills with the accent as stock runs low. */
export function StockBar({ available, total, className = '' }) {
  const pct = total > 0 ? Math.max(0, Math.min(100, (available / total) * 100)) : 0
  const low = pct <= 34
  const gone = available <= 0
  return (
    <div className={className}>
      <div className="mb-1.5 flex items-baseline justify-between">
        <span className="text-xs font-medium text-ink-2">
          {gone ? 'Sold out' : `${available} left`}
        </span>
        <span className="tabular text-xs text-muted">
          {available}/{total}
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-surface-2">
        <div
          className={`h-full rounded-full transition-[width] duration-500 ease-out ${
            gone ? 'bg-muted' : low ? 'bg-danger' : 'bg-ink'
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

/* ---------------- product art ---------------- */

const CATEGORY_ICON = {
  earbuds: Headphones,
  phone: DeviceMobile,
  watch: Watch,
  joystick: GameController,
  headphone: MusicNotes,
  camera: Camera,
  footwear: Sneaker,
}

/** Calm gradient + a single line icon, standing in for product photography. */
export function ProductArt({ category, className = '', iconSize = 96 }) {
  const { from, to } = categoryStyle(category)
  const Icon = CATEGORY_ICON[(category || '').toLowerCase()] || Lightning
  return (
    <div
      className={`relative flex items-center justify-center overflow-hidden bg-gradient-to-br ${from} ${to} ${className}`}
    >
      <div className="absolute inset-0 opacity-20 [background:radial-gradient(circle_at_28%_20%,white,transparent_55%)]" />
      <Icon weight="light" size={iconSize} className="relative text-white/90" />
    </div>
  )
}

export function PriceRow({ base, sale, size = 'md' }) {
  const big = size === 'lg'
  return (
    <div className="flex items-baseline gap-2">
      <span className={`tabular font-semibold tracking-tight ${big ? 'text-3xl' : 'text-lg'}`}>
        {money(sale)}
      </span>
      {base > sale && (
        <span className={`tabular text-muted line-through ${big ? 'text-base' : 'text-sm'}`}>
          {money(base)}
        </span>
      )}
    </div>
  )
}
