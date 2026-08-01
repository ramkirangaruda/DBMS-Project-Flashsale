"""
Section 8.4 -- Deadlock Handling

Scenario: a checkout transaction (T1) locks the Inventory row then wants to
lock the User row (to award loyalty points). Simultaneously, an admin
"bulk loyalty adjustment" transaction (T2) locks the User row first, then
wants to lock the Inventory row. Opposite lock order -> classic deadlock.

Part A shows the deadlock actually happening (relies on PostgreSQL's own
deadlock detector as the safety net, since without any detection this would
hang forever).

Part B shows an application-level wound-wait scheme: each transaction is
given a timestamp; an "older" transaction is never made to wait for a
"younger" one -- the younger one is aborted (wounded) instead. This avoids
the deadlock happening at all, rather than detecting it after the fact.

Run with: venv/bin/python -m app.demos.demo_6_deadlock
"""
import sys, os, time, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from app.database import SessionLocal
from scripts.seed_ids import SALE_ID, USER_IDS


def get_inventory_id():
    db = SessionLocal()
    row = db.execute(text("SELECT id FROM inventory WHERE sale_id = :sid"), {"sid": SALE_ID}).fetchone()
    db.close()
    return row[0]


# ---------------------- Part A: cause the deadlock (PostgreSQL detects it) ----------------------

def txn1_inventory_then_user(inv_id, user_id, log):
    db = SessionLocal()
    try:
        db.execute(text("BEGIN"))
        db.execute(text("SELECT * FROM inventory WHERE id = :id FOR UPDATE"), {"id": inv_id})
        log.append("T1: locked Inventory")
        time.sleep(0.3)  # hold the lock long enough for T2 to grab the User row
        log.append("T1: requesting User lock...")
        db.execute(text("SELECT * FROM users WHERE id = :id FOR UPDATE"), {"id": user_id})
        log.append("T1: got User lock (should not normally reach here in the deadlock case)")
        db.commit()
    except OperationalError as e:
        db.rollback()
        log.append(f"T1: ABORTED by PostgreSQL deadlock detector: {str(e).splitlines()[0]}")
    finally:
        db.close()


def txn2_user_then_inventory(inv_id, user_id, log):
    db = SessionLocal()
    try:
        db.execute(text("BEGIN"))
        db.execute(text("SELECT * FROM users WHERE id = :id FOR UPDATE"), {"id": user_id})
        log.append("T2: locked User")
        time.sleep(0.3)
        log.append("T2: requesting Inventory lock...")
        db.execute(text("SELECT * FROM inventory WHERE id = :id FOR UPDATE"), {"id": inv_id})
        log.append("T2: got Inventory lock (should not normally reach here in the deadlock case)")
        db.commit()
    except OperationalError as e:
        db.rollback()
        log.append(f"T2: ABORTED by PostgreSQL deadlock detector: {str(e).splitlines()[0]}")
    finally:
        db.close()


def part_a():
    print("=" * 70)
    print("PART A: Deliberately causing a deadlock (opposite lock order)")
    print("=" * 70)
    inv_id = get_inventory_id()
    user_id = USER_IDS[0]
    log = []
    t1 = threading.Thread(target=txn1_inventory_then_user, args=(inv_id, user_id, log))
    t2 = threading.Thread(target=txn2_user_then_inventory, args=(inv_id, user_id, log))
    t1.start(); t2.start()
    t1.join(); t2.join()
    for line in log:
        print(f"  {line}")
    print("\nPostgreSQL's built-in deadlock detector found the wait-for cycle")
    print("(T1 waits for T2, T2 waits for T1) and aborted one side automatically.")
    print("This is the 'detect after the fact' approach.\n")


# ---------------------- Part B: wound-wait prevents it up front ----------------------

class WoundWaitLock:
    """
    Minimal in-process simulation of wound-wait for teaching purposes:
    each transaction has a timestamp (lower = older = higher priority).
    If an OLDER transaction wants a lock held by a YOUNGER one, the younger
    one is wounded (aborted) immediately, so the older one never waits.
    If a YOUNGER transaction wants a lock held by an OLDER one, it waits.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._holder = {}  # resource_name -> (timestamp, thread_name)

    def acquire(self, resource, timestamp, name, wounded_flags):
        while True:
            with self._lock:
                holder = self._holder.get(resource)
                if holder is None:
                    self._holder[resource] = (timestamp, name)
                    return True
                holder_ts, holder_name = holder
                if timestamp < holder_ts:
                    # We are older than the holder -- wound the holder.
                    wounded_flags[holder_name] = True
                    self._holder[resource] = (timestamp, name)
                    return True
                # We are younger -- wait (simulate by retrying after a short sleep,
                # but bail out if we've been wounded ourselves).
            if wounded_flags.get(name):
                return False
            time.sleep(0.02)

    def release(self, resource, name):
        with self._lock:
            holder = self._holder.get(resource)
            if holder and holder[1] == name:
                del self._holder[resource]


def wound_wait_worker(name, timestamp, resources_in_order, mgr: WoundWaitLock, wounded_flags, log):
    acquired = []
    for r in resources_in_order:
        if wounded_flags.get(name):
            log.append(f"{name}: wounded before acquiring {r}, aborting and releasing held locks")
            for held in acquired:
                mgr.release(held, name)
            return
        ok = mgr.acquire(r, timestamp, name, wounded_flags)
        if not ok:
            log.append(f"{name}: wounded while waiting for {r}, aborting")
            for held in acquired:
                mgr.release(held, name)
            return
        log.append(f"{name}: acquired {r}")
        acquired.append(r)
        time.sleep(0.1)
    log.append(f"{name}: completed successfully, holding {acquired}")
    for r in acquired:
        mgr.release(r, name)


def part_b():
    print("=" * 70)
    print("PART B: Wound-wait prevention (application-level, no DB deadlock needed)")
    print("=" * 70)
    mgr = WoundWaitLock()
    wounded_flags = {}
    log = []
    # T1 is OLDER (lower timestamp = higher priority), wants Inventory then User.
    # T2 is YOUNGER, wants User then Inventory -- opposite order, same as Part A.
    t1 = threading.Thread(target=wound_wait_worker, args=("T1(older)", 1, ["Inventory", "User"], mgr, wounded_flags, log))
    t2 = threading.Thread(target=wound_wait_worker, args=("T2(younger)", 2, ["User", "Inventory"], mgr, wounded_flags, log))
    t1.start(); t2.start()
    t1.join(); t2.join()
    for line in log:
        print(f"  {line}")
    print("\nBecause T1 is older, T2 was wounded the moment their lock requests")
    print("conflicted -- no cycle ever formed, so there was nothing to detect.")
    print("This is the 'prevent before it happens' approach.\n")


def run():
    part_a()
    part_b()


if __name__ == "__main__":
    run()
