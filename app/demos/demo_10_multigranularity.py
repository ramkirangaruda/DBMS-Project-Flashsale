"""
Section 8.5 -- Multi-Granularity Locking (table-level vs row-level, with
intention locks keeping the hierarchy consistent).

Scenario: an admin "pause this entire sale" action needs to stop ALL
checkout activity on the Inventory table -- not just one row -- while it
works. Meanwhile several ordinary checkouts are trying to take row-level
locks (SELECT ... FOR UPDATE) on individual Inventory rows to complete
their purchase. Multi-granularity locking is what lets Postgres detect
"the whole table is locked" without literally scanning every row: a
coarse table-level lock and per-row locks share one hierarchy, and
INTENTION locks at the table level record "something below me is/will be
locked" so a coarse-grained request only has to check the top of the tree.

Lock mode chosen for the admin action: ACCESS EXCLUSIVE, not SHARE ROW
EXCLUSIVE. This matters, and it's a common mistake to pick the wrong one:

  - SELECT ... FOR UPDATE doesn't jump straight to a row lock. It first
    takes an implicit ROW SHARE lock at the TABLE level -- this *is*
    Postgres's intention-lock mechanism, announcing "I intend to lock a
    row below you" before it actually locks one.
  - SHARE ROW EXCLUSIVE does NOT conflict with ROW SHARE (see PostgreSQL's
    table lock conflict matrix). An admin holding SHARE ROW EXCLUSIVE
    would therefore NOT block checkouts at all -- their ROW SHARE
    intention lock would be granted immediately, defeating the entire
    point of "pause the sale".
  - ACCESS EXCLUSIVE conflicts with every other lock mode, including ROW
    SHARE, so it's the only mode that reliably blocks
    "SELECT ... FOR UPDATE" from even starting. The tradeoff: it also
    blocks plain unlocked SELECTs (which take ACCESS SHARE) -- an
    intentional choice here, since "paused" should mean nobody sees a
    stale mid-pause view of stock either.

Run with: venv/bin/python -m app.demos.demo_10_multigranularity
"""
import sys
import os
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from app.database import SessionLocal
from scripts.seed_ids import SALE_ID, USER_IDS

ADMIN_HOLD_SECONDS = 1.5
N_CHECKOUTS = 3

LOCK_MODE_MAP = [
    ("IS  (Intention Share)",     "intends to read some rows below",              "ACCESS SHARE / ROW SHARE"),
    ("IX  (Intention Exclusive)", "intends to write some rows below",             "ROW EXCLUSIVE"),
    ("S   (Share)",               "locks the whole object for reading",           "SHARE"),
    ("SIX (Share + Intention X)", "reads the whole object, will write some rows", "SHARE ROW EXCLUSIVE"),
    ("X   (Exclusive)",           "locks the whole object exclusively",           "EXCLUSIVE / ACCESS EXCLUSIVE"),
]


def get_inventory_id():
    db = SessionLocal()
    row = db.execute(text("SELECT id FROM inventory WHERE sale_id = :sid"), {"sid": SALE_ID}).fetchone()
    db.close()
    return row[0]


def admin_pause_sale(log):
    db = SessionLocal()
    try:
        db.execute(text("BEGIN"))
        t0 = time.time()
        db.execute(text("LOCK TABLE inventory IN ACCESS EXCLUSIVE MODE"))
        log.append(f"[admin]      table-level ACCESS EXCLUSIVE lock acquired -- sale is now PAUSED")
        time.sleep(ADMIN_HOLD_SECONDS)
        db.commit()
        log.append(f"[admin]      released the lock after {time.time()-t0:.3f}s -- sale is RESUMED")
    finally:
        db.close()


def checkout_attempt(name, inv_id, log, start_barrier):
    db = SessionLocal()
    try:
        db.execute(text("BEGIN"))
        start_barrier.wait()  # all checkouts fire at (roughly) the same instant
        t0 = time.time()
        log.append(f"[{name}] requesting row-level FOR UPDATE lock on inventory...")
        db.execute(text("SELECT total_stock, reserved_stock FROM inventory WHERE id = :id FOR UPDATE"), {"id": inv_id})
        waited = time.time() - t0
        log.append(f"[{name}] lock GRANTED after waiting {waited:.3f}s -- was blocked by the admin's table lock")
        db.rollback()  # just demonstrating the block, not actually consuming stock
    finally:
        db.close()


def print_pg_locks():
    db = SessionLocal()
    rows = db.execute(text("""
        SELECT pl.locktype, pl.mode, pl.granted, pc.relname, pl.pid,
               COALESCE(psa.state, '') AS state
        FROM pg_locks pl
        JOIN pg_class pc ON pc.oid = pl.relation
        LEFT JOIN pg_stat_activity psa ON psa.pid = pl.pid
        WHERE pc.relname = 'inventory'
        ORDER BY pl.granted DESC, pl.pid
    """)).fetchall()
    db.close()

    print("\n  pg_locks JOIN pg_class, filtered to the inventory table, taken WHILE checkouts are blocked:")
    print(f"  {'locktype':10} {'mode':20} {'granted':8} {'relname':10} {'pid':8} state")
    print("  " + "-" * 70)
    for r in rows:
        print(f"  {r.locktype:10} {r.mode:20} {str(r.granted):8} {r.relname:10} {r.pid:<8} {r.state}")
    if not any(not r.granted for r in rows):
        print("  (note: if every row above shows granted=True, the checkouts hadn't reached")
        print("   their lock request yet when this snapshot was taken -- re-run the demo.)")
    return rows


def print_lock_mapping():
    print("\n  Postgres doesn't literally have locks named IS/IX/S/SIX/X -- but its table lock")
    print("  modes map directly onto that textbook multi-granularity hierarchy:")
    print(f"\n  {'Textbook':28} {'Meaning':47} Closest Postgres mode(s)")
    print("  " + "-" * 100)
    for name, meaning, pg in LOCK_MODE_MAP:
        print(f"  {name:28} {meaning:47} {pg}")
    print("\n  The key mechanism: SELECT ... FOR UPDATE doesn't jump straight to a row lock --")
    print("  it first takes an implicit ROW SHARE (~IS) lock at the TABLE level, announcing")
    print("  'something below me wants a lock'. A coarse-grained request (like the admin's")
    print("  ACCESS EXCLUSIVE / X) only has to check that ONE table-level lock to know the")
    print("  whole subtree is contested -- it never has to scan every row to find out.")


def run():
    inv_id = get_inventory_id()

    print("=" * 78)
    print("Section 8.5 -- Multi-Granularity Locking")
    print("=" * 78)
    print(f"Admin fires a table-level ACCESS EXCLUSIVE lock ('pause the sale') while")
    print(f"{N_CHECKOUTS} ordinary checkouts try to take row-level FOR UPDATE locks.\n")

    log = []
    start_barrier = threading.Barrier(N_CHECKOUTS + 1)

    admin_thread = threading.Thread(target=admin_pause_sale, args=(log,))
    admin_thread.start()

    # Give the admin a moment to actually acquire the table lock before checkouts pile up.
    time.sleep(0.3)

    checkout_threads = []
    for i in range(N_CHECKOUTS):
        t = threading.Thread(target=checkout_attempt, args=(f"checkout-{i}", inv_id, log, start_barrier))
        t.start()
        checkout_threads.append(t)
    start_barrier.wait()  # release all checkouts at (roughly) the same instant

    # Give the checkouts a moment to actually reach the blocked state before we inspect pg_locks.
    time.sleep(0.3)
    print("While the admin lock is held, here's what Postgres itself reports as blocked:")
    print_pg_locks()

    admin_thread.join()
    for t in checkout_threads:
        t.join()

    print("\nEvent log (chronological):")
    for line in log:
        print(f"  {line}")

    print_lock_mapping()


if __name__ == "__main__":
    run()
