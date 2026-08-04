import { useEffect } from 'react'

/**
 * Full-screen outcome. Three distinct looks so the result is unmistakable
 * across a room of people holding phones:
 *   confirmed -> green, celebratory
 *   sold_out  -> deep red, "beaten by milliseconds"
 *   failed    -> amber, retryable
 */
const LOOKS = {
  confirmed: {
    bg: 'bg-gradient-to-br from-emerald-500 via-emerald-600 to-teal-700',
    glyph: '🎉',
    title: 'You got it!',
  },
  sold_out: {
    bg: 'bg-gradient-to-br from-rose-600 via-red-700 to-neutral-900',
    glyph: '😢',
    title: 'Sold out',
  },
  failed: {
    bg: 'bg-gradient-to-br from-amber-500 via-orange-600 to-red-700',
    glyph: '⚠️',
    title: 'Checkout failed',
  },
}

export default function ResultOverlay({ result, onClose, onRetry }) {
  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = ''
    }
  }, [onClose])

  if (!result) return null
  const look = LOOKS[result.status] || LOOKS.failed
  const shortId = result.order_id ? result.order_id.slice(0, 8).toUpperCase() : null

  return (
    <div
      className={`fixed inset-0 z-50 flex items-center justify-center px-6 text-white ${look.bg}`}
      role="alertdialog"
      aria-modal="true"
      aria-label={look.title}
    >
      <div className="w-full max-w-md text-center">
        <div className="animate-pop text-7xl sm:text-8xl">{look.glyph}</div>

        <h2 className="animate-slide-up mt-6 text-4xl font-black tracking-tight sm:text-5xl">
          {look.title}
        </h2>

        <p className="animate-slide-up mt-3 text-base text-white/85 sm:text-lg">{result.message}</p>

        {shortId && (
          <div className="animate-slide-up mt-7 inline-block rounded-xl bg-black/25 px-5 py-3 backdrop-blur">
            <div className="text-[10px] font-bold uppercase tracking-[0.16em] text-white/60">
              Order
            </div>
            <div className="tabular mt-0.5 font-mono text-xl font-bold">#{shortId}</div>
          </div>
        )}

        <div className="animate-slide-up mt-9 flex flex-col gap-3 sm:flex-row sm:justify-center">
          {result.status !== 'confirmed' && onRetry && (
            <button
              onClick={onRetry}
              className="rounded-xl bg-white px-6 py-3 text-sm font-black uppercase tracking-wider text-neutral-900 transition hover:bg-white/90 active:scale-95"
            >
              Try again
            </button>
          )}
          <button
            onClick={onClose}
            className="rounded-xl border-2 border-white/45 px-6 py-3 text-sm font-black uppercase tracking-wider text-white transition hover:bg-white/10 active:scale-95"
          >
            {result.status === 'confirmed' ? 'Keep shopping' : 'Back to sale'}
          </button>
        </div>

        {/* Which concurrency strategy served this. Tiny and unobtrusive --
            useful when demoing, invisible to anyone not looking for it. */}
        {result.strategy && (
          <div className="mt-8 text-[10px] uppercase tracking-[0.16em] text-white/40">
            {result.strategy} strategy
          </div>
        )}
      </div>
    </div>
  )
}
