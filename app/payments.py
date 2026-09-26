"""
Stripe test-mode payment step, layered on top of an already-confirmed order.

Fully separate from checkout: a buyer already has a confirmed `orders` row
(from /checkout/pessimistic|optimistic|redis) before ever touching this
module. Checkout handlers themselves are untouched -- the only change made
to them anywhere in this project was adding one column (`strategy`) to their
existing INSERT statements, so this module knows which counter to release on
a decline (see the reconciliation section below).

STATE LIVES IN POSTGRES -- `payments` table (see app/models.py's Payment
docstring for the full reasoning). This module's FIRST version stored
payment state in Redis only (`payment:{order_id}`, TTL'd), scoped that way
deliberately for that phase. It was replaced because it left a real gap: no
durable record anywhere that money had changed hands. If Redis evicted the
key or the TTL simply elapsed, that fact became unrecoverable, while every
other fact this system treats as real -- orders, stock, the audit log --
lives in Postgres for exactly this reason. Payment status is at least as
important a fact as any of them.

IDEMPOTENCY COMPOSITION -- THE THING THIS MODULE EXISTS TO PROVE
    Every /payment/create call for a given (order, attempt) sends Stripe the
    SAME idempotency key:

        flashsale-order-{order_id}-attempt-{attempt}-payment-create

    Ten simultaneous calls for the same order all compute the same next
    `attempt` number (see `_next_attempt()`) with no coordination between
    them, so they all share one key and send Stripe ten IDENTICAL requests.
    Stripe's own servers -- not this process, and not a Redis or Postgres
    lock placed in front of them -- are what collapse that into one
    PaymentIntent: whoever arrives first executes; everyone else either
    learns a request with that key is already in flight (IdempotencyError,
    retried below) or, once the first has finished, receives back the exact
    same cached response. Deliberately NOT gated by a reservation the way
    app/idempotency.py gates checkout retries -- adding one here would only
    prove that OUR lock works, which the idempotency phase already did.

    THE ATTEMPT COUNTER fixes a limitation the Redis-only version explicitly
    named and left unfixed: with a key that was a pure function of order_id
    and nothing else, retrying after a genuine decline just replayed
    Stripe's cached decline for 24h, since Stripe reuses whatever response
    it already has for that key. `_next_attempt()` returns the SAME number
    for calls that should dedupe (nothing yet, or the latest attempt is
    still succeeded/in-flight) and the NEXT number once the latest attempt
    is a definitive `failed` -- so a genuine retry reaches the card network
    again, while true concurrent duplicates of one attempt still collapse
    through Stripe's layer exactly as before.

STOCK RECONCILIATION ON DECLINE
    A declined payment releases the unit it was holding back to whichever
    counter actually reserved it -- `inventory.reserved_stock` for the
    pessimistic/optimistic strategies, the Redis `stock:{sale_id}` counter
    for the redis strategy, which never touches `reserved_stock` at all (see
    the CAP-tradeoff note on /checkout/redis in app/main.py). Which one to
    release is read from `orders.strategy`, recorded by the checkout
    handlers at INSERT time for exactly this purpose.

    Applied from the WEBHOOK handler only, guarded by a conditional UPDATE
    (`... WHERE status != 'failed' RETURNING id`) so a redelivered webhook
    for the same failure -- Stripe does not guarantee exactly-once delivery
    -- cannot release the same unit twice. An order whose `strategy` is NULL
    (any order written before this column existed) is left alone with a
    logged warning rather than guessed at.
"""
import json
import os
import time
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db

router = APIRouter(prefix="/payment", tags=["payment"])

_redis = None                    # used ONLY to release the redis-strategy
                                  # stock counter on decline -- see below

IDEMPOTENCY_MAX_RETRIES = 8
IDEMPOTENCY_RETRY_BACKOFF_S = 0.35
DEFAULT_CURRENCY = "usd"
DEFAULT_PAYMENT_METHOD = "pm_card_visa"

STRIPE_KEY_HELP = (
    "STRIPE_SECRET_KEY is not set, so this endpoint cannot reach Stripe. "
    "Get a free test-mode secret key (no business verification needed): "
    "1) create an account at https://dashboard.stripe.com/register  "
    "2) stay in TEST MODE (toggle, top-right of the Dashboard)  "
    "3) Developers -> API keys -> copy the Secret key (starts with sk_test_)  "
    "4) set it before starting the app -- "
    "PowerShell: $env:STRIPE_SECRET_KEY='sk_test_...'  ;  "
    "bash: export STRIPE_SECRET_KEY=sk_test_...  "
    "Then restart: python -m app.main"
)


def _idempotency_key(order_id, attempt) -> str:
    return f"flashsale-order-{order_id}-attempt-{attempt}-payment-create"


def _require_stripe():
    """
    Imports and configures the Stripe SDK per call rather than at module
    import time.

    Configuring at import time would mean a missing key takes down the
    WHOLE app -- app/main.py imports this module unconditionally alongside
    every other phase's routes, and a student without a Stripe account
    would lose the storefront, the dashboard and every earlier demo along
    with it, over a feature they may not even be testing. Failing loudly
    only when a payment endpoint is actually called keeps the blast radius
    to this module.
    """
    import stripe

    key = os.getenv("STRIPE_SECRET_KEY")
    if not key:
        raise HTTPException(status_code=503, detail=STRIPE_KEY_HELP)
    stripe.api_key = key
    return stripe


def payment_result(status: str, order_id, http_status: int = 200, **fields):
    body = {"status": status, "order_id": str(order_id), **fields}
    if http_status == 200:
        return body
    return JSONResponse(status_code=http_status, content=body)


# --------------------------------------------------------------------------
# Pricing -- read-only against orders/order_items/flash_sale_events/products
# --------------------------------------------------------------------------
def _fetch_order(db: Session, order_id):
    """
    order_id, status, and the amount to charge -- one SELECT, no writes.

    WHY THE FALLBACK: the three /checkout/* handlers in app/main.py insert
    exactly one `orders` row per purchase and never populate `order_items`
    (that table is populated only by scripts/seed_large.py's bulk synthetic
    dataset, for the indexing demos). So a live-checkout order has no
    OrderItem rows to sum. The COALESCE falls back to the sale's price for
    one unit, which is what checkout actually sold.
    """
    return db.execute(
        text("""
            SELECT
                o.id, o.status,
                COALESCE(
                    (SELECT SUM(oi.quantity * oi.price_at_purchase)
                     FROM order_items oi WHERE oi.order_id = o.id),
                    f.sale_price
                ) AS amount,
                p.name AS product_name
            FROM orders o
            JOIN flash_sale_events f ON f.id = o.sale_id
            JOIN products p ON p.id = f.product_id
            WHERE o.id = :oid
        """),
        {"oid": order_id},
    ).fetchone()


def _to_cents(amount) -> int:
    try:
        return int((Decimal(str(amount)) * 100).to_integral_value())
    except (InvalidOperation, TypeError):
        return -1


# --------------------------------------------------------------------------
# Attempt numbering -- what makes the idempotency-key fix work
# --------------------------------------------------------------------------
def _next_attempt(db: Session, order_id):
    """
    Which attempt number a /payment/create call for this order should use
    right now, and the latest row if one exists.

    Ten simultaneous calls with NO existing row all compute attempt=1 with
    no coordination -- they don't need to agree by talking to each other,
    only by applying the same deterministic rule to the same (empty) state,
    which is exactly why this needs no lock. A call arriving after the
    latest attempt definitively FAILED computes attempt+1 -- a fresh key,
    reaching Stripe again. Anything else (succeeded, or still processing)
    reuses the latest attempt's number, so a duplicate of an in-flight or
    already-resolved attempt still shares that attempt's key.
    """
    row = db.execute(
        text("""
            SELECT attempt, status FROM payments
            WHERE order_id = :oid ORDER BY attempt DESC LIMIT 1
        """),
        {"oid": order_id},
    ).fetchone()
    if row is None:
        return 1, None
    attempt, status = row
    return (attempt + 1 if status == "failed" else attempt), row


def _load_latest(db: Session, order_id):
    row = db.execute(
        text("""
            SELECT id, order_id, attempt, stripe_payment_intent_id, status,
                   amount_cents, currency, payment_method, decline_code,
                   failure_message, last_event, created_at, updated_at
            FROM payments WHERE order_id = :oid
            ORDER BY attempt DESC LIMIT 1
        """),
        {"oid": order_id},
    ).fetchone()
    if row is None:
        return None
    cols = ["id", "order_id", "attempt", "stripe_payment_intent_id", "status",
            "amount_cents", "currency", "payment_method", "decline_code",
            "failure_message", "last_event", "created_at", "updated_at"]
    return dict(zip(cols, row))


def _upsert_payment(db: Session, order_id, attempt, **fields):
    """
    INSERT ... ON CONFLICT (order_id, attempt) DO UPDATE.

    Safe under concurrent callers for the same reason the old Redis
    read-modify-write was safe: every writer -- this synchronous path and
    the webhook handler alike -- is reporting the SAME underlying Stripe
    truth for the SAME PaymentIntent, never independent state that could
    disagree. Two racing UPSERTs converge on the same final row.
    """
    cols = ["stripe_payment_intent_id", "status", "amount_cents", "currency",
            "payment_method", "last_event"]
    values = {c: fields.get(c) for c in cols}
    row = db.execute(
        text(f"""
            INSERT INTO payments
                (id, order_id, attempt, {", ".join(cols)}, created_at, updated_at)
            VALUES
                (gen_random_uuid(), :order_id, :attempt,
                 {", ".join(":" + c for c in cols)}, now(), now())
            ON CONFLICT (order_id, attempt) DO UPDATE SET
                {", ".join(f"{c} = EXCLUDED.{c}" for c in cols)},
                updated_at = now()
            RETURNING id
        """),
        {"order_id": order_id, "attempt": attempt, **values},
    ).fetchone()
    db.commit()
    return row[0]


# --------------------------------------------------------------------------
# Stock reconciliation -- webhook-driven, strategy-aware
# --------------------------------------------------------------------------
def _release_reserved_unit(db: Session, sale_id, strategy):
    """
    Releases the one unit a declined payment's order was holding, back to
    whichever counter the order's checkout strategy actually reserved it
    from. Returns a short description of what happened, for logging --
    never raises on a data inconsistency, since a reconciliation bug must
    not turn into a 500 on Stripe's webhook (that would make Stripe retry
    forever and can get an endpoint disabled).
    """
    if strategy in ("pessimistic", "optimistic"):
        inv = db.execute(
            text("SELECT id, reserved_stock FROM inventory WHERE sale_id = :sid"),
            {"sid": sale_id},
        ).fetchone()
        if not inv:
            return f"no inventory row for sale {sale_id}; skipped"
        inv_id, before = inv
        if before <= 0:
            # Would go negative -- a real inconsistency somewhere else, not
            # something to paper over by releasing anyway.
            return (f"inventory {inv_id} reserved_stock already {before}; "
                    f"release skipped rather than going negative")
        db.execute(
            text("UPDATE inventory SET reserved_stock = reserved_stock - 1 WHERE id = :id"),
            {"id": inv_id},
        )
        db.execute(
            text("""
                INSERT INTO stock_audit_log
                    (id, inventory_id, operation, before_value, after_value, timestamp, committed)
                VALUES (gen_random_uuid(), :inv_id, 'payment_declined_release', :before, :after, now(), 'true')
            """),
            {"inv_id": inv_id, "before": before, "after": before - 1},
        )
        return f"inventory {inv_id} reserved_stock {before} -> {before - 1}"

    if strategy == "redis":
        if _redis is None:
            return "redis client unavailable; release skipped"
        key = f"stock:{sale_id}"
        after = _redis.incr(key)
        before = after - 1
        inv = db.execute(
            text("SELECT id FROM inventory WHERE sale_id = :sid"), {"sid": sale_id},
        ).fetchone()
        if inv:
            db.execute(
                text("""
                    INSERT INTO stock_audit_log
                        (id, inventory_id, operation, before_value, after_value, timestamp, committed)
                    VALUES (gen_random_uuid(), :inv_id, 'payment_declined_release_redis_counter',
                            :before, :after, now(), 'true')
                """),
                {"inv_id": inv[0], "before": before, "after": after},
            )
        return f"redis counter {key} {before} -> {after}"

    return (f"order's strategy is {strategy!r} -- unknown or predates the "
            f"strategy column; stock NOT reconciled, left for manual review")


# --------------------------------------------------------------------------
# Stripe create + confirm, with the idempotency-conflict retry
# --------------------------------------------------------------------------
def _create_and_confirm(stripe, order_id, attempt, amount_cents, currency, payment_method):
    """
    Calls PaymentIntent.create(confirm=True, ...) with a key fixed to
    (order_id, attempt). Returns (intent_dict, tries, decline_exc_or_None).

    Retries ONLY on IdempotencyError -- "a request with this key is
    currently being processed elsewhere." That is not a transient network
    blip to paper over; it IS the concurrency scenario this module exists to
    demonstrate. The identical request is resent unchanged, so either it
    collides again (another retry) or the first request has by then
    finished and Stripe hands back its now-completed, identical response.

    A CardError (decline) is NOT retried here -- it is a real, definitive
    outcome for THIS attempt. A genuine retry is a NEW attempt, handled by
    `_next_attempt()` on the caller's next call, not by looping here.
    """
    idem_key = _idempotency_key(order_id, attempt)
    for tries in range(1, IDEMPOTENCY_MAX_RETRIES + 1):
        try:
            intent = stripe.PaymentIntent.create(
                amount=amount_cents,
                currency=currency,
                payment_method=payment_method,
                confirm=True,
                metadata={"flashsale_order_id": str(order_id)},
                idempotency_key=idem_key,
            )
            return intent, tries, None
        except stripe.CardError as exc:
            intent = exc.error.payment_intent if exc.error else None
            return intent, tries, exc
        except stripe.IdempotencyError:
            if tries == IDEMPOTENCY_MAX_RETRIES:
                raise
            time.sleep(IDEMPOTENCY_RETRY_BACKOFF_S * tries)
    raise HTTPException(502, "Stripe idempotency conflict did not resolve in time.")


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@router.post("/create/{order_id}")
def create_payment(
    order_id: str,
    payment_method: str = Query(
        DEFAULT_PAYMENT_METHOD,
        description="A Stripe test PaymentMethod token, e.g. pm_card_visa "
                    "(succeeds) or a decline token (fails). Confirmed "
                    "server-side -- no frontend card form involved.",
    ),
    db: Session = Depends(get_db),
):
    try:
        oid = uuid.UUID(order_id)
    except ValueError:
        raise HTTPException(400, "order_id is not a valid UUID.")

    stripe = _require_stripe()          # 503 with STRIPE_KEY_HELP if unset

    attempt, latest = _next_attempt(db, oid)

    # Already durably succeeded -- serve the row, no Stripe call.
    #
    # This does NOT weaken the concurrency test: it only ever short-circuits
    # a call that arrives AFTER a burst has already settled (a stray
    # double-click two seconds late), never the burst itself. Ten truly
    # simultaneous calls for a fresh order all compute attempt=1 with no
    # existing row, so all ten proceed to Stripe -- what exercises the thing
    # being proved. This exists so a click #11 the next day does not
    # re-attempt a charge that already happened.
    if latest and latest[1] == "succeeded":
        row = _load_latest(db, oid)
        return payment_result("succeeded", oid, source="postgres_cache",
                              attempt=row["attempt"],
                              stripe_payment_intent_id=row["stripe_payment_intent_id"],
                              amount_cents=row["amount_cents"], currency=row["currency"])

    row = _fetch_order(db, oid)
    if not row:
        raise HTTPException(404, "Order not found.")
    _, order_status, amount, product_name = row
    if order_status != "confirmed":
        raise HTTPException(
            409,
            f"Order {oid} is '{order_status}', not 'confirmed' -- only a "
            f"confirmed checkout can be paid.",
        )

    amount_cents = _to_cents(amount)
    if amount_cents <= 0:
        raise HTTPException(500, f"Resolved a non-positive amount ({amount}) for order {oid}.")

    try:
        intent, tries, decline_exc = _create_and_confirm(
            stripe, oid, attempt, amount_cents, DEFAULT_CURRENCY, payment_method)
    except stripe.CardError:
        raise                                # handled inside _create_and_confirm
    except stripe.StripeError as exc:
        raise HTTPException(502, f"Stripe error: {exc.user_message or str(exc)}")

    intent_id = intent.get("id") if intent else None
    raw_status = intent.get("status") if intent else "unknown"

    _upsert_payment(
        db, oid, attempt,
        stripe_payment_intent_id=intent_id,
        status=raw_status,
        amount_cents=amount_cents,
        currency=DEFAULT_CURRENCY,
        payment_method=payment_method,
        last_event="create_confirm_sync",
    )

    if decline_exc is not None:
        # Definitive decline. The row above holds Stripe's immediate raw
        # status; the webhook handler is what later writes the terminal
        # status="failed" plus decline metadata AND releases the reserved
        # unit -- see results/payment_notes.md for that transition
        # captured before/after the webhook fires.
        return payment_result(
            raw_status, oid, http_status=402, source="stripe",
            attempt=attempt, stripe_payment_intent_id=intent_id, tries=tries,
            message=decline_exc.user_message or str(decline_exc),
        )

    return payment_result(
        raw_status, oid, source="stripe",
        attempt=attempt, stripe_payment_intent_id=intent_id, tries=tries,
        amount_cents=amount_cents, currency=DEFAULT_CURRENCY,
    )


@router.get("/status/{order_id}")
def payment_status(order_id: str, db: Session = Depends(get_db)):
    """Latest attempt's row for an order's payment -- what a client polls
    while waiting for the webhook to settle the final status."""
    try:
        oid = uuid.UUID(order_id)
    except ValueError:
        raise HTTPException(400, "order_id is not a valid UUID.")
    row = _load_latest(db, oid)
    if not row:
        raise HTTPException(404, "No payment record for this order.")
    row["order_id"] = str(row["order_id"])
    row["id"] = str(row["id"])
    return row


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Updates the payments row and, on a decline, releases the reserved unit.
    This is the ONLY code path that ever writes the normalized terminal
    states "succeeded" / "failed" -- see the module docstring and
    results/payment_notes.md for why that distinction (versus the raw
    Stripe status written synchronously in create_payment) is the evidence
    for the webhook-driven-transition requirement.

    Test locally with the Stripe CLI:
        stripe listen --forward-to localhost:8010/payment/webhook
    which prints a `whsec_...` value -- set that as STRIPE_WEBHOOK_SECRET so
    the signature is actually verified.

    Must return 2xx for anything it does not recognise or has already
    applied. Stripe retries a webhook endpoint on non-2xx and disables it
    after enough consecutive failures, so an event this handler does not
    care about -- or one whose effect is already recorded -- is a no-op
    200, never an error. The ONE deliberate exception is a genuine
    "come back in a second" case, noted below.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    if webhook_secret:
        import stripe
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        except stripe.SignatureVerificationError:
            raise HTTPException(400, "Invalid Stripe webhook signature.")
        except ValueError:
            raise HTTPException(400, "Invalid webhook payload.")
    else:
        # No STRIPE_WEBHOOK_SECRET configured -- accept unverified.
        # Deliberate and loud, not silent: logged on every call, and it is
        # the one place in this module that is NOT how a production
        # deployment should run. It exists so the webhook path is testable
        # before `stripe listen` has been wired up, mirroring how
        # ADMIN_TOKEN in app/main.py is optional-if-unset for the same
        # "local demo, not production" reason.
        print("WARNING: /payment/webhook received an event with "
              "STRIPE_WEBHOOK_SECRET unset -- processing UNVERIFIED. "
              "Set STRIPE_WEBHOOK_SECRET (from `stripe listen`) before "
              "trusting this endpoint.")
        try:
            event = json.loads(payload)
        except Exception:                                       # noqa: BLE001
            raise HTTPException(400, "Invalid JSON body.")

    etype = event["type"]
    obj = event["data"]["object"]
    order_id = (obj.get("metadata") or {}).get("flashsale_order_id")
    intent_id = obj.get("id")

    if not order_id or etype not in ("payment_intent.succeeded", "payment_intent.payment_failed"):
        # Not one of ours, or not a type we act on. Acknowledge and ignore.
        return {"received": True, "handled": False, "type": etype}

    payment_row = db.execute(
        text("SELECT id, order_id FROM payments WHERE stripe_payment_intent_id = :iid"),
        {"iid": intent_id},
    ).fetchone()

    if not payment_row:
        # The synchronous create/confirm path writes its row immediately
        # after Stripe's create+confirm call returns, which happens before
        # Stripe would have any grounds to fire a webhook about that same
        # PaymentIntent -- so this should be unreachable in practice. If it
        # ever IS reached (a genuine race), a non-2xx tells Stripe to retry
        # with backoff rather than silently dropping the event's effect.
        raise HTTPException(409, "No matching payment row yet -- retry shortly.")

    new_status = "succeeded" if etype == "payment_intent.succeeded" else "failed"
    last_error = obj.get("last_payment_error") or {}

    # Atomic, and the ONLY guard needed against a redelivered webhook: this
    # UPDATE returns a row ONLY on the transition INTO new_status, never on
    # a webhook that arrives after that transition already happened.
    transitioned = db.execute(
        text("""
            UPDATE payments SET
                status = :status,
                decline_code = :decline_code,
                failure_message = :failure_message,
                last_event = :last_event,
                last_webhook_event_id = :event_id,
                updated_at = now()
            WHERE id = :pid AND status != :status
            RETURNING id
        """),
        {
            "pid": payment_row[0], "status": new_status,
            "decline_code": last_error.get("decline_code"),
            "failure_message": last_error.get("message"),
            "last_event": f"webhook:{etype}", "event_id": event.get("id"),
        },
    ).fetchone()
    db.commit()

    if not transitioned:
        # Already applied by an earlier delivery of this same event -- a
        # no-op, not an error.
        return {"received": True, "handled": False, "type": etype,
               "order_id": order_id, "note": "already applied"}

    if new_status == "failed":
        order_row = db.execute(
            text("SELECT sale_id, strategy FROM orders WHERE id = :oid"),
            {"oid": payment_row[1]},
        ).fetchone()
        if order_row:
            sale_id, strategy = order_row
            outcome = _release_reserved_unit(db, sale_id, strategy)
            db.execute(
                text("UPDATE payments SET stock_released_at = now() WHERE id = :pid"),
                {"pid": payment_row[0]},
            )
            db.commit()
            print(f"payment {payment_row[0]} (order {order_id}) declined -- {outcome}")

    return {"received": True, "handled": True, "type": etype, "order_id": order_id}


def install(app, redis_client):
    """
    One call from app/main.py. `redis_client` is used for exactly one
    thing now -- releasing the redis-strategy stock counter on a decline
    (see `_release_reserved_unit`) -- payment STATE itself lives entirely
    in Postgres. Same install() shape as app/idempotency.py's.
    """
    global _redis
    _redis = redis_client
    app.include_router(router)
    return app
