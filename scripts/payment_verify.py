"""
Verification for app/payments.py -- the two scenarios the phase exists to
prove, run as a black box over HTTP plus one independent check against
Stripe's own API (not this app's own bookkeeping).

    python -m scripts.payment_verify --scenario concurrency
    python -m scripts.payment_verify --scenario decline
    python -m scripts.payment_verify --scenario all

Requires the API running (python -m app.main) with STRIPE_SECRET_KEY set to
a real test-mode key (sk_test_...) -- this script does not mock or fake
anything Stripe-shaped; every claim it prints comes from either this app's
HTTP responses or a live call to Stripe's own Search API.

Storage-agnostic by construction: this script only ever talks to the HTTP
API, never to Postgres or Redis directly, so it did not need to change when
app/payments.py's own state storage moved from Redis (TTL'd, the original
design) to Postgres's new `payments` table (durable -- see that module's
docstring for why). What it verifies -- the HTTP contract -- is unchanged.

SCENARIO A -- CONCURRENCY (the thing this phase exists to prove)
    Fires N simultaneous POST /payment/create/{order_id} for ONE freshly
    confirmed order (no pre-existing payment record, so nothing short-
    circuits locally -- see app/payments.py's note on why that short-circuit
    is safe for a settled order but must not fire here).

    Two independent checks that exactly one PaymentIntent exists:
      1. Every successful response's stripe_payment_intent_id, compared.
      2. stripe.PaymentIntent.search() queried directly against Stripe's
         API by this order's metadata -- NOT read from this app's own
         `payments` table, so a bug in app/payments.py's own bookkeeping
         could not hide a real duplicate from this check.

SCENARIO B -- DECLINE
    One /payment/create call with a decline test PaymentMethod token.
    Captures GET /payment/status/{order_id} immediately after the
    synchronous response (Stripe's raw status, written by the create/
    confirm code path) and again after waiting for the webhook to arrive
    (status flips to "failed" with decline metadata, written ONLY by the
    webhook handler). Requires `stripe listen --forward-to
    <host>:<port>/payment/webhook` running in another terminal, or a
    registered webhook endpoint reaching this host -- Stripe's cloud
    cannot reach a bare localhost otherwise. If the webhook never arrives
    within the timeout, this is reported plainly, not glossed over.
"""
import argparse
import asyncio
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from sqlalchemy import text

from app.database import SessionLocal

BASE_URL = os.getenv("CHAOS_BASE_URL", "http://127.0.0.1:8010")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "results")

SUCCESS_PM = "pm_card_visa"
# Stripe's documented generic decline token. Tried FIRST because it is the
# one guaranteed to exist across API versions; the brand-prefixed variant
# named in the task is tried as a preference where available, with this as
# the automatic, reported fallback if Stripe rejects it as unrecognized.
DECLINE_PM_PREFERRED = "pm_card_visa_chargeDeclined"
DECLINE_PM_FALLBACK = "pm_card_chargeDeclined"


# --------------------------------------------------------------------------
# Getting a fresh, confirmed order with NO pre-existing payment record
# --------------------------------------------------------------------------
async def fetch_user_id(client):
    # Read-only, mirrors chaos_verification.py's fetch_user_ids().
    db = SessionLocal()
    try:
        row = db.execute(text("SELECT id FROM users LIMIT 1")).fetchone()
        return str(row[0]) if row else None
    finally:
        db.close()


async def make_confirmed_order(client, user_id):
    """
    Drives one real purchase through the actual front door -- queue, then
    checkout -- so the resulting order is exactly as genuine as any other
    buyer's. No shortcuts into the database.
    """
    sales = (await client.get("/sales")).json().get("sales", [])
    if not sales:
        raise SystemExit("No active sales. Run: python -m scripts.seed_storefront")
    sale_id = sales[0]["sale_id"]

    await client.post(f"/admin/reset-sale/{sale_id}", params={"stock": 50})

    j = await client.post(f"/queue/join/{sale_id}")
    j.raise_for_status()
    token = j.json()["queue_token"]

    admission = None
    deadline = time.time() + 30
    while time.time() < deadline:
        s = (await client.get(f"/queue/status/{sale_id}", params={"token": token})).json()
        if s.get("admitted"):
            admission = s["admission_token"]
            break
        await asyncio.sleep(0.3)
    if not admission:
        raise SystemExit("Never admitted from the queue within 30s.")

    r = await client.post(
        f"/checkout/pessimistic",
        params={"sale_id": sale_id, "user_id": user_id},
        headers={"X-Admission-Token": admission},
    )
    body = r.json()
    if body.get("status") != "confirmed":
        raise SystemExit(f"Checkout did not confirm: {body}")
    return body["order_id"]


# --------------------------------------------------------------------------
# Scenario A -- concurrency
# --------------------------------------------------------------------------
async def one_create_call(client, order_id, payment_method, barrier, results):
    await barrier.wait()
    started = time.perf_counter()
    try:
        r = await client.post(f"/payment/create/{order_id}",
                              params={"payment_method": payment_method})
        elapsed = (time.perf_counter() - started) * 1000
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        results.append({
            "http_status": r.status_code, "elapsed_ms": round(elapsed, 1),
            "status": body.get("status"),
            "stripe_payment_intent_id": body.get("stripe_payment_intent_id"),
            # "attempt" = which distinct payment attempt this is for the
            # order (app/models.py's Payment.attempt); "tries" = how many
            # times THIS call retried Stripe's IdempotencyError internally
            # (app/payments.py's _create_and_confirm). All ten calls in this
            # scenario should report the SAME attempt number.
            "attempt": body.get("attempt"), "tries": body.get("tries"),
            "source": body.get("source"),
        })
    except Exception as exc:                                   # noqa: BLE001
        results.append({"http_status": None, "error": f"{type(exc).__name__}: {exc}"})


def search_stripe_for_order(order_id, tries=6, delay_s=2.0):
    """
    The independent check: ask STRIPE, not this app, how many PaymentIntents
    carry this order's metadata.

    Stripe's Search API indexes asynchronously and documents a short delay
    before newly created objects become searchable -- this is a real
    limitation of the VERIFICATION METHOD, not of the payment system, and is
    why this polls rather than checking once. Reported either way.
    """
    import stripe
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

    query = f"metadata['flashsale_order_id']:'{order_id}'"
    last = None
    for attempt in range(1, tries + 1):
        try:
            result = stripe.PaymentIntent.search(query=query, limit=10)
            last = result
            if result.data:
                return result, attempt
        except Exception as exc:                               # noqa: BLE001
            last = exc
        time.sleep(delay_s)
    return last, tries


async def scenario_concurrency(n=10, payment_method=SUCCESS_PM):
    print(f"\n=== Scenario A: {n} simultaneous /payment/create for ONE order ===")
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0) as client:
        user_id = await fetch_user_id(client)
        if not user_id:
            raise SystemExit("No users in the database. Run: python -m scripts.seed")

        order_id = await make_confirmed_order(client, user_id)
        print(f"  fresh confirmed order: {order_id}  (no payment record exists yet)")

        barrier = asyncio.Event()
        results = []
        tasks = [asyncio.create_task(one_create_call(client, order_id, payment_method,
                                                      barrier, results))
                for _ in range(n)]
        t0 = time.perf_counter()
        barrier.set()
        await asyncio.gather(*tasks)
        wall = time.perf_counter() - t0

    intent_ids = {r.get("stripe_payment_intent_id") for r in results
                 if r.get("stripe_payment_intent_id")}
    attempt_numbers = {r.get("attempt") for r in results if r.get("attempt") is not None}
    errors = [r for r in results if r.get("http_status") not in (200,)]

    print(f"  wall={wall:.2f}s  responses={len(results)}  "
         f"distinct PaymentIntent ids seen in OUR responses={len(intent_ids)}  "
         f"distinct attempt numbers computed independently by each call={attempt_numbers}")
    for r in results:
        print(f"    {r}")

    print("\n  Independent check -- querying Stripe's own Search API "
         "(not this app's own `payments` table) ...")
    search_result, attempts_used = search_stripe_for_order(order_id)
    if isinstance(search_result, Exception):
        stripe_count = None
        print(f"  Stripe search FAILED after {attempts_used} attempts: {search_result}")
    else:
        stripe_count = len(search_result.data) if search_result else 0
        found_ids = [pi["id"] for pi in (search_result.data if search_result else [])]
        print(f"  Stripe reports {stripe_count} PaymentIntent(s) for this order "
             f"(found on search attempt {attempts_used}/6): {found_ids}")

    passed = (len(intent_ids) <= 1) and (stripe_count == 1)
    print(f"\n  VERDICT: {'PASS' if passed else 'FAIL'} -- "
         f"{'exactly one PaymentIntent existed' if passed else 'see counts above'}")

    return {
        "order_id": order_id, "n": n, "wall_s": round(wall, 2),
        "responses": results,
        "distinct_intent_ids_in_responses": list(intent_ids),
        "stripe_search_count": stripe_count,
        "stripe_search_attempts": attempts_used,
        "passed": passed,
    }


# --------------------------------------------------------------------------
# Scenario B -- decline
# --------------------------------------------------------------------------
async def scenario_decline(payment_method=DECLINE_PM_PREFERRED, webhook_wait_s=25.0):
    print(f"\n=== Scenario B: decline handling, webhook-driven transition ===")
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0) as client:
        user_id = await fetch_user_id(client)
        order_id = await make_confirmed_order(client, user_id)
        print(f"  fresh confirmed order: {order_id}")

        pm = payment_method
        r = await client.post(f"/payment/create/{order_id}", params={"payment_method": pm})
        body = r.json()
        if r.status_code not in (200, 402) and "unrecognized" in json.dumps(body).lower():
            print(f"  {pm} rejected as unrecognized by Stripe; "
                 f"falling back to {DECLINE_PM_FALLBACK}")
            pm = DECLINE_PM_FALLBACK
            r = await client.post(f"/payment/create/{order_id}", params={"payment_method": pm})
            body = r.json()

        print(f"  create/confirm response (http {r.status_code}, payment_method={pm}): {body}")

        before = (await client.get(f"/payment/status/{order_id}")).json()
        print(f"  status immediately after sync response: {before}")

        print(f"  waiting up to {webhook_wait_s:.0f}s for "
             f"payment_intent.payment_failed via the webhook "
             f"(needs `stripe listen --forward-to <host>:8010/payment/webhook` "
             f"running, or a registered endpoint reaching this host)...")
        after = before
        deadline = time.time() + webhook_wait_s
        got_webhook = False
        while time.time() < deadline:
            after = (await client.get(f"/payment/status/{order_id}")).json()
            if (after.get("status") == "failed"
                    and str(after.get("last_event", "")).startswith("webhook:")):
                got_webhook = True
                break
            await asyncio.sleep(1.0)

        print(f"  status after wait: {after}")
        print(f"\n  VERDICT: {'PASS' if got_webhook else 'NO WEBHOOK RECEIVED'} -- "
             f"{'transition to failed was webhook-driven' if got_webhook else 'is `stripe listen` forwarding to this host?'}")

    return {
        "order_id": order_id, "payment_method_used": pm,
        "sync_response": body, "status_before_webhook": before,
        "status_after_wait": after, "webhook_received": got_webhook,
    }


# --------------------------------------------------------------------------
async def main_async(args):
    if not os.getenv("STRIPE_SECRET_KEY"):
        print("STRIPE_SECRET_KEY is not set in THIS shell -- the app may still "
             "refuse with 503, and this script's own Stripe Search check will "
             "fail outright. See app/payments.py:STRIPE_KEY_HELP.")

    out = {}
    if args.scenario in ("concurrency", "all"):
        out["concurrency"] = await scenario_concurrency(n=args.n)
    if args.scenario in ("decline", "all"):
        out["decline"] = await scenario_decline(webhook_wait_s=args.webhook_wait)

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "payment_verify_results.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nwrote {path}")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--scenario", choices=["concurrency", "decline", "all"], default="all")
    p.add_argument("--n", type=int, default=10, help="simultaneous calls for scenario A")
    p.add_argument("--webhook-wait", type=float, default=25.0,
                   help="seconds to wait for the decline webhook in scenario B")
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
