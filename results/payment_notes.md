# Payment step (Stripe test mode) — durable architecture, real reconciliation evidence, live-Stripe verification still pending

This supersedes the first version of this file. The payment step was
rebuilt on top of the original implementation to close two of the
architectural gaps identified after that phase shipped:

1. **Payment state now lives in Postgres, not Redis.** The original design
   stored `payment:{order_id}` as a 7-day-TTL Redis key — a deliberate
   scoping choice for that phase, but one that meant there was no durable
   record anywhere that money had changed hands. A new `payments` table
   replaces it entirely.
2. **A declined payment now actually releases the reserved unit.** The
   original version explicitly named this as out of scope. It is now
   implemented, strategy-aware, redelivery-safe, and verified against real
   Postgres and Redis (evidence below) — not against a live Stripe network
   call, which still needs a real key (see the section on that).

Code: [`app/models.py`](../app/models.py) (`Payment`, `Order.strategy`) ·
[`app/migrate.py`](../app/migrate.py) · [`app/payments.py`](../app/payments.py) ·
Verification driver: [`scripts/payment_verify.py`](../scripts/payment_verify.py)

---

## What changed, and why

### Payment state: Redis → Postgres

`payments` table, `(order_id, attempt)` unique. Every write — the
synchronous create/confirm path and the webhook handler alike — is an
`INSERT ... ON CONFLICT (order_id, attempt) DO UPDATE`, safe under
concurrent writers for the same reason the old Redis read-modify-write was
safe: every writer reports the same underlying Stripe truth for the same
PaymentIntent, never independent state that could disagree.

Adding this table needed no real migration framework: `Base.metadata.
create_all()` creates any model that doesn't exist yet, and `Payment` is a
brand-new model, so it just appears. `Order.strategy` is different — an
existing table needs an actual `ALTER TABLE`, which `create_all()` never
issues for a table it already knows about. `app/migrate.py` is one
idempotent `ADD COLUMN IF NOT EXISTS`, run from the startup event; see that
file's docstring for why this wasn't built as a real Alembic migration
(`alembic` has been in requirements.txt since early in the project but was
never actually initialized anywhere in this repo — discovered while
researching this fix, not assumed).

**Verified against real Postgres**, not just written:
```
docker compose exec postgres psql ... \d payments
docker compose exec postgres psql ... \d orders   (strategy column present)
```
ran `create_all()` + `migrate.run()` three times in a row with no error —
confirms the ALTER really is a no-op on every call after the first.

### Idempotency key: fixed → attempt-counter

The original key was `flashsale-order-{order_id}-payment-create` — a pure
function of order_id, named at the time as a limitation: Stripe caches an
idempotency key's response for 24h, so retrying after a genuine decline
just replayed the same cached decline rather than reaching the card network
again.

The key is now `flashsale-order-{order_id}-attempt-{attempt}-payment-
create`. `attempt` comes from `_next_attempt()`: the same number as the
latest attempt if none exists yet, or if the latest is still succeeded/
in-flight (so concurrent duplicates of ONE attempt still share a key and
still dedupe through Stripe exactly as before); the NEXT number if the
latest is a definitive `failed` (so a genuine retry gets a fresh key).

**Verified directly against a real declined-then-retried order:**
```
order previously failed at attempt 1 -> next attempt is: 2
fresh idempotency key for the retry: flashsale-order-...-attempt-2-payment-create
OLD (attempt 1) key would have been: flashsale-order-...-attempt-1-payment-create
keys differ: True
```

---

## Stock reconciliation on decline — real evidence, not a description of intent

This is the part worth reading carefully, since it's the biggest actual gap
this pass closed. All three checks below ran against the live stack
(Postgres + Redis via `docker compose`), using a synthetic webhook payload
shaped exactly like Stripe's real `payment_intent.payment_failed` event
(same `id`, `type`, `data.object.{id,amount,currency,metadata,
last_payment_error}` fields Stripe sends) — the ONE piece this could not
exercise is Stripe's own network round trip, since that needs the still-
missing real key. Everything from "a decline event arrives at
`/payment/webhook`" onward is real: real HTTP request, real Postgres writes,
real Redis writes, real audit log rows.

### 1. Pessimistic/optimistic strategy — releases `inventory.reserved_stock`

A fresh order was driven through the real queue and `/checkout/pessimistic`.

| | before | after the decline webhook |
|---|---|---|
| `orders.strategy` | — | `pessimistic` |
| `inventory.reserved_stock` (this sale) | **1** | **0** |
| `payments.status` | `requires_payment_method` | `failed` |
| `payments.decline_code` / `failure_message` | — | `generic_decline` / "Your card was declined." |
| `stock_audit_log` | — | `payment_declined_release`, before=1, after=0, committed='true' |

### 2. redis strategy — releases the Redis `stock:{sale_id}` counter, NOT `reserved_stock`

A second fresh order, driven through `/checkout/redis` on the same sale.

| | before | after the decline webhook |
|---|---|---|
| `orders.strategy` | — | `redis` |
| `stock:{sale_id}` (Redis) | **9** (decremented by checkout) | **10** |
| `stock_audit_log` | — | `payment_declined_release_redis_counter`, before=9, after=10 |

This is the check that specifically proves the reconciliation is
**strategy-aware** rather than hardcoded to one counter: the same webhook
handler code path released a completely different value in a completely
different store, chosen by reading `orders.strategy`.

### 3. Redelivery safety — Stripe does not guarantee exactly-once webhook delivery

The IDENTICAL event from check 1 (same `event.id`, same intent id) was
POSTed to `/payment/webhook` a second time.

```
first delivery:  {"received":true,"handled":true, ...}                    HTTP 200
second delivery: {"received":true,"handled":false,"note":"already applied"} HTTP 200

reserved_stock after second delivery: 0   (NOT -1)
stock_audit_log rows for this release: 1  (NOT 2)
```

The guard is the conditional `UPDATE payments SET status = :status ...
WHERE id = :pid AND status != :status RETURNING id` — it returns a row
**only** on the transition into the new status, never on a webhook that
arrives after that transition already happened. The stock release is
gated entirely on that return value, so it structurally cannot run twice
for the same failure, regardless of how many times Stripe redelivers.

### 4. Unknown or missing strategy — refuses to guess

```
NULL strategy outcome:    "order's strategy is None -- unknown or predates
                           the strategy column; stock NOT reconciled, left
                           for manual review"
unknown strategy outcome: "order's strategy is 'some_future_strategy' -- ...
                           stock NOT reconciled, left for manual review"
```

Any order written before `orders.strategy` existed, or one with a value
this code doesn't recognize, is left alone with a logged reason rather than
having stock released against a guess. A wrong guess here is worse than no
action — releasing a unit that wasn't actually held by this strategy's
counter would create a real oversell risk, which is exactly the class of
bug the first half of this project exists to prevent.

---

## What's STILL blocked on a real Stripe key

Everything above is genuinely new evidence, gathered this session. What
remains exactly as before: **I do not have a Stripe account or a test
secret key**, and creating one requires a human signing up with a real
email — not something obtainable programmatically. The two things that
specifically require a live network round trip to Stripe are:

1. **Section 5a's concurrency proof** — ten simultaneous real
   `PaymentIntent.create` calls actually racing on Stripe's servers, plus
   the independent `PaymentIntent.search()` confirmation that exactly one
   exists. The retry-on-`IdempotencyError` logic was verified reachable
   (confirmed the real exception classes exist in `stripe==15.4.0` by
   reading the installed SDK's source, not from memory) but never actually
   triggered by a genuine collision, since that requires Stripe's real
   servers to genuinely be processing two requests with the same key at
   once.
2. **Section 5b's webhook-driven transition, end-to-end** — a real decline
   from Stripe (via `pm_card_visa_chargeDeclined` or the documented fallback
   `pm_card_chargeDeclined`), with the webhook delivered by Stripe itself
   rather than replayed from a payload this session constructed by hand.

`scripts/payment_verify.py --scenario all` runs both the moment a key
exists:

```bash
export STRIPE_SECRET_KEY=sk_test_...
python -m app.main
# in another terminal, for the webhook:
stripe listen --forward-to localhost:8010/payment/webhook
export STRIPE_WEBHOOK_SECRET=whsec_...
python -m scripts.payment_verify --scenario all
```

The pipeline reaching real Stripe was re-confirmed this session with the
new attempt-counter key format (a deliberately wrong key produces a real
`502 "Invalid API Key provided: sk_test_****...0000"` straight from
Stripe's own servers), so the only missing piece really is the credential.

---

## Files touched (this session's full pass, all fixes)

**New:**

| file | purpose |
|---|---|
| `app/migrate.py` | idempotent `orders.strategy` column backfill |
| `results/payment_notes.md` | this file |

**Rewritten:**

| file | change |
|---|---|
| `app/payments.py` | Postgres-durable `payments` table replaces the Redis JSON record; attempt-counter idempotency key; webhook-driven, strategy-aware, redelivery-safe stock reconciliation |

**Modified:**

| file | change |
|---|---|
| `app/models.py` | new `Payment` model; `Order.strategy` column (nullable, additive) |
| `app/main.py` | `migrate.run(engine)` added to startup, right after `create_all()`; the three checkout INSERTs now also write `strategy` — **no other line in any checkout handler changed**; imports `app.payments.install` |
| `monitoring/nginx.conf` | new `/nginx-health` location, decoupled from the upstream proxy |
| `docker-compose.yml` | nginx healthcheck now targets `127.0.0.1` (root-caused IPv6/IPv4 mismatch, see `results/horizontal_scaling_notes.md` history) and `/nginx-health`; `STRIPE_SECRET_KEY`/`STRIPE_WEBHOOK_SECRET` passed through to `api1`/`api2`/`api3` |
| `app/queue.py`, `app/metrics.py` | admission-worker leader election (Redis lease) so N replicas admit at the configured rate, not N times it — see commit `8f4820f` for the full writeup and measured before/after |
| `scripts/payment_verify.py` | field names (`attempt`/`tries` replacing the old `attempts`) updated to match the new response shape; storage-layer references corrected from "Redis" to "the `payments` table" — the test logic itself is unchanged, since it only ever talked to the HTTP API |
| `requirements.txt` | `stripe==15.4.0` (unchanged from the first version) |

### Confirmations

- **`app/database.py` — zero diff, still.** No connection-handling logic
  touched by any fix in this pass.
- **No checkout handler LOGIC changed.** The lock, the version check, the
  Redis DECR/INCR, the transaction boundaries — byte-identical. The only
  change inside any of the three `/checkout/*` functions is one additional
  column + literal value in an INSERT statement already being executed.
- **`app/idempotency.py`, `app/admission.py` — zero diff.** Untouched by
  this pass.
- **`postgres` and `redis` service definitions in `docker-compose.yml` —
  zero diff.** Every change this pass made to that file is either a new
  service (none added this time) or an addition to `api1/2/3`'s existing
  environment block and the nginx healthcheck string.

---

## Reproducing the reconciliation evidence above

```bash
docker compose up -d postgres redis
python -m app.main     # migrate.run() adds orders.strategy on this boot

# drive a real order through the real queue + checkout, note its order_id
# and strategy from the response, then simulate the decline webhook Stripe
# would send (no real key needed for this part):
curl -X POST http://localhost:8010/payment/webhook \
  -H "Content-Type: application/json" \
  -d '{"id":"evt_x","type":"payment_intent.payment_failed",
       "data":{"object":{"id":"pi_x","amount":22900,"currency":"usd",
       "metadata":{"flashsale_order_id":"<the order id>"},
       "last_payment_error":{"decline_code":"generic_decline","message":"declined"}}}}'
# (requires a matching payments row already present with that
# stripe_payment_intent_id -- in real use that row is written by
# /payment/create's synchronous path moments before Stripe's webhook
# would ever fire)
```
