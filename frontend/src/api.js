/**
 * Backend access + the server-clock offset that the whole synchronized drop
 * depends on.
 *
 * In dev, requests go to /api and Vite proxies them to FastAPI (see
 * vite.config.js). Set VITE_API_BASE to point somewhere else -- e.g.
 * "http://192.168.1.20:8010" when loading the site on a phone.
 */
const BASE = import.meta.env.VITE_API_BASE ?? '/api'

/* ------------------------------------------------------------------ *
 * Server clock
 *
 * Never trust the device clock for the drop. Two phones can disagree by
 * several seconds, which would unlock Buy Now at visibly different moments
 * and quietly destroy the race we are trying to demonstrate.
 *
 * Every payload carries `server_time`. On each response we recompute
 *     offset = server_time - local_time
 * (minus half the round trip, so we are not systematically late), and all
 * countdowns run on `serverNow()` instead of Date.now(). Every device then
 * converges on the same instant regardless of how wrong its own clock is.
 * ------------------------------------------------------------------ */
let clockOffsetMs = 0
let synced = false

export function noteServerTime(iso, roundTripMs = 0) {
  if (!iso) return
  const serverMs = new Date(iso).getTime()
  if (Number.isNaN(serverMs)) return
  // The timestamp was produced roughly halfway through the round trip.
  clockOffsetMs = serverMs + roundTripMs / 2 - Date.now()
  synced = true
}

/** "Now" according to the server, in ms. */
export function serverNow() {
  return Date.now() + clockOffsetMs
}

export function isClockSynced() {
  return synced
}

export function getClockOffsetMs() {
  return clockOffsetMs
}

async function request(path, options) {
  const started = performance.now()
  const res = await fetch(`${BASE}${path}`, options)
  const rtt = performance.now() - started
  let data = null
  try {
    data = await res.json()
  } catch {
    data = null
  }
  if (data && data.server_time) noteServerTime(data.server_time, rtt)
  return { res, data }
}

export async function fetchSales() {
  const { res, data } = await request('/sales')
  if (!res.ok) throw new Error(`GET /sales failed (${res.status})`)
  return data
}

export async function fetchSale(saleId) {
  const { res, data } = await request(`/sales/${saleId}`)
  if (res.status === 404) throw new Error('Sale not found')
  if (!res.ok) throw new Error(`GET /sales/${saleId} failed (${res.status})`)
  return data
}

export const STRATEGIES = ['pessimistic', 'optimistic', 'redis']

/**
 * POST a checkout. Every strategy returns the same body shape
 * ({status, order_id, message, strategy}) on success AND on failure, so this
 * never has to branch on which endpoint it hit -- only on `status`.
 */
export async function checkout({ saleId, userId, strategy }) {
  const s = STRATEGIES.includes(strategy) ? strategy : 'redis'
  const qs = new URLSearchParams({ sale_id: saleId, user_id: userId })
  const { res, data } = await request(`/checkout/${s}?${qs}`, { method: 'POST' })

  if (data && data.status) return data
  // Network-level or unexpected failure -- normalise into the same shape.
  return {
    status: 'failed',
    order_id: null,
    message: `Checkout failed (HTTP ${res.status}).`,
    strategy: s,
  }
}
