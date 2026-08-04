"""
Section 8.3 -- Timestamp Ordering (T/O) concurrency control.

Postgres has no native T/O protocol (it's MVCC + optional explicit
locking), so this demo implements Basic Timestamp Ordering, extended with
Thomas's Write Rule, as an application-level protocol layered on top of
the real Inventory row -- alongside the pessimistic locking (demo_2), OCC
(demo_3), and Redis DECR (demo_4) strategies already shown for Section 8.2.

Protocol:
  - Every transaction gets a monotonically increasing logical timestamp
    TS(T) the instant it starts. A logical counter, not wall-clock time,
    since two transactions can start in the same millisecond and still
    need a strict, gap-free ordering.
  - Each data item (here: one Inventory row) tracks R-TS (the highest
    timestamp of any transaction that has read it) and W-TS (the highest
    timestamp of any transaction that has written it).
  - READ rule: if TS(T) < W-TS(item), T would be reading a value already
    overwritten by a "later" transaction -- REJECT (rollback). Otherwise
    allow the read and set R-TS(item) = max(R-TS(item), TS(T)).
  - WRITE rule: if TS(T) < R-TS(item), a later transaction has already
    read a value this write would retroactively invalidate -- REJECT
    (rollback), no exception. Otherwise, if TS(T) < W-TS(item), a later
    transaction has already written a newer value, so this write is
    already obsolete. Under strict Basic T/O that's still a REJECT; under
    THOMAS'S WRITE RULE we just SKIP the write (ignore it silently) since
    it can have no observable effect and aborting the whole transaction
    over it would be wasted work. Otherwise allow the write and set
    W-TS(item) = TS(T).

R-TS/W-TS live in a plain in-memory dict here, NOT in the inventory table.
That's a deliberate simplification for the demo: a real implementation
would need to persist them (extra columns on inventory, or a separate
timestamp-tracking table) so they survive a crash. An in-memory dict is
wiped the instant the process restarts, silently resetting every item's
ordering history and letting a since-superseded transaction through
undetected -- exactly the kind of durability problem Section 7 covers.

Run with: venv/bin/python -m app.demos.demo_9_timestamp_ordering
"""
import sys
import os
import itertools

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from app.database import SessionLocal
from scripts.seed import DEMO_SALE_ID

# Fixed canonical id from scripts/seed.py -- stable across reseeds.
SALE_ID = str(DEMO_SALE_ID)

_clock = itertools.count(1)


def next_timestamp():
    return next(_clock)


# In-memory R-TS / W-TS store, keyed by inventory_id. See module
# docstring for why this doesn't survive a crash in this form.
read_ts = {}
write_ts = {}

TRACE = []


def log(txn, op, decision, detail):
    TRACE.append({
        "txn": txn["name"], "ts": txn["ts"], "op": op,
        "r_ts": read_ts.get(txn["inv_id"], 0), "w_ts": write_ts.get(txn["inv_id"], 0),
        "decision": decision, "detail": detail,
    })
    print(f"  [{txn['name']} TS={txn['ts']}] {op:6} -> {decision:9} {detail}")


def to_read(db, txn):
    """Attempt a T/O-gated read of the inventory row."""
    inv_id, ts = txn["inv_id"], txn["ts"]
    w = write_ts.get(inv_id, 0)
    if ts < w:
        log(txn, "READ", "REJECTED", f"TS({ts}) < W-TS({w}) -- would read a value already overwritten by a later txn")
        return None
    read_ts[inv_id] = max(read_ts.get(inv_id, 0), ts)
    row = db.execute(text("SELECT total_stock, reserved_stock FROM inventory WHERE id = :iid"), {"iid": inv_id}).fetchone()
    available = row[0] - row[1]
    log(txn, "READ", "ALLOWED", f"R-TS updated to {read_ts[inv_id]}; sees {available} unit(s) available")
    return available


def to_write(db, txn, thomas_rule=True):
    """Attempt a T/O-gated write (reserve one unit). If thomas_rule=True,
    an obsolete-but-harmless write (TS < W-TS with no intervening read)
    is silently skipped instead of aborting the whole transaction."""
    inv_id, ts = txn["inv_id"], txn["ts"]
    r = read_ts.get(inv_id, 0)
    w = write_ts.get(inv_id, 0)

    if ts < r:
        log(txn, "WRITE", "REJECTED", f"TS({ts}) < R-TS({r}) -- a later txn already read a value this write would invalidate")
        return "rejected"

    if ts < w:
        if thomas_rule:
            log(txn, "WRITE", "SKIPPED", f"TS({ts}) < W-TS({w}) but no reader saw a TS in between -- Thomas's Write Rule: obsolete write ignored, no rollback needed")
            return "skipped"
        log(txn, "WRITE", "REJECTED", f"TS({ts}) < W-TS({w}) -- a later txn already wrote a newer value (strict Basic T/O)")
        return "rejected"

    write_ts[inv_id] = ts
    db.execute(text("UPDATE inventory SET reserved_stock = reserved_stock + 1 WHERE id = :iid"), {"iid": inv_id})
    db.commit()
    log(txn, "WRITE", "ALLOWED", f"W-TS updated to {ts}; unit reserved")
    return "confirmed"


def reset_inventory():
    db = SessionLocal()
    inv_id = db.execute(text("SELECT id FROM inventory WHERE sale_id = :sid"), {"sid": SALE_ID}).fetchone()[0]
    db.execute(text("UPDATE inventory SET reserved_stock = 0, total_stock = 1, version = 0 WHERE id = :iid"), {"iid": inv_id})
    db.commit()
    db.close()
    read_ts.clear()
    write_ts.clear()
    TRACE.clear()
    return inv_id


def print_trace_table():
    print("\n" + "=" * 78)
    print("TRACE TABLE -- paste directly into your report")
    print("=" * 78 + "\n")
    print("| Txn | TS | Op | R-TS (after) | W-TS (after) | Decision | Why |")
    print("|---|---|---|---|---|---|---|")
    for row in TRACE:
        print(f"| {row['txn']} | {row['ts']} | {row['op']} | {row['r_ts']} | {row['w_ts']} | {row['decision']} | {row['detail']} |")


def run():
    inv_id = reset_inventory()
    db = SessionLocal()

    print("=" * 78)
    print("Section 8.3 -- Timestamp Ordering (Basic T/O + Thomas's Write Rule)")
    print("=" * 78)
    print("Scenario: 3 overlapping checkouts race for the LAST unit of stock.")
    print("T1, T2, T3 get logical timestamps in start order (1, 2, 3), but network")
    print("delay means they don't execute in that order -- exactly the mismatch")
    print("between issue-order and arrival-order that T/O exists to police.\n")

    t1 = {"name": "T1", "ts": next_timestamp(), "inv_id": inv_id}
    t2 = {"name": "T2", "ts": next_timestamp(), "inv_id": inv_id}
    t3 = {"name": "T3", "ts": next_timestamp(), "inv_id": inv_id}
    print(f"Timestamps assigned at start: T1={t1['ts']}  T2={t2['ts']}  T3={t3['ts']}\n")

    print("Step 1: T2 checks stock (TO-gated read).")
    to_read(db, t2)

    print("\nStep 2: T3 goes straight to checkout -- it's working off stock info from")
    print("        an earlier page load, not a fresh TO-gated read -- and writes directly.")
    to_write(db, t3)

    print("\nStep 3: T2 now tries to complete its checkout (write), based on the")
    print("        availability it saw back in Step 1 -- but T3 has since sold the unit.")
    to_write(db, t2)

    print("\nStep 4: T1 finally arrives (it was the slowest) and tries to check stock.")
    to_read(db, t1)

    print("\n" + "=" * 78)
    print("Without Thomas's Write Rule, Step 3 would have gone differently:")
    print("=" * 78)
    r, w = read_ts[inv_id], write_ts[inv_id]
    print(f"  [T2 TS={t2['ts']}] WRITE  -> REJECTED  TS({t2['ts']}) < W-TS({w}) -- strict Basic T/O aborts T2")
    print("  T2 would have to roll back its ENTIRE transaction and retry from scratch,")
    print("  even though its write could never have had any effect once T3 committed.")
    print("  Thomas's Write Rule recognizes that (no reader's TS fell between T2's and")
    print("  T3's) and just drops the stale write instead of paying for a full abort.\n")

    row = db.execute(text("SELECT total_stock, reserved_stock FROM inventory WHERE id = :iid"), {"iid": inv_id}).fetchone()
    print(f"Final inventory state: total_stock={row[0]}, reserved_stock={row[1]} "
          f"-- exactly 1 unit sold (to T3), no oversell, and T2 avoided a wasted rollback.")

    db.close()
    print_trace_table()


if __name__ == "__main__":
    run()
