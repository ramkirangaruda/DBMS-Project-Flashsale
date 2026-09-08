/**
 * Per-device demo identity + the hidden strategy switch.
 */

/**
 * scripts/seed.py seeds 30 demo users with FIXED ids of this shape
 * (see DEMO_USER_IDS there). They are constants by design, so deriving them
 * here needs no extra endpoint.
 *
 * Each browser picks one at random and keeps it, so two tabs / two phones
 * check out as different users -- which is what makes the race realistic
 * rather than one user hammering the same row.
 */
const N_DEMO_USERS = 30
const userIdFor = (n) => `d0000000-0000-4000-8000-${String(n).padStart(12, '0')}`

const STORAGE_KEY = 'flashsale.demoUserId'

export function getDemoUserId() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved) return saved
    const fresh = userIdFor(Math.floor(Math.random() * N_DEMO_USERS))
    localStorage.setItem(STORAGE_KEY, fresh)
    return fresh
  } catch {
    // Private mode / storage blocked -- fall back to a per-load identity.
    return userIdFor(Math.floor(Math.random() * N_DEMO_USERS))
  }
}

/**
 * Which concurrency strategy Buy Now hits.
 *
 * Read ONCE from ?strategy=pessimistic|optimistic|redis and deliberately
 * never surfaced in the UI -- ordinary viewers are just shopping; only the
 * person running the demo knows the switch exists. Defaults to redis.
 */
export function getStrategy() {
  const valid = ['pessimistic', 'optimistic', 'redis']
  try {
    const q = new URLSearchParams(window.location.search).get('strategy')
    if (q && valid.includes(q.toLowerCase())) return q.toLowerCase()
  } catch {
    /* ignore */
  }
  return 'redis'
}

/**
 * Admin mode, same hidden-switch pattern as the strategy toggle: only the
 * person running the demo knows ?admin=1 exists. Reveals a stock-reset
 * control so a drop can be re-run between rounds without a terminal --
 * useful when the laptop is plugged into a projector and everyone is
 * waiting.
 */
export function getAdminMode() {
  try {
    const q = new URLSearchParams(window.location.search)
    return q.get('admin') === '1'
  } catch {
    return false
  }
}

/** Optional ADMIN_TOKEN, passed through as ?token= if the server requires one. */
export function getAdminToken() {
  try {
    return new URLSearchParams(window.location.search).get('token') || ''
  } catch {
    return ''
  }
}

/** Visual identity for a product category -- gradient + glyph, no photos needed. */
const CATEGORY_STYLES = {
  earbuds:    { from: 'from-violet-500',  to: 'to-fuchsia-600', glyph: '🎧' },
  phone:      { from: 'from-sky-500',     to: 'to-blue-700',    glyph: '📱' },
  watch:      { from: 'from-emerald-500', to: 'to-teal-700',    glyph: '⌚' },
  joystick:   { from: 'from-orange-500',  to: 'to-red-600',     glyph: '🎮' },
  headphone:  { from: 'from-indigo-500',  to: 'to-purple-700',  glyph: '🎵' },
  camera:     { from: 'from-amber-500',   to: 'to-orange-700',  glyph: '📷' },
  footwear:   { from: 'from-rose-500',    to: 'to-pink-700',    glyph: '👟' },
  apparel:    { from: 'from-cyan-500',    to: 'to-sky-700',     glyph: '👕' },
  backpack:   { from: 'from-lime-500',    to: 'to-green-700',   glyph: '🎒' },
  sunglasses: { from: 'from-yellow-500',  to: 'to-amber-700',   glyph: '🕶️' },
  drone:      { from: 'from-slate-500',   to: 'to-neutral-800', glyph: '🛸' },
  speaker:    { from: 'from-fuchsia-500', to: 'to-purple-800',  glyph: '🔊' },
  laptop:     { from: 'from-blue-500',    to: 'to-indigo-800',  glyph: '💻' },
  console:    { from: 'from-red-500',     to: 'to-rose-800',    glyph: '🕹️' },
  luggage:    { from: 'from-teal-500',    to: 'to-cyan-800',    glyph: '🧳' },
  keyboard:   { from: 'from-violet-600',  to: 'to-indigo-900',  glyph: '⌨️' },
}
const FALLBACK = { from: 'from-neutral-600', to: 'to-neutral-800', glyph: '⚡' }

export function categoryStyle(category) {
  return CATEGORY_STYLES[(category || '').toLowerCase()] || FALLBACK
}

/** ms -> "04:12" / "1:02:33"; clamped at zero. */
export function formatDuration(ms) {
  const total = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const pad = (n) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`
}

export const money = (n) =>
  `$${Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

/**
 * Deterministic star rating + review count for a product, seeded off its
 * sale_id so it's stable across renders and reloads instead of jumping
 * around every poll. There's no reviews table behind this -- flash-sale
 * items are, definitionally, too new to have any -- so this is storefront
 * dressing, the same category as the search bar in Header.jsx: it makes
 * the page read like a shop instead of an API response, without pretending
 * to be real user data anywhere it could mislead (no reviewer names, no
 * review text, just the aggregate a listing page shows before you click in).
 */
function seed32(str) {
  let h = 2166136261
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

export function seededRating(id) {
  const h = seed32(String(id))
  const stars = Math.round((3.9 + (h % 100) / 100) * 10) / 10 // 3.9 - 4.9
  const count = 80 + ((h >>> 8) % 4200) // 80 - 4279
  return { stars, count }
}
