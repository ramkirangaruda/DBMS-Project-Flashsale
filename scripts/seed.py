"""
Seeds the database with:
- A handful of users (some sharing a device fingerprint, to give the
  bot-detection demo something to find)
- One product with a flash sale that has deliberately scarce stock
  (so the oversell / concurrency demos have something to fight over)

Run with: venv/bin/python -m scripts.seed
"""
import sys
import os
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.database import Base, engine, SessionLocal
from app import models


def run():
    print("Creating tables...")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        # Wipe existing demo data (idempotent re-seed for repeated demo runs).
        #
        # TRUNCATE, not per-table DELETE: none of the FK columns pointing at
        # orders/users (order_items.order_id, user_behavior_logs.order_id,
        # user_behavior_logs.user_id, orders.user_id) carry an index, so a
        # `DELETE FROM orders` makes Postgres run a per-row FK check --
        # "SELECT 1 FROM ONLY order_items WHERE $1 = order_id FOR KEY SHARE" --
        # that seq-scans the child table once per deleted row. After
        # scripts/seed_large.py that is 200k orders x a 400k-row child table
        # (whose freshly-deleted rows are still there as un-vacuumed dead
        # tuples), which does not finish in any reasonable time. TRUNCATE
        # drops the underlying files instead, so it is O(1) in row count and
        # skips per-row FK checks entirely.
        db.execute(text(
            "TRUNCATE TABLE "
            "stock_audit_log, flagged_orders, user_behavior_logs, order_items, "
            "orders, inventory, flash_sale_events, products, users "
            "RESTART IDENTITY CASCADE"
        ))
        db.commit()

        print("Seeding users (including a device-fingerprint cluster for bot demo)...")
        shared_fp = str(uuid.uuid4())
        users = []
        for i in range(30):
            fp = shared_fp if i < 6 else str(uuid.uuid4())  # 6 users share one device -> bot signal
            u = models.User(
                name=f"User {i}",
                email=f"user{i}@example.com",
                device_fingerprint=fp,
            )
            db.add(u)
            users.append(u)
        db.commit()

        print("Seeding product + flash sale with scarce stock...")
        product = models.Product(name="Limited Edition Sneaker", category="footwear", base_price=199.00)
        db.add(product)
        db.commit()

        sale = models.FlashSaleEvent(
            product_id=product.id,
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc) + timedelta(minutes=10),
            sale_price=99.00,
        )
        db.add(sale)
        db.commit()

        inventory = models.Inventory(
            sale_id=sale.id,
            total_stock=5,   # deliberately scarce -- this is the contested resource
            reserved_stock=0,
            version=0,
        )
        db.add(inventory)
        db.commit()

        print(f"Seed complete.")
        print(f"  product_id = {product.id}")
        print(f"  sale_id    = {sale.id}")
        print(f"  inventory total_stock = {inventory.total_stock}")
        print(f"  sample user_ids = {[str(u.id) for u in users[:3]]}")

        # Write ids to a small file so demo scripts can pick them up without
        # re-querying / hardcoding.
        with open(os.path.join(os.path.dirname(__file__), "seed_ids.py"), "w") as f:
            f.write(f'PRODUCT_ID = "{product.id}"\n')
            f.write(f'SALE_ID = "{sale.id}"\n')
            f.write(f'USER_IDS = {[str(u.id) for u in users]}\n')

    finally:
        db.close()


if __name__ == "__main__":
    run()
