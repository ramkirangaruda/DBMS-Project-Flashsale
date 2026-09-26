import { useEffect } from 'react'
import { CheckCircle, WarningCircle, XCircle } from '@phosphor-icons/react'

/**
 * Full-screen outcome. Three distinct looks so the result is unmistakable
 * across a room of people holding phones:
 *   confirmed -> success, calm affirmation
 *   sold_out  -> danger, "beaten by milliseconds"
 *   failed    -> accent, retryable
 */
const LOOKS = {
  confirmed: {
    tone: 'bg-success text-success-ink',
    icon: CheckCircle,
    title: 'You got it',
  },
  sold_out: {
    tone: 'bg-danger text-danger-ink',
    icon: XCircle,
    title: 'Sold out',
  },
  failed: {
    tone: 'bg-accent text-accent-ink',
    icon: WarningCircle,
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
  const Icon = look.icon
  const shortId = result.order_id ? result.order_id.slice(0, 8).toUpperCase() : null

  return (
    <div
      className={`fixed inset-0 z-50 flex items-center justify-center px-6 ${look.tone}`}
      role="alertdialog"
      aria-modal="true"
      aria-label={look.title}
    >
      <div className="w-full max-w-sm text-center">
        <div className="animate-pop mx-auto flex h-20 w-20 items-center justify-center rounded-full bg-white/15">
          <Icon weight="bold" size={40} />
        </div>

        <h2 className="animate-slide-up mt-6 text-4xl font-semibold tracking-tight sm:text-5xl">
          {look.title}
        </h2>

        <p className="animate-slide-up mt-3 text-base opacity-85 sm:text-lg">{result.message}</p>

        {shortId && (
          <div className="animate-slide-up mt-7 inline-block rounded-2xl bg-black/15 px-5 py-3 backdrop-blur">
            <div className="text-[11px] font-medium uppercase tracking-[0.1em] opacity-70">
              Order
            </div>
            <div className="tabular mt-0.5 font-mono text-xl font-semibold">#{shortId}</div>
          </div>
        )}

        <div className="animate-slide-up mt-9 flex flex-col gap-3 sm:flex-row sm:justify-center">
          {result.status !== 'confirmed' && onRetry && (
            <button
              onClick={onRetry}
              className="rounded-full bg-white px-6 py-3 text-sm font-semibold text-neutral-900 transition hover:bg-white/90 active:scale-95"
            >
              Try again
            </button>
          )}
          <button
            onClick={onClose}
            className="rounded-full border border-white/40 px-6 py-3 text-sm font-semibold transition hover:bg-white/10 active:scale-95"
          >
            {result.status === 'confirmed' ? 'Keep shopping' : 'Back to sale'}
          </button>
        </div>

        {/* Which concurrency strategy served this. Tiny and unobtrusive --
            useful when demoing, invisible to anyone not looking for it. */}
        {result.strategy && (
          <div className="mt-8 text-[11px] uppercase tracking-[0.1em] opacity-40">
            {result.strategy} strategy
          </div>
        )}
      </div>
    </div>
  )
}
