"""
Chaos verification harness -- black-box concurrency testing of the three
checkout strategies.

Treats the running system entirely as a black box: every purchase goes over
the real HTTP API, exactly as a browser would. Nothing here imports or
patches the checkout code, and nothing writes to the database. Stock is set
up through the existing POST /admin/reset-sale endpoint; the only direct
database access is read-only counting for the invariant check (see
count_confirmed_orders() for why the API cannot answer that question).

THE ONE HARD INVARIANT
    confirmed orders created during a run <= stock the sale started with

Anything else it reports -- latency, duplicate outcomes, aborted requests --
is context. That single line is the pass/fail.

    python -m scripts.chaos_verification
    python -m scripts.chaos_verification --n 500 --chaos 0.4
    python -m scripts.chaos_verification --strategy redis --n 1000 --stock 10

Requires the API to be running (python -m app.main).
"""
import argparse
import asyncio
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from sqlalchemy import text

from app.database import SessionLocal

STRATEGIES = ["pessimistic", "optimistic", "redis"]
DEFAULT_BASE_URL = os.getenv("CHAOS_BASE_URL", "http://127.0.0.1:8010")


# --------------------------------------------------------------------------
# Result records
# --------------------------------------------------------------------------
@dataclass
class Attempt:
    """One HTTP request. `logical_id` groups a duplicate with its original."""
    logical_id: int
    is_duplicate: bool
    status: str            # confirmed | sold_out | failed | aborted | error
    latency_ms: float | None
    http_code: int | None = None
    order_id: str | None = None
    detail: str = ""


@dataclass
class StrategyRun:
    strategy: str
    sale_id: str
    n: int
    chaos_fraction: float
    starting_stock: int
    attempts: list[Attempt] = field(default_factory=list)
    wall_time_s: float = 0.0
    confirmed_in_db: int = 0          # ground truth, counted server-side
    baseline_orders: int = 0
    # Availability as each store reports it afterwards. Kept separate on
    # purpose: the strategies do not all write to the same counter, so a
    # single "available" number would look self-contradictory in the report.
    final_reported: int | None = None
    final_sql: int | None = None
    final_redis: int | None = None
    error: str | None = None

    # ---- derived ----
    @property
    def client_confirmed(self):
        return sum(1 for a in self.attempts if a.status == "confirmed")

    @property
    def sold_out(self):
        return sum(1 for a in self.attempts if a.status == "sold_out")

    @property
    def aborted(self):
        return sum(1 for a in self.attempts if a.status == "aborted")

    @property
    def failed(self):
        return sum(1 for a in self.attempts if a.status in ("failed", "error"))

    @property
    def oversold(self):
        """How far past the starting stock we went. 0 means the guarantee held."""
        return max(0, self.confirmed_in_db - self.starting_stock)

    @property
    def invariant_holds(self):
        return self.confirmed_in_db <= self.starting_stock

    @property
    def duplicate_pairs(self):
        """(logical_id, [statuses]) for every logical buyer that sent twice."""
        by_logical = {}
        for a in self.attempts:
            by_logical.setdefault(a.logical_id, []).append(a)
        return {k: v for k, v in by_logical.items() if len(v) > 1}

    @property
    def duplicates_both_confirmed(self):
        return sum(
            1 for v in self.duplicate_pairs.values()
            if sum(1 for a in v if a.status == "confirmed") > 1
        )

    def percentile(self, p):
        lat = sorted(a.latency_ms for a in self.attempts if a.latency_ms is not None)
        if not lat:
            return None
        k = max(0, min(len(lat) - 1, int(round((p / 100) * len(lat) + 0.5)) - 1))
        return lat[k]


# --------------------------------------------------------------------------
# Read-only database access (counting only -- never writes)
# --------------------------------------------------------------------------
def count_confirmed_orders(sale_id, since=None):
    """
    Ground truth for the invariant.

    Deliberately NOT read from GET /sales/{id}: that reports availability,
    and the Redis fast path does not decrement inventory.reserved_stock at
    all (by design -- see the CAP note on /checkout/redis), so SQL
    availability would under-report Redis purchases and quietly hide a real
    oversell. The orders table is the only place all three strategies agree
    on what was actually sold.

    Equally deliberately NOT counted from the harness's own successful
    responses: a client that aborts mid-flight can leave a committed order it
    never saw the confirmation for. Counting client-side would miss exactly
    the orders chaos testing exists to find.

    SELECT only. This function is the sole database contact in this file.
    """
    db = SessionLocal()
    try:
        sql = "SELECT count(*) FROM orders WHERE sale_id = :sid AND status = 'confirmed'"
        params = {"sid": sale_id}
        if since is not None:
            sql += " AND created_at >= :since"
            params["since"] = since
        return db.execute(text(sql), params).scalar() or 0
    finally:
        db.close()


def fetch_user_ids(limit):
    """Real user ids for the FK on orders.user_id. Read-only."""
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT id FROM users ORDER BY created_at NULLS LAST LIMIT :lim"),
            {"lim": max(1, limit)},
        ).fetchall()
        return [str(r[0]) for r in rows]
    finally:
        db.close()


# --------------------------------------------------------------------------
# Setup via the existing API
# --------------------------------------------------------------------------
async def discover_sales(client):
    r = await client.get("/sales")
    r.raise_for_status()
    return r.json().get("sales", [])


async def reset_sale(client, sale_id, stock, token=""):
    """Uses the existing admin endpoint -- resets PostgreSQL inventory AND the
    Redis counter together, which is what makes 'starting stock' exact for
    all three strategies."""
    params = {"stock": stock}
    if token:
        params["token"] = token
    r = await client.post(f"/admin/reset-sale/{sale_id}", params=params)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------
# Chaos
# --------------------------------------------------------------------------
class Chaos:
    """
    Client-side chaos only -- the server is never touched or degraded.

      delay     a random pause before firing, smearing the thundering herd
      abort     the request is cancelled mid-flight (dropped client). The
                server may still commit; that is the point.
      duplicate the same logical purchase is sent a second time.

    Duplicates are sent as two INDEPENDENT requests because the API has no
    idempotency key yet. Both succeeding is therefore the expected outcome,
    not a bug -- it is recorded so the eventual idempotency work has a
    before-picture.
    """

    def __init__(self, fraction, rng):
        self.fraction = fraction
        self.rng = rng

    def roll(self):
        if self.rng.random() >= self.fraction:
            return {"delay": 0.0, "abort": False, "duplicate": False}
        kind = self.rng.choice(["delay", "abort", "duplicate"])
        return {
            "delay": self.rng.uniform(0.005, 0.25) if kind == "delay" else 0.0,
            "abort": kind == "abort",
            "duplicate": kind == "duplicate",
        }


async def one_purchase(client, strategy, sale_id, user_id, logical_id,
                       plan, barrier, results, is_duplicate=False):
    """A single buyer. Waits on the barrier so everyone fires together."""
    await barrier.wait()

    if plan["delay"]:
        await asyncio.sleep(plan["delay"])

    started = time.perf_counter()
    params = {"sale_id": sale_id, "user_id": user_id}
    try:
        if plan["abort"] and not is_duplicate:
            # Cancel mid-flight: the connection dies before the response is
            # read. Whether the server committed is genuinely unknown here --
            # which is exactly the ambiguity being tested.
            try:
                await asyncio.wait_for(
                    client.post(f"/checkout/{strategy}", params=params),
                    timeout=random.uniform(0.001, 0.02),
                )
            except (asyncio.TimeoutError, httpx.HTTPError):
                results.append(Attempt(logical_id, is_duplicate, "aborted", None,
                                       detail="client aborted mid-flight"))
                return
            # It answered before we could cut it -- fall through as normal.

        resp = await client.post(f"/checkout/{strategy}", params=params)
        elapsed = (time.perf_counter() - started) * 1000
        try:
            body = resp.json()
        except Exception:                                      # noqa: BLE001
            body = {}
        results.append(Attempt(
            logical_id, is_duplicate,
            body.get("status", "failed"), elapsed, resp.status_code,
            body.get("order_id"), body.get("message", "")[:120],
        ))
    except Exception as exc:                                   # noqa: BLE001
        elapsed = (time.perf_counter() - started) * 1000
        results.append(Attempt(logical_id, is_duplicate, "error", elapsed,
                               detail=f"{type(exc).__name__}: {exc}"[:120]))


async def run_strategy(base_url, strategy, sale_id, n, chaos_fraction,
                       stock, user_ids, admin_token, seed):
    run = StrategyRun(strategy=strategy, sale_id=sale_id, n=n,
                      chaos_fraction=chaos_fraction, starting_stock=stock)
    rng = random.Random(seed)
    chaos = Chaos(chaos_fraction, rng)

    limits = httpx.Limits(max_connections=n + 50, max_keepalive_connections=n + 50)
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, limits=limits) as client:
        try:
            await reset_sale(client, sale_id, stock, admin_token)
        except Exception as exc:                               # noqa: BLE001
            run.error = f"could not reset sale: {exc}"
            return run

        # Everything created from this instant belongs to this run.
        cutoff = datetime.now(timezone.utc)
        run.baseline_orders = count_confirmed_orders(sale_id)
        await asyncio.sleep(0.05)

        results = []
        barrier = asyncio.Event()
        tasks = []
        for i in range(n):
            plan = chaos.roll()
            uid = user_ids[i % len(user_ids)]
            tasks.append(one_purchase(client, strategy, sale_id, uid, i, plan,
                                      barrier, results))
            if plan["duplicate"]:
                # Same logical buyer, second independent request.
                tasks.append(one_purchase(client, strategy, sale_id, uid, i,
                                          {"delay": rng.uniform(0.0, 0.05),
                                           "abort": False, "duplicate": False},
                                          barrier, results, is_duplicate=True))

        gathered = asyncio.gather(*tasks)
        t0 = time.perf_counter()
        barrier.set()                       # release the herd
        await gathered
        run.wall_time_s = time.perf_counter() - t0

        run.attempts = results
        # Let any in-flight commit from an aborted connection land.
        await asyncio.sleep(0.75)
        run.confirmed_in_db = count_confirmed_orders(sale_id, since=cutoff)

        try:
            body = (await client.get(f"/sales/{sale_id}")).json()
            run.final_reported = body.get("available")
            run.final_sql = body.get("sql_available")
            run.final_redis = body.get("redis_available")
        except Exception:                                      # noqa: BLE001
            pass

    return run


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def fmt(v, suffix=""):
    return "--" if v is None else f"{v:.1f}{suffix}"


def build_report(runs, args, sale_names):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    any_violation = any(not r.invariant_holds for r in runs if not r.error)
    any_dupes = any(r.duplicates_both_confirmed for r in runs if not r.error)

    L = []
    L.append("# Chaos Verification Report\n")
    L.append(f"Generated: {now}\n")
    L.append(
        f"Black-box run against the live HTTP API at `{args.base_url}`. "
        f"No checkout code, schema, or configuration was modified; stock was "
        f"prepared through the existing `POST /admin/reset-sale` endpoint and "
        f"verified with read-only `SELECT`s.\n"
    )
    L.append("## Configuration\n")
    L.append(f"- Concurrent buyers (N): **{args.n}** per strategy")
    L.append(f"- Chaos fraction: **{args.chaos:.0%}** of requests")
    L.append(f"- Starting stock: **{args.stock}** units per sale")
    L.append(f"- Strategies: {', '.join(r.strategy for r in runs)}")
    L.append(f"- RNG seed: {args.seed} (re-run with the same seed to reproduce)\n")

    L.append("## The invariant\n")
    L.append("> **Confirmed orders created during a run must never exceed the "
             "stock the sale started with.**\n")
    L.append(
        "Counted from the `orders` table rather than from this harness's own "
        "responses. A client that aborts mid-flight can leave behind a "
        "committed order it never saw confirmed, so a client-side tally would "
        "under-count exactly the case chaos testing exists to surface. It is "
        "also not read from `GET /sales/{id}`: the Redis fast path never "
        "decrements `inventory.reserved_stock`, so SQL availability would "
        "under-report Redis purchases.\n"
    )

    L.append("## Summary\n")
    L.append("| Strategy | N | Stock | Confirmed (DB) | Oversold | Invariant | "
             "Sold out | Aborted | Failed | Dup pairs | Dup both OK | "
             "p50 | p95 | p99 | Wall |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in runs:
        if r.error:
            L.append(f"| {r.strategy} | {r.n} | {r.starting_stock} | "
                     f"ERROR | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- |")
            continue
        L.append(
            f"| `{r.strategy}` | {r.n} | {r.starting_stock} | **{r.confirmed_in_db}** | "
            f"{r.oversold} | {'✅ HOLDS' if r.invariant_holds else '❌ VIOLATED'} | "
            f"{r.sold_out} | {r.aborted} | {r.failed} | {len(r.duplicate_pairs)} | "
            f"{r.duplicates_both_confirmed} | "
            f"{fmt(r.percentile(50), 'ms')} | {fmt(r.percentile(95), 'ms')} | "
            f"{fmt(r.percentile(99), 'ms')} | {r.wall_time_s:.2f}s |"
        )
    L.append("")

    verdict = "❌ **AT LEAST ONE INVARIANT VIOLATION**" if any_violation \
        else "✅ **All invariants held.**"
    L.append(f"### Verdict: {verdict}\n")
    if not any_violation:
        L.append(
            "Under this load, with chaos injected, no strategy sold more units "
            "than it had. Note that this is evidence, not proof: concurrency "
            "bugs are probabilistic, and a passing run bounds how easily a "
            "defect reproduces rather than showing there is none. Re-run at "
            "higher N and higher chaos before treating it as settled.\n"
        )

    # ---- per-strategy detail ----
    L.append("## Per-strategy detail\n")
    for r in runs:
        L.append(f"### `{r.strategy}` — sale `{r.sale_id}`")
        L.append(f"*{sale_names.get(r.sale_id, 'unknown product')}*\n")
        if r.error:
            L.append(f"**Run failed:** {r.error}\n")
            continue

        L.append(f"- Requests sent: **{len(r.attempts)}** "
                 f"({r.n} logical buyers + {len(r.attempts) - r.n} chaos duplicates)")
        L.append(f"- Confirmed in database: **{r.confirmed_in_db}** / {r.starting_stock} stock")
        L.append(f"- Confirmed seen by clients: {r.client_confirmed}")
        gap = r.confirmed_in_db - r.client_confirmed
        if gap > 0:
            L.append(f"  - **{gap} order(s) committed that no client saw confirmed** — "
                     f"aborted connections whose transaction still landed. Invisible "
                     f"to the buyer, real in the database.")
        L.append(f"- Rejected as sold out: {r.sold_out}")
        L.append(f"- Aborted mid-flight (chaos): {r.aborted}")
        L.append(f"- Failed / errored: {r.failed}")
        L.append(f"- Final availability — reported `{r.final_reported}` "
                 f"(SQL `{r.final_sql}`, Redis `{r.final_redis}`)")
        if (r.final_sql is not None and r.final_redis is not None
                and r.final_sql != r.final_redis):
            owner = "Redis" if r.strategy == "redis" else "PostgreSQL"
            other = "PostgreSQL" if r.strategy == "redis" else "Redis"
            L.append(f"  - The two disagree, which is expected here rather than "
                     f"alarming: `{r.strategy}` decrements **{owner}** only, so "
                     f"**{other}** still shows its pre-run figure. Each strategy "
                     f"owns one counter; nothing reconciles them, by design (see "
                     f"the CAP note on `/checkout/redis`). It is also why the "
                     f"invariant above is counted from the `orders` table, which "
                     f"every strategy writes to, instead of from either counter.")
        L.append(f"- Latency p50 / p95 / p99: {fmt(r.percentile(50), 'ms')} / "
                 f"{fmt(r.percentile(95), 'ms')} / {fmt(r.percentile(99), 'ms')}")
        L.append(f"- Wall time: {r.wall_time_s:.2f}s "
                 f"({len(r.attempts) / max(r.wall_time_s, 1e-9):.0f} req/s)\n")

        if not r.invariant_holds:
            L.append(f"> ❌ **INVARIANT VIOLATED — oversold by {r.oversold}.**")
            L.append(f"> `{r.strategy}` confirmed {r.confirmed_in_db} orders against "
                     f"{r.starting_stock} units of stock. Every extra order is a unit "
                     f"promised to a customer that does not exist. This is a real "
                     f"finding and is reported as-is; the underlying implementation "
                     f"was deliberately not changed to make this pass.\n")

        dp = r.duplicate_pairs
        if dp:
            both = r.duplicates_both_confirmed
            L.append(f"**Duplicate requests:** {len(dp)} logical buyer(s) sent the same "
                     f"purchase twice (simulated retry-after-timeout).")
            if both:
                L.append(f"{both} of those had **both requests confirmed** — the same "
                         f"buyer got two units from one intent.\n")
                L.append("> Expected for now: the API has no idempotency key, so two "
                         "independent POSTs are two independent purchases and the "
                         "server has no way to know they were one intent. This does "
                         "**not** violate the stock invariant — the total is still "
                         "capped — but it is the exact behaviour idempotency is meant "
                         "to remove, recorded here as a before-picture for that work.\n")
            else:
                L.append("None of them had both requests confirmed — in this run the "
                         "duplicates lost the race for remaining stock rather than "
                         "being deduplicated. That is scarcity doing the work, not "
                         "idempotency; at higher stock levels both would be expected "
                         "to succeed.\n")

    # ---- closing notes ----
    L.append("## Reading these numbers\n")
    L.append("- **Aborted** requests have no latency recorded — they were cancelled "
             "before a response arrived, so percentiles cover completed requests only.")
    L.append("- **Failed** includes optimistic-strategy version conflicts, which are "
             "the OCC design working (a losing writer is told to retry), not an error.")
    L.append("- A `confirmed (DB)` count *lower* than stock is normal when chaos "
             "aborts requests before they reach the server.")
    if any_dupes:
        L.append("- **Duplicates both succeeding is currently expected** and is "
                 "tracked separately from the stock invariant.")
    L.append("")
    L.append("Re-run at other scales:\n")
    L.append("```bash")
    L.append("python -m scripts.chaos_verification --n 1000 --chaos 0.5")
    L.append("python -m scripts.chaos_verification --strategy optimistic --n 500 --stock 25")
    L.append("```")
    return "\n".join(L)


# --------------------------------------------------------------------------
async def main_async(args):
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as client:
        try:
            sales = await discover_sales(client)
        except Exception as exc:                               # noqa: BLE001
            print(f"ERROR: cannot reach the API at {args.base_url} ({exc}).")
            print("Start it with:  python -m app.main")
            return 1

    if not sales:
        print("ERROR: no active sales found.")
        print("Seed some with:  python -m scripts.seed_storefront")
        return 1

    targets = STRATEGIES if args.strategy == "all" else [args.strategy]

    # A distinct sale per strategy where possible, so runs cannot contaminate
    # each other. Falls back to reusing one -- the per-run cutoff timestamp
    # keeps the counting correct either way.
    sale_names = {s["sale_id"]: s["product"] for s in sales}
    assigned = {st: sales[i % len(sales)]["sale_id"] for i, st in enumerate(targets)}
    if len(sales) < len(targets):
        print(f"NOTE: only {len(sales)} sale(s) available for {len(targets)} strategies; "
              f"reusing sales. Counting stays correct (per-run cutoff).")

    user_ids = fetch_user_ids(max(args.n, 30))
    if not user_ids:
        print("ERROR: no users in the database. Run:  python -m scripts.seed")
        return 1

    print(f"Chaos verification — N={args.n}, chaos={args.chaos:.0%}, stock={args.stock}")
    print(f"API: {args.base_url}   users available: {len(user_ids)}\n")

    runs = []
    for strategy in targets:
        sale_id = assigned[strategy]
        print(f"[{strategy}] sale {sale_id[:8]}… resetting to {args.stock} units, "
              f"firing {args.n} buyers…", flush=True)
        run = await run_strategy(args.base_url, strategy, sale_id, args.n,
                                 args.chaos, args.stock, user_ids,
                                 args.admin_token, args.seed)
        runs.append(run)
        if run.error:
            print(f"  ERROR: {run.error}\n")
            continue
        flag = "HOLDS" if run.invariant_holds else "VIOLATED"
        print(f"  confirmed(DB)={run.confirmed_in_db}/{run.starting_stock}  "
              f"sold_out={run.sold_out}  aborted={run.aborted}  "
              f"dup_both_ok={run.duplicates_both_confirmed}  "
              f"wall={run.wall_time_s:.2f}s  INVARIANT {flag}\n", flush=True)

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(build_report(runs, args, sale_names))

    print(f"Report written to {out_path}")
    violated = [r.strategy for r in runs if not r.error and not r.invariant_holds]
    if violated:
        print(f"\n*** INVARIANT VIOLATED: {', '.join(violated)} ***")
        return 2
    print("\nAll invariants held.")
    return 0


def main():
    p = argparse.ArgumentParser(
        description="Black-box chaos verification of the checkout strategies.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--n", type=int, default=200, help="concurrent buyers per strategy")
    p.add_argument("--chaos", type=float, default=0.3,
                   help="fraction of requests given chaos (0.0-1.0)")
    p.add_argument("--strategy", choices=STRATEGIES + ["all"], default="all",
                   help="which strategy to exercise")
    p.add_argument("--stock", type=int, default=5, help="starting stock per sale")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="running API base URL")
    p.add_argument("--admin-token", default=os.getenv("ADMIN_TOKEN", ""),
                   help="value for ?token= if the server sets ADMIN_TOKEN")
    p.add_argument("--seed", type=int, default=1337, help="RNG seed for reproducible chaos")
    p.add_argument("--out", default="results/chaos_verification_report.md",
                   help="report output path")
    args = p.parse_args()

    if not 0.0 <= args.chaos <= 1.0:
        p.error("--chaos must be between 0.0 and 1.0")
    if args.n < 1:
        p.error("--n must be >= 1")

    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
