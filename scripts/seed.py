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

from app.database import Base, engine, SessionLocal
from app import models


def run():
    print("Creating tables...")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        # Wipe existing demo data (idempotent re-seed for repeated demo runs)
        db.query(models.StockAuditLog).delete()
        db.query(models.FlaggedOrder).delete()
        db.query(models.UserBehaviorLog).delete()
        db.query(models.OrderItem).delete()
        db.query(models.Order).delete()
        db.query(models.Inventory).delete()
        db.query(models.FlashSaleEvent).delete()
        db.query(models.Product).delete()
        db.query(models.User).delete()
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
