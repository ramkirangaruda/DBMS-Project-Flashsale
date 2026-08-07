// k6 load driver for the flash-sale drop.
//
// WHY THIS EXISTS ALONGSIDE scripts/chaos_verification.py
//   The Python harness stopped being able to measure the system once the API
//   tier was scaled out. At N=600 it reported buyers waiting 25.2s in the
//   queue while the server's own histogram said p50 0.22s / p99 0.50s -- a
//   ~50x gap that was entirely the load generator: one Python process,
//   1000 coroutines, ~130% CPU, unable to issue and read requests fast
//   enough. It was measuring itself.
//
//   k6 is Go, and its VUs are real concurrency rather than coroutines
//   sharing one interpreter. This script exists ONLY to generate load.
//   chaos_verification.py remains the correctness tool -- it still owns the
//   stock invariant, counted from the orders table -- and is not modified.
//
// WHAT ONE VU DOES
//   queued mode:  POST /queue/join -> poll GET /queue/status until admitted
//                 -> POST /checkout/{strategy} with X-Admission-Token
//   raw mode:     POST /checkout/{strategy} immediately, no token
//                 (needs the server running with ADMISSION_ENFORCED=false)
//
// Run via results/load/run_k6.py, which handles stock reset, user ids, the
// order-table counting k6 cannot do, and the before/after bookkeeping.

import http from 'k6/http';
import { sleep } from 'k6';
import { SharedArray } from 'k6/data';
import { Counter, Trend } from 'k6/metrics';

const users = new SharedArray('users', () => JSON.parse(open('./users.json')));

const BASE      = __ENV.BASE_URL   || 'http://nginx:80';
const STRATEGY  = __ENV.STRATEGY   || 'pessimistic';
const SALE_ID   = __ENV.SALE_ID;
const USE_QUEUE = (__ENV.USE_QUEUE || 'true') === 'true';
const N         = parseInt(__ENV.N || '1000', 10);
const NONCE     = __ENV.RUN_NONCE  || 'k6';
const MAX_POLLS = parseInt(__ENV.MAX_POLLS || '400', 10);

// Outcomes, parsed from the uniform checkout body the API has returned since
// the storefront phase: {status, order_id, message, strategy}.
const confirmed       = new Counter('fs_confirmed');
const soldOut         = new Counter('fs_sold_out');
const versionConflict = new Counter('fs_version_conflict');
const refused403      = new Counter('fs_refused_403');
const otherFailure    = new Counter('fs_other_failure');
const neverAdmitted   = new Counter('fs_never_admitted');
const joinFailed      = new Counter('fs_join_failed');

const checkoutMs  = new Trend('fs_checkout_ms', true);
const queueWaitMs = new Trend('fs_queue_wait_ms', true);
const pollCount   = new Trend('fs_polls');

export const options = {
  scenarios: {
    drop: {
      // Every buyer arrives at once and buys once -- a drop, not a
      // sustained arrival rate. VUs are pre-allocated so the burst is real
      // rather than a ramp.
      executor: 'per-vu-iterations',
      vus: N,
      iterations: 1,
      maxDuration: __ENV.MAX_DURATION || '15m',
      gracefulStop: '60s',
    },
  },
  // The interesting outcomes here are 409 (sold out / version conflict) and
  // 403 (not admitted). Those are the system working, not transport errors,
  // so k6 must not count them as failed requests.
  thresholds: {},
  discardResponseBodies: false,
  noConnectionReuse: false,
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

function acquireAdmission() {
  const j = http.post(`${BASE}/queue/join/${SALE_ID}`, null, {
    tags: { phase: 'join' },
  });
  if (j.status !== 200) {
    joinFailed.add(1);
    return null;
  }
  let body;
  try { body = j.json(); } catch (e) { joinFailed.add(1); return null; }

  const token = body.queue_token;
  let est = body.estimated_wait_s || 0.5;
  const t0 = Date.now();

  for (let i = 0; i < MAX_POLLS; i++) {
    // Poll interval derived from the server's own estimate, exactly as the
    // Python harness did: a buyer 900 places back sleeps seconds, one near
    // the front sleeps milliseconds. A thousand VUs polling tightly would
    // rebuild the stampede against /queue/status instead of /checkout.
    sleep(Math.min(Math.max(est * 0.5, 0.2), 3.0));

    const s = http.get(`${BASE}/queue/status/${SALE_ID}?token=${token}`, {
      tags: { phase: 'poll' },
    });
    if (s.status !== 200) continue;

    let sb;
    try { sb = s.json(); } catch (e) { continue; }

    if (sb.admitted) {
      queueWaitMs.add(Date.now() - t0);
      pollCount.add(i + 1);
      return sb.admission_token;
    }
    if (sb.expired) break;
    est = sb.estimated_wait_s || 0.5;
  }

  queueWaitMs.add(Date.now() - t0);
  neverAdmitted.add(1);
  return null;
}

export default function () {
  const uid = users[(__VU - 1) % users.length];

  let token = null;
  if (USE_QUEUE) {
    token = acquireAdmission();
    if (!token) return;
  }

  const headers = {
    // One key per logical buyer, fresh per invocation -- same reasoning as
    // make_idem_key() in the Python harness. Reused keys would replay the
    // previous run's 24h cache and create zero orders.
    'Idempotency-Key': `${NONCE}-${STRATEGY}-${__VU}`,
  };
  if (token) headers['X-Admission-Token'] = token;

  const res = http.post(
    `${BASE}/checkout/${STRATEGY}?sale_id=${SALE_ID}&user_id=${uid}`,
    null,
    { headers, tags: { phase: 'checkout', strategy: STRATEGY } },
  );

  checkoutMs.add(res.timings.duration);

  if (res.status === 403) { refused403.add(1); return; }

  let body = {};
  try { body = res.json(); } catch (e) { /* non-JSON: counted below */ }

  const status = body.status || '';
  const message = (body.message || '').toLowerCase();

  if (status === 'confirmed')      confirmed.add(1);
  else if (status === 'sold_out')  soldOut.add(1);
  else if (message.includes('version conflict')) versionConflict.add(1);
  else                             otherFailure.add(1);
}

export function handleSummary(data) {
  // Written next to the script so run_k6.py can read exact numbers rather
  // than scraping k6's pretty-printed stdout.
  const out = {};
  out[__ENV.SUMMARY_PATH || '/scripts/summary.json'] = JSON.stringify(data, null, 2);
  out['stdout'] = textSummaryLine(data);
  return out;
}

function textSummaryLine(data) {
  const m = data.metrics;
  const c = (k) => (m[k] && m[k].values ? (m[k].values.count || 0) : 0);
  const t = (k, s) => (m[k] && m[k].values ? (m[k].values[s] || 0).toFixed(1) : '--');
  return [
    '',
    `  strategy=${STRATEGY} queue=${USE_QUEUE} vus=${N}`,
    `  confirmed=${c('fs_confirmed')} sold_out=${c('fs_sold_out')} ` +
      `version_conflict=${c('fs_version_conflict')} refused_403=${c('fs_refused_403')} ` +
      `other_failure=${c('fs_other_failure')} never_admitted=${c('fs_never_admitted')} ` +
      `join_failed=${c('fs_join_failed')}`,
    `  checkout ms  p50=${t('fs_checkout_ms', 'med')} p95=${t('fs_checkout_ms', 'p(95)')} ` +
      `p99=${t('fs_checkout_ms', 'p(99)')} max=${t('fs_checkout_ms', 'max')}`,
    `  queue wait ms p50=${t('fs_queue_wait_ms', 'med')} p95=${t('fs_queue_wait_ms', 'p(95)')}`,
    '',
  ].join('\n');
}
