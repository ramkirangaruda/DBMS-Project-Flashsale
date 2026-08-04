"""
Section 7 -- Transaction Processing & Recovery

Part A: ACID atomicity. A checkout that decrements stock and inserts an
order, wrapped in one transaction. We simulate a mid-transaction crash and
show PostgreSQL's own rollback restores correctness -- versus what happens
without a transaction wrapper (stock silently vanishes).

Part B: a simplified write-ahead log using StockAuditLog, demonstrating the
difference between deferred-update recovery (redo only) and immediate-update
recovery (undo + redo), per Section 7 of the design doc.

Run with: venv/bin/python -m app.demos.demo_7_recovery
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from app.database import SessionLocal
from scripts.seed import DEMO_SALE_ID, DEMO_USER_IDS

# Fixed canonical ids from scripts/seed.py -- stable across reseeds.
SALE_ID = str(DEMO_SALE_ID)
USER_IDS = [str(u) for u in DEMO_USER_IDS]


def get_inventory():
    db = SessionLocal()
    row = db.execute(text("SELECT id, reserved_stock FROM inventory WHERE sale_id = :sid"), {"sid": SALE_ID}).fetchone()
    db.close()
    return row


def part_a():
    print("=" * 70)
    print("PART A: ACID atomicity -- WITHOUT a transaction wrapper (broken)")
    print("=" * 70)
    inv_id, before = get_inventory()
    print(f"reserved_stock before: {before}")

    db = SessionLocal()
    try:
        # Each statement auto-commits individually (no explicit transaction) --
        # simulate a crash between the two statements.
        db.execute(text("UPDATE inventory SET reserved_stock = reserved_stock + 1 WHERE id = :id"), {"id": inv_id})
        db.commit()  # stock decremented and COMMITTED
        print("  -> reserved_stock incremented and committed")
        print("  -> CRASH before the order INSERT runs")
        raise RuntimeError("simulated crash")
    except RuntimeError:
        pass
    finally:
        db.close()

    _, after = get_inventory()
    print(f"reserved_stock after 'crash': {after}  <-- stock is gone, no order exists for it!")
    print("This is exactly the atomicity violation Section 7 warns about.\n")

    # restore
    db = SessionLocal()
    db.execute(text("UPDATE inventory SET reserved_stock = reserved_stock - 1 WHERE id = :id"), {"id": inv_id})
    db.commit()
    db.close()

    print("=" * 70)
    print("PART A continued: WITH a transaction wrapper (correct)")
    print("=" * 70)
    _, before = get_inventory()
    print(f"reserved_stock before: {before}")
    db = SessionLocal()
    try:
        db.execute(text("BEGIN"))
        db.execute(text("UPDATE inventory SET reserved_stock = reserved_stock + 1 WHERE id = :id"), {"id": inv_id})
        print("  -> reserved_stock incremented (NOT yet committed)")
        print("  -> CRASH before the order INSERT runs")
        raise RuntimeError("simulated crash before commit")
    except RuntimeError:
        db.rollback()
        print("  -> ROLLBACK undid the increment automatically")
    finally:
        db.close()
    _, after = get_inventory()
    print(f"reserved_stock after rollback: {after}  <-- correctly restored\n")


def part_b():
    print("=" * 70)
    print("PART B: Write-ahead log (StockAuditLog) -- deferred vs immediate update")
    print("=" * 70)
    inv_id, current = get_inventory()

    db = SessionLocal()
    # Simulate a WAL entry being written BEFORE the actual data change --
    # this is the "write-ahead" part: the log record exists before we know
    # if the transaction will ultimately commit.
    log_id = db.execute(
        text(
            "INSERT INTO stock_audit_log (id, inventory_id, operation, before_value, after_value, timestamp, committed) "
            "VALUES (gen_random_uuid(), :iid, 'reserve', :before, :after, now(), 'false') RETURNING id"
        ),
        {"iid": inv_id, "before": current, "after": current + 1},
    ).fetchone()[0]
    db.commit()
    print(f"WAL entry written (committed='false'): before={current}, after={current+1}")

    print("\n-- Scenario 1: DEFERRED UPDATE --")
    print("   The actual data page was never touched before commit, so if we")
    print("   crash now, there's nothing to undo. On restart, recovery only")
    print("   needs to REDO transactions whose WAL entry says committed='true'")
    print("   but whose data change didn't make it to disk yet.")

    print("\n-- Scenario 2: IMMEDIATE UPDATE --")
    print("   The actual data page IS updated before commit (for performance).")
    print("   If we crash before commit, recovery must UNDO using before_value")
    print("   from the WAL. If we crash after commit but before the data page")
    print("   is flushed, recovery must REDO using after_value.")

    # Simulate the immediate-update crash-then-recover sequence:
    db.execute(text("UPDATE inventory SET reserved_stock = :v WHERE id = :id"), {"v": current + 1, "id": inv_id})
    print(f"\n   Data page updated to {current+1} immediately (before commit)...")
    print("   CRASH -- transaction never committed.")
    # Recovery: since committed='false' in the WAL, UNDO using before_value
    db.rollback()  # in a real system this is what recovery's UNDO pass does
    db.execute(text("UPDATE stock_audit_log SET committed = 'false' WHERE id = :id"), {"id": log_id})
    db.commit()

    db2 = SessionLocal()
    row = db2.execute(text("SELECT before_value, after_value, committed FROM stock_audit_log WHERE id = :id"), {"id": log_id}).fetchone()
    print(f"   Recovery reads WAL: before={row[0]}, after={row[1]}, committed={row[2]}")
    print(f"   Since committed='false' -> UNDO -> restore reserved_stock to {row[0]}")
    db2.execute(text("UPDATE inventory SET reserved_stock = :v WHERE id = :id"), {"v": row[0], "id": inv_id})
    db2.commit()
    _, final = get_inventory()
    print(f"   reserved_stock after recovery: {final}  <-- correctly restored via UNDO")
    db2.close()
    db.close()


def run():
    part_a()
    part_b()


if __name__ == "__main__":
    run()
