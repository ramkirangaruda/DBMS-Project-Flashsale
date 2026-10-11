"""Sale window: the countdown used to be cosmetic; now the server enforces it."""
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.salewindow import SaleWindowMiddleware, SaleWindows

S = "sale-1"


def test_state_machine_with_a_fake_clock():
    t = {"now": 1000.0}
    w = SaleWindows(clock=lambda: t["now"])
    w.set_windows({S: (1100.0, 1200.0)})
    assert w.state(S) == ("not_started", 100.0)
    t["now"] = 1100.0
    assert w.state(S)[0] == "open"
    t["now"] = 1199.9
    assert w.state(S)[0] == "open"
    t["now"] = 1200.0
    assert w.state(S)[0] == "ended"
    assert w.state("never-heard-of-it")[0] == "unknown"       # fails open


def _client(windows, enforced=True):
    app = FastAPI()
    hits = {"n": 0}

    @app.post("/checkout/redis")
    def co(sale_id: str):
        hits["n"] += 1
        return {"status": "confirmed"}

    @app.get("/sales")
    def sales():
        return []

    app.add_middleware(SaleWindowMiddleware, windows=windows, enforced=enforced)
    return TestClient(app), hits


def test_checkout_outside_the_window_never_reaches_the_handler():
    t = {"now": 1000.0}
    w = SaleWindows(clock=lambda: t["now"])
    w.set_windows({S: (1100.0, 1200.0)})
    c, hits = _client(w)

    early = c.post("/checkout/redis", params={"sale_id": S})
    assert early.status_code == 403 and early.headers["X-Sale-Window"] == "not_started"
    assert early.json()["status"] == "failed"

    t["now"] = 1150.0
    assert c.post("/checkout/redis", params={"sale_id": S}).json()["status"] == "confirmed"

    t["now"] = 1300.0
    late = c.post("/checkout/redis", params={"sale_id": S})
    assert late.status_code == 403 and late.headers["X-Sale-Window"] == "ended"
    assert hits["n"] == 1


def test_reads_and_unknown_sales_are_untouched():
    w = SaleWindows(clock=lambda: 1.0)
    w.set_windows({S: (100.0, 200.0)})
    c, hits = _client(w)
    assert c.get("/sales").status_code == 200
    assert c.post("/checkout/redis", params={"sale_id": "unknown"}).status_code == 200


def test_can_be_switched_off():
    w = SaleWindows(clock=lambda: 1.0)
    w.set_windows({S: (100.0, 200.0)})
    c, _ = _client(w, enforced=False)
    assert c.post("/checkout/redis", params={"sale_id": S}).status_code == 200
