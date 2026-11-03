"""
/payment/* against real Postgres + Redis, with Stripe replaced by an
in-process fake that keeps Stripe's idempotency-key contract: the first
request for a key executes, a concurrent duplicate gets IdempotencyError while
it is in flight, and a later duplicate gets the cached result.

    * double-charge   -- simultaneous /payment/create calls charge once
    * stock release   -- a decline gives the unit back exactly once, even when
                         the webhook is redelivered or the order is declined
                         twice
    * paid retries    -- a retry after a released decline holds a unit again,
                         or is refused when none is left
    * webhook trust   -- unsigned events are refused unless opted in

Needs Postgres + Redis; skipped otherwise.
"""
import asyncio
import json
import threading
import time
import types
import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from tests.conftest import sale_state

DECLINE = "pm_card_chargeDeclined"


class FakeStripe:
    class StripeError(Exception):
        user_message = None

    class CardError(StripeError):
        def __init__(self, intent):
            super().__init__("Your card was declined.")
            self.error = types.SimpleNamespace(payment_intent=intent)
            self.user_message = "Your card was declined."

    class IdempotencyError(StripeError):
        pass

    SignatureVerificationError = Exception

    def __init__(self, latency=0.2):
        self._lock = threading.Lock()
        self._by_key = {}           # key -> ("inflight", None) | ("done", (intent, declined))
        self.latency = latency
        self.calls = 0
        self.intents = []           # every PaymentIntent actually created
        self.PaymentIntent = self

    @property
    def charges(self):
        return [i for i in self.intents if i["status"] == "succeeded"]

    def create(self, amount, currency, payment_method, confirm, metadata, idempotency_key):
        with self._lock:
            self.calls += 1
            state = self._by_key.get(idempotency_key)
            if state and state[0] == "inflight":
                raise self.IdempotencyError("in flight")
            if state is None:
                self._by_key[idempotency_key] = ("inflight", None)
        if state is None:
            time.sleep(self.latency)
            declined = payment_method == DECLINE
            intent = {"id": f"pi_{uuid.uuid4().hex[:16]}", "amount": amount,
                      "status": "requires_payment_method" if declined else "succeeded",
                      "metadata": metadata}
            with self._lock:
                self.intents.append(intent)
                self._by_key[idempotency_key] = ("done", (intent, declined))
        intent, declined = self._by_key[idempotency_key][1]
        if declined:
            raise self.CardError(intent)
        return intent


@pytest.fixture
def stripe(monkeypatch):
    from app import payments
    fake = FakeStripe()
    monkeypatch.setattr(payments, "_require_stripe", lambda: fake)
    monkeypatch.setattr(payments, "IDEMPOTENCY_RETRY_BACKOFF_S", 0.05)
    monkeypatch.setattr(payments, "ALLOW_UNVERIFIED_WEBHOOKS", True)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    return fake


@pytest.fixture
def pay_app(redis_client):
    from app import payments
    app = FastAPI()
    payments.install(app, redis_client)
    return app


def place_order(engine, redis_client, sale_id, user_id, strategy="pessimistic"):
    """What a confirmed checkout leaves behind: an order row plus one held unit."""
    with engine.begin() as c:
        oid = c.execute(text(
            "INSERT INTO orders (id,user_id,sale_id,status,strategy,created_at) "
            "VALUES (gen_random_uuid(),:u,:s,'confirmed',:st,now()) RETURNING id"),
            {"u": user_id, "s": sale_id, "st": strategy}).scalar()
        if strategy == "redis":
            total = c.execute(text("SELECT total_stock FROM inventory WHERE sale_id=:s"),
                              {"s": sale_id}).scalar()
            redis_client.setnx(f"stock:{sale_id}", total)
            redis_client.decr(f"stock:{sale_id}")
        else:
            c.execute(text("UPDATE inventory SET reserved_stock=reserved_stock+1 WHERE sale_id=:s"),
                      {"s": sale_id})
    return str(oid)


def webhook_event(etype, intent_id, order_id):
    return {"id": f"evt_{uuid.uuid4().hex[:12]}", "type": etype,
            "data": {"object": {"id": intent_id, "metadata": {"flashsale_order_id": order_id},
                                "last_payment_error": {"decline_code": "generic_decline",
                                                       "message": "declined"}}}}


def latest_intent(engine, order_id):
    with engine.connect() as c:
        return c.execute(text("SELECT stripe_payment_intent_id FROM payments WHERE order_id=:o "
                              "ORDER BY attempt DESC LIMIT 1"), {"o": order_id}).scalar()


def payments_rows(engine, order_id):
    with engine.connect() as c:
        return c.execute(text("SELECT attempt, status, stock_released_at IS NOT NULL FROM payments "
                              "WHERE order_id=:o ORDER BY attempt"), {"o": order_id}).fetchall()


def create(c, order_id, pm="pm_card_visa"):
    return c.post(f"/payment/create/{order_id}", params={"payment_method": pm})


def decline_and_settle(call_app, pay_app, engine, order_id):
    """Declined attempt, then the payment_failed webhook for it."""
    r = call_app(pay_app, lambda c: create(c, order_id, DECLINE))
    assert r.status_code == 402, r.text
    ev = webhook_event("payment_intent.payment_failed", latest_intent(engine, order_id), order_id)
    w = call_app(pay_app, lambda c: c.post("/payment/webhook", json=ev))
    assert w.status_code == 200, w.text
    return ev


# ---------------------------------------------------------------------------
# Double-charge
# ---------------------------------------------------------------------------
def test_simultaneous_creates_charge_the_card_once(stripe, pay_app, make_sale, call_app,
                                                   redis_client, schema):
    sid, (user,) = make_sale(stock=3)
    oid = place_order(schema, redis_client, sid, user)
    responses = call_app(pay_app, lambda c: asyncio.gather(*[create(c, oid) for _ in range(10)]))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert len(stripe.charges) == 1
    assert {r.json()["stripe_payment_intent_id"] for r in responses} == {stripe.charges[0]["id"]}
    assert payments_rows(schema, oid) == [(1, "succeeded", False)]
    assert stripe.calls > 10      # the in-flight collisions were really exercised


def test_paying_again_after_success_never_reaches_stripe(stripe, pay_app, make_sale, call_app,
                                                         redis_client, schema):
    sid, (user,) = make_sale(stock=3)
    oid = place_order(schema, redis_client, sid, user)
    call_app(pay_app, lambda c: create(c, oid))
    calls = stripe.calls
    again = call_app(pay_app, lambda c: create(c, oid))
    assert again.json()["source"] == "postgres_cache"
    assert stripe.calls == calls and len(stripe.charges) == 1


def test_unpaid_order_states_are_refused(stripe, pay_app, call_app):
    assert call_app(pay_app, lambda c: create(c, "not-a-uuid")).status_code == 400
    assert call_app(pay_app, lambda c: create(c, str(uuid.uuid4()))).status_code == 404
    assert stripe.calls == 0


# ---------------------------------------------------------------------------
# Stock release on decline
# ---------------------------------------------------------------------------
def test_decline_releases_the_unit_once_even_when_redelivered(stripe, pay_app, make_sale,
                                                              call_app, redis_client, schema):
    sid, (user,) = make_sale(stock=2)
    oid = place_order(schema, redis_client, sid, user)
    assert sale_state(schema, sid)[1] == 1
    ev = decline_and_settle(call_app, pay_app, schema, oid)
    assert sale_state(schema, sid)[1] == 0
    for _ in range(3):
        again = call_app(pay_app, lambda c: c.post("/payment/webhook", json=ev))
        assert again.json()["handled"] is False
    assert sale_state(schema, sid)[1] == 0


def test_concurrent_redelivery_releases_once(stripe, pay_app, make_sale, call_app,
                                             redis_client, schema):
    sid, users = make_sale(stock=3, n_users=2)
    place_order(schema, redis_client, sid, users[0])            # someone else's held unit
    oid = place_order(schema, redis_client, sid, users[1])
    call_app(pay_app, lambda c: create(c, oid, DECLINE))
    ev = webhook_event("payment_intent.payment_failed", latest_intent(schema, oid), oid)
    rs = call_app(pay_app, lambda c: asyncio.gather(
        *[c.post("/payment/webhook", json=ev) for _ in range(8)]))
    assert all(r.status_code == 200 for r in rs)
    assert sale_state(schema, sid)[1] == 1                      # only OUR unit came back


def test_order_declined_twice_releases_one_unit_not_two(stripe, pay_app, make_sale, call_app,
                                                        redis_client, schema):
    """Regression: every declined attempt used to release a unit, so an order
    declined twice handed back someone else's unit too."""
    sid, users = make_sale(stock=3, n_users=2)
    place_order(schema, redis_client, sid, users[0])
    oid = place_order(schema, redis_client, sid, users[1])
    assert sale_state(schema, sid)[1] == 2
    decline_and_settle(call_app, pay_app, schema, oid)          # attempt 1
    decline_and_settle(call_app, pay_app, schema, oid)          # attempt 2 (retry)
    assert sale_state(schema, sid)[1] == 1
    assert [r[0] for r in payments_rows(schema, oid)] == [1, 2]


def test_redis_strategy_release_and_retry_use_the_redis_counter(stripe, pay_app, make_sale,
                                                                call_app, redis_client, schema):
    sid, (user,) = make_sale(stock=2)
    oid = place_order(schema, redis_client, sid, user, strategy="redis")
    assert int(redis_client.get(f"stock:{sid}")) == 1
    decline_and_settle(call_app, pay_app, schema, oid)
    assert int(redis_client.get(f"stock:{sid}")) == 2
    ok = call_app(pay_app, lambda c: create(c, oid))
    assert ok.status_code == 200 and ok.json()["attempt"] == 2
    assert int(redis_client.get(f"stock:{sid}")) == 1
    assert sale_state(schema, sid)[1] == 0                      # SQL counter untouched


# ---------------------------------------------------------------------------
# Paid retries after a released decline
# ---------------------------------------------------------------------------
def test_successful_retry_takes_its_unit_back(stripe, pay_app, make_sale, call_app,
                                              redis_client, schema):
    sid, (user,) = make_sale(stock=2)
    oid = place_order(schema, redis_client, sid, user)
    decline_and_settle(call_app, pay_app, schema, oid)
    assert sale_state(schema, sid)[1] == 0
    ok = call_app(pay_app, lambda c: create(c, oid))
    assert ok.status_code == 200 and ok.json()["attempt"] == 2
    assert sale_state(schema, sid)[1] == 1
    assert len(stripe.charges) == 1


def test_retry_is_refused_when_the_released_unit_was_sold(stripe, pay_app, make_sale, call_app,
                                                          redis_client, schema):
    """Regression: the retry used to charge the card for a unit that had
    already gone back on sale and been bought by someone else -- an oversell."""
    sid, users = make_sale(stock=1, n_users=2)
    oid = place_order(schema, redis_client, sid, users[0])
    decline_and_settle(call_app, pay_app, schema, oid)
    place_order(schema, redis_client, sid, users[1])            # the freed unit sells
    calls = stripe.calls
    r = call_app(pay_app, lambda c: create(c, oid))
    assert r.status_code == 409
    assert stripe.calls == calls and stripe.charges == []
    assert sale_state(schema, sid)[:2] == (1, 1)


def test_simultaneous_retries_reserve_one_unit_and_charge_once(stripe, pay_app, make_sale,
                                                               call_app, redis_client, schema):
    sid, (user,) = make_sale(stock=5)
    oid = place_order(schema, redis_client, sid, user)
    decline_and_settle(call_app, pay_app, schema, oid)
    responses = call_app(pay_app, lambda c: asyncio.gather(*[create(c, oid) for _ in range(8)]))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert {r.json()["attempt"] for r in responses} == {2}
    assert sale_state(schema, sid)[1] == 1
    assert len(stripe.charges) == 1


def test_late_failure_event_cannot_unpay_a_succeeded_payment(stripe, pay_app, make_sale,
                                                             call_app, redis_client, schema):
    sid, (user,) = make_sale(stock=2)
    oid = place_order(schema, redis_client, sid, user)
    call_app(pay_app, lambda c: create(c, oid))
    pi = latest_intent(schema, oid)
    call_app(pay_app, lambda c: c.post("/payment/webhook",
                                       json=webhook_event("payment_intent.succeeded", pi, oid)))
    r = call_app(pay_app, lambda c: c.post("/payment/webhook",
                                           json=webhook_event("payment_intent.payment_failed", pi, oid)))
    assert r.status_code == 200 and r.json()["handled"] is False
    assert payments_rows(schema, oid) == [(1, "succeeded", False)]
    assert sale_state(schema, sid)[1] == 1


# ---------------------------------------------------------------------------
# Webhook trust
# ---------------------------------------------------------------------------
def test_unsigned_webhook_is_refused_by_default(pay_app, call_app, monkeypatch):
    from app import payments
    monkeypatch.setattr(payments, "ALLOW_UNVERIFIED_WEBHOOKS", False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    ev = webhook_event("payment_intent.payment_failed", "pi_x", str(uuid.uuid4()))
    assert call_app(pay_app, lambda c: c.post("/payment/webhook", json=ev)).status_code == 503


def test_badly_signed_webhook_is_refused(pay_app, call_app, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    ev = webhook_event("payment_intent.payment_failed", "pi_x", str(uuid.uuid4()))
    r = call_app(pay_app, lambda c: c.post("/payment/webhook", content=json.dumps(ev),
                                           headers={"stripe-signature": "t=1,v1=deadbeef"}))
    assert r.status_code == 400


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"type": "x"}', b'{"type":"x","data":{}}'])
def test_malformed_webhook_is_a_400_not_a_500(stripe, pay_app, call_app, body):
    r = call_app(pay_app, lambda c: c.post("/payment/webhook", content=body))
    assert r.status_code == 400


def test_unrelated_event_types_are_acknowledged(stripe, pay_app, call_app):
    ev = {"id": "evt_1", "type": "charge.refunded", "data": {"object": {"id": "ch_1"}}}
    r = call_app(pay_app, lambda c: c.post("/payment/webhook", json=ev))
    assert r.status_code == 200 and r.json()["handled"] is False
