"""
Pilot: futility-aware admission for depleting-inventory (flash-sale) workloads.

Real PostgreSQL, one hot inventory row, a herd of N buyers for S units.
Admitted buyers may NOT show up (token expires after TTL) and buyers who
reserve a unit may FAIL payment (unit is released back). The sale has a
deadline. We compare admission policies:

  raw          everyone admitted at t=0 (no waiting room)
  rate:K       open-loop waiting room, K admits per 100 ms (repo's static mode)
  win:W        closed window of W outstanding admits
  aimd         latency/conflict-driven congestion window (repo's AIMD idea)
  stock:b      futility window only: outstanding <= ceil(b * free_units)
  fa           futility window with online show-rate: ceil(free / p_hat)
  fa+aimd      min(aimd cwnd, ceil(free / p_hat))

"free_units" = total - held - paid. A buyer admitted when free_units are
already spoken for can, at best, lose -- that admission is futile.
"""
import argparse, asyncio, bisect, json, math, random, time

import asyncpg

DSN = dict(host="/tmp", port=5440, user="postgres", database="postgres")


class Sim:
    def __init__(s, a):
        s.a = a
        s.rng = random.Random(a.seed)
        s.S, s.N = a.stock, a.buyers
        s.pending = 0          # admitted, not yet resolved before/at checkout
        s.inflight = 0         # inside checkout (incl. pool wait)
        s.holds = s.paid = 0
        s.admitted = s.admitted_lost = s.noshow = 0
        s.db_txns = s.conflicts = s.soldout_attempts = s.reservations = s.releases = 0
        s.lat = []
        s.recent = []          # (t_end, latency, conflicts_in_this_checkout)
        s.admit_log = []       # [admit_t, showed]
        s.peak_pending = s.peak_inflight = 0
        s.t_sellout = s.t_95 = None
        s.done = False
        s.cwnd_trace = []
        s.show_delays = []
        s._ph, s._ph_t = 1.0, -1.0
        s.limit = 2.0 if a.cl else 1e9
        s.db_inflight = 0
        s.db_recent = []
        s.cond = asyncio.Condition()

    def now(s):
        return time.perf_counter() - s.t0

    @property
    def free(s):
        return s.S - s.holds - s.paid

    # ---------------- database ----------------
    async def checkout(s, i):
        """returns (order_id or None, conflicts)"""
        conf = 0
        if s.a.cc == "pessimistic":
            async with s.pool.acquire() as c:
                async with c.transaction():
                    s.db_txns += 1
                    row = await c.fetchrow("SELECT total,reserved FROM inventory WHERE id=1 FOR UPDATE")
                    if row["reserved"] >= row["total"]:
                        return None, 0
                    await asyncio.sleep(s.a.work)
                    await c.execute("UPDATE inventory SET reserved=reserved+1, version=version+1 WHERE id=1")
                    oid = await c.fetchval("INSERT INTO orders(buyer,status) VALUES($1,'held') RETURNING id", i)
            return oid, 0
        # optimistic (version column), retry until success or sold out
        while not s.done:
            async with s.pool.acquire() as c:
                s.db_txns += 1
                row = await c.fetchrow("SELECT total,reserved,version FROM inventory WHERE id=1")
                if row["reserved"] >= row["total"]:
                    return None, conf
                await asyncio.sleep(s.a.work)
                async with c.transaction():
                    r = await c.execute(
                        "UPDATE inventory SET reserved=reserved+1, version=version+1 WHERE id=1 AND version=$1",
                        row["version"])
                    if r == "UPDATE 1":
                        oid = await c.fetchval("INSERT INTO orders(buyer,status) VALUES($1,'held') RETURNING id", i)
                        return oid, conf
            conf += 1
            s.conflicts += 1
            await asyncio.sleep(s.rng.uniform(0, 0.005))
        return None, conf

    async def settle(s, oid, ok):
        async with s.pool.acquire() as c:
            async with c.transaction():
                s.db_txns += 1
                if ok:
                    await c.execute("UPDATE orders SET status='paid' WHERE id=$1", oid)
                else:
                    await c.execute("UPDATE inventory SET reserved=reserved-1, version=version+1 WHERE id=1")
                    await c.execute("UPDATE orders SET status='failed' WHERE id=$1", oid)

    # ---------------- buyer ----------------
    async def buyer(s, i):
        a, r = s.a, s.rng
        rec = [s.now(), None, None]   # admit time, (hidden) truth, observed show delay
        s.admit_log.append(rec)
        show = r.random() < a.p_show
        rec[1] = show
        if not show:
            await asyncio.sleep(a.ttl)
            s.pending -= 1
            s.noshow += 1
            return
        await asyncio.sleep(r.uniform(0.05, 0.30))
        if s.done:
            return
        rec[2] = s.now() - rec[0]
        bisect.insort(s.show_delays, rec[2])
        s.inflight += 1
        s.peak_inflight = max(s.peak_inflight, s.inflight)
        t = time.perf_counter()
        async with s.cond:
            while s.db_inflight >= int(s.limit) and not s.done:
                await s.cond.wait()
            s.db_inflight += 1
        td = time.perf_counter()
        try:
            oid, conf = await s.checkout(i)
        finally:
            async with s.cond:
                s.db_inflight -= 1
                s.cond.notify(1)
        s.db_recent.append((s.now(), time.perf_counter() - td, conf))
        lat = time.perf_counter() - t
        s.inflight -= 1
        s.pending -= 1
        s.lat.append(lat)
        s.recent.append((s.now(), lat, conf))
        if oid is None:
            s.soldout_attempts += 1
            s.admitted_lost += 1
            return
        s.reservations += 1
        s.holds += 1
        await asyncio.sleep(r.uniform(0.3, 1.0))
        if s.done:
            return
        ok = r.random() < a.p_pay
        await s.settle(oid, ok)
        s.holds -= 1
        if ok:
            s.paid += 1
            if s.t_95 is None and s.paid >= math.ceil(0.95 * s.S):
                s.t_95 = s.now()
            if s.paid == s.S:
                s.t_sellout = s.now()
        else:
            s.releases += 1

    # ---------------- controller ----------------
    def p_hat(s):
        """Censoring-aware show-rate estimate from server-observable data only.
        Mature cohorts (admitted >= TTL ago) are fully observed: they give an unbiased
        show/no-show count AND an unbiased show-delay CDF G. Young buyers who have not
        shown are right-censored; they contribute G(age) expected shows each:
            p_hat = shows / (n_mature + sum_young G(age))
        Before any cohort matures there is no unbiased G, so the estimate stays at the
        conservative prior p = 1 (never over-admits on no evidence)."""
        now = s.now()
        if now - s._ph_t < 0.1:
            return s._ph
        ttl = s.a.ttl
        mature_delays, n_mature, shows_mature = [], 0, 0
        young = []
        for t, _truth, delay in s.admit_log:
            age = now - t
            if age >= ttl:
                n_mature += 1
                if delay is not None:
                    shows_mature += 1
                    mature_delays.append(delay)
            else:
                young.append((age, delay))
        k = 2.0  # pseudo-observations at p = 1
        if not mature_delays:
            s._ph = 1.0
        else:
            mature_delays.sort()
            nd = len(mature_delays)
            shows = shows_mature + sum(1 for _, d in young if d is not None)
            expect = n_mature + sum(bisect.bisect_right(mature_delays, age) / nd for age, _ in young)
            s._ph = min(1.0, max(0.1, (shows + k) / (expect + k)))
        s._ph_t = now
        return s._ph

    async def controller(s):
        pol = s.a.policy
        nxt = 0
        tasks = []

        def admit(k):
            nonlocal nxt
            k = max(0, min(k, s.N - nxt))
            for _ in range(k):
                s.admitted += 1
                s.pending += 1
                s.peak_pending = max(s.peak_pending, s.pending)
                tasks.append(asyncio.create_task(s.buyer(nxt)))
                nxt += 1

        cwnd, ss, base, last_adj = 2.0, True, None, 0.0
        lss, lbase, last_l = True, None, 0.0
        if pol == "raw":
            admit(s.N)
        while not s.done:
            now = s.now()
            if now >= s.a.deadline or s.paid == s.S or (nxt >= s.N and s.pending == 0 and s.holds == 0):
                s.done = True
                break
            # AIMD window update every 0.5 s
            if pol in ("aimd", "fa+aimd") and now - last_adj >= 0.5:
                last_adj = now
                win = [x for x in s.recent if x[0] >= now - 1.0]
                if len(win) >= 5:
                    lats = sorted(x[1] for x in win)
                    p90 = lats[int(0.9 * (len(lats) - 1))]
                    base = p90 if base is None else min(base, p90)
                    conf_ratio = sum(x[2] for x in win) / max(1, len(win) + sum(x[2] for x in win))
                    congested = p90 > max(3 * base, 0.030) or conf_ratio > 0.3
                    if congested:
                        ss = False
                        cwnd = max(1.0, cwnd * 0.5)
                    elif ss:
                        cwnd = min(2000.0, cwnd * 2)
                    else:
                        cwnd = min(2000.0, cwnd + max(1.0, 0.05 * cwnd))
                elif ss and s.pending >= cwnd:
                    cwnd = min(2000.0, cwnd * 2)
                s.cwnd_trace.append((round(now, 2), round(cwnd, 1)))
            if s.a.cl and now - last_l >= 0.25:
                last_l = now
                win = [x for x in s.db_recent if x[0] >= now - 0.5]
                if len(win) >= 5:
                    lats = sorted(x[1] for x in win)
                    p90 = lats[int(0.9 * (len(lats) - 1))]
                    lbase = p90 if lbase is None else min(lbase, p90)
                    att = len(win) + sum(x[2] for x in win)
                    congested = p90 > max(2 * lbase, 0.015) or sum(x[2] for x in win) / att > 0.2
                    if congested:
                        lss = False
                        s.limit = max(1.0, s.limit * 0.5)
                    elif lss:
                        s.limit = min(float(s.a.pool), s.limit * 2)
                    else:
                        s.limit = min(float(s.a.pool), s.limit + 1)
                    async with s.cond:
                        s.cond.notify_all()
            if pol.startswith("rate:"):
                admit(int(pol.split(":")[1]))
                await asyncio.sleep(0.1)
                continue
            if pol.startswith("win:"):
                admit(int(pol.split(":")[1]) - s.pending)
            elif pol == "aimd":
                admit(int(cwnd) - s.pending)
            elif pol.startswith("stock:"):
                b = float(pol.split(":")[1])
                admit(math.ceil(b * s.free) - s.pending)
            elif pol == "fa":
                admit(math.ceil(s.free / s.p_hat()) - s.pending)
            elif pol == "fa2":
                # release-aware: units held now that are expected to come back count as claimable
                p_pay = (s.paid + 1) / (s.paid + s.releases + 2)
                expected = s.free + s.holds * (1 - p_pay)
                admit(math.ceil(expected / s.p_hat()) - s.pending)
            elif pol == "fa+aimd":
                admit(min(int(cwnd), math.ceil(s.free / s.p_hat())) - s.pending)
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        async with s.cond:
            s.cond.notify_all()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(s):
        s.pool = await asyncpg.create_pool(**DSN, min_size=s.a.pool, max_size=s.a.pool)
        async with s.pool.acquire() as c:
            await c.execute("DROP TABLE IF EXISTS orders; DROP TABLE IF EXISTS inventory;"
                            "CREATE TABLE inventory(id int primary key, total int not null, reserved int not null, version int not null);"
                            "CREATE TABLE orders(id bigserial primary key, buyer int, status text);")
            await c.execute("INSERT INTO inventory VALUES (1,$1,0,0)", s.S)
        s.t0 = time.perf_counter()
        await s.controller()
        elapsed = s.now()
        async with s.pool.acquire() as c:
            inv = await c.fetchrow("SELECT total,reserved FROM inventory WHERE id=1")
            db_paid = await c.fetchval("SELECT count(*) FROM orders WHERE status='paid'")
        await s.pool.close()
        lat = sorted(s.lat) or [0]
        q = lambda p: round(1000 * lat[int(p * (len(lat) - 1))], 1)
        a = s.a
        return dict(
            policy=a.policy + ("+cl" if a.cl else ""), cc=a.cc, ttl=a.ttl, p_show=a.p_show, p_pay=a.p_pay, seed=a.seed, S=s.S, N=s.N,
            sold=s.paid, sold_pct=round(100 * s.paid / s.S, 1), db_paid=db_paid,
            oversold=inv["reserved"] > inv["total"],
            t_sellout=None if s.t_sellout is None else round(s.t_sellout, 2),
            t_95=None if s.t_95 is None else round(s.t_95, 2), elapsed=round(elapsed, 2),
            db_txns=s.db_txns, conflicts=s.conflicts, soldout_attempts=s.soldout_attempts,
            reservations=s.reservations, releases=s.releases,
            futile_txns=s.soldout_attempts + s.conflicts,
            futile_pct=round(100 * (s.soldout_attempts + s.conflicts) / max(1, s.db_txns), 1),
            admitted=s.admitted, admitted_lost=s.admitted_lost, noshow=s.noshow,
            checkout_p50_ms=q(0.5), checkout_p99_ms=q(0.99),
            peak_pending=s.peak_pending, peak_inflight=s.peak_inflight,
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default="fa")
    ap.add_argument("--cc", default="pessimistic", choices=["pessimistic", "optimistic"])
    ap.add_argument("--stock", type=int, default=150)
    ap.add_argument("--buyers", type=int, default=2000)
    ap.add_argument("--p-show", type=float, default=0.7)
    ap.add_argument("--p-pay", type=float, default=0.8)
    ap.add_argument("--ttl", type=float, default=1.5)
    ap.add_argument("--work", type=float, default=0.005)
    ap.add_argument("--deadline", type=float, default=25.0)
    ap.add_argument("--pool", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--cl", action="store_true", help="AIMD limit on concurrent DB checkouts")
    a = ap.parse_args()
    print(json.dumps(asyncio.run(Sim(a).run())))


if __name__ == "__main__":
    main()
