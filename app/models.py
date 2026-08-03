"""
SQLAlchemy models for the Flash-Sale Inventory & Oversell Prevention Engine.

Maps directly to Section 4 (ER Model) and Section 5 (Normalization) of the
design document: User, Product, FlashSaleEvent, Inventory, Order, OrderItem,
UserBehaviorLog, FlaggedOrder, StockAuditLog.
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Integer, Numeric, DateTime, ForeignKey, Enum, Text, Index
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


def uuid_pk():
    return Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def now_utc():
    return datetime.now(timezone.utc)


class OrderStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    failed = "failed"
    flagged = "flagged"


class User(Base):
    __tablename__ = "users"

    id = uuid_pk()
    name = Column(String(120), nullable=False)
    email = Column(String(200), nullable=False, unique=True)
    device_fingerprint = Column(String(200))
    created_at = Column(DateTime(timezone=True), default=now_utc)

    orders = relationship("Order", back_populates="user")

    __table_args__ = (
        # Exact-match only ("how many accounts share this device fingerprint"),
        # no range queries needed -- hash index per Section 6.
        Index(
            "ix_users_device_fingerprint_hash",
            "device_fingerprint",
            postgresql_using="hash",
        ),
    )


class Product(Base):
    __tablename__ = "products"

    id = uuid_pk()
    name = Column(String(200), nullable=False)
    category = Column(String(100))
    base_price = Column(Numeric(10, 2), nullable=False)

    sale_events = relationship("FlashSaleEvent", back_populates="product")


class FlashSaleEvent(Base):
    __tablename__ = "flash_sale_events"

    id = uuid_pk()
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=False)
    sale_price = Column(Numeric(10, 2), nullable=False)

    product = relationship("Product", back_populates="sale_events")
    inventory = relationship("Inventory", back_populates="sale", uselist=False)
    orders = relationship("Order", back_populates="sale")


class Inventory(Base):
    """
    One row per flash-sale event. `version` is used by the optimistic
    concurrency control (OCC) demo (Section 8.2). `reserved_stock` is the
    deliberate denormalization discussed in Section 5 -- a live counter
    instead of SUM(quantity) over OrderItem on every read.
    """
    __tablename__ = "inventory"

    id = uuid_pk()
    sale_id = Column(UUID(as_uuid=True), ForeignKey("flash_sale_events.id"), nullable=False, unique=True)
    total_stock = Column(Integer, nullable=False)
    reserved_stock = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=0)  # OCC version column

    sale = relationship("FlashSaleEvent", back_populates="inventory")

    @property
    def available(self) -> int:
        return self.total_stock - self.reserved_stock


class Order(Base):
    __tablename__ = "orders"

    id = uuid_pk()
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    sale_id = Column(UUID(as_uuid=True), ForeignKey("flash_sale_events.id"), nullable=False)
    status = Column(Enum(OrderStatus), default=OrderStatus.pending, nullable=False)
    created_at = Column(DateTime(timezone=True), default=now_utc)

    user = relationship("User", back_populates="orders")
    sale = relationship("FlashSaleEvent", back_populates="orders")
    items = relationship("OrderItem", back_populates="order")
    flag = relationship("FlaggedOrder", back_populates="order", uselist=False)

    __table_args__ = (
        # Composite B+ tree index -- "all orders for this sale in the last
        # N seconds" per Section 6. sale_id leads so equality filters on
        # sale_id can also range-scan created_at within the same index.
        Index("ix_orders_sale_id_created_at", "sale_id", "created_at"),
        # Referencing side of orders.user_id -> users.id. Postgres indexes the
        # REFERENCED key automatically but never the referencing column, so
        # without this a DELETE from users seq-scans orders once per deleted
        # row. See the note in scripts/seed.py.
        Index("ix_orders_user_id", "user_id"),
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id = uuid_pk()
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    price_at_purchase = Column(Numeric(10, 2), nullable=False)

    order = relationship("Order", back_populates="items")

    __table_args__ = (
        # Referencing side of order_items.order_id -> orders.id. This is the
        # one whose absence made `DELETE FROM orders` effectively never finish
        # after seed_large.py -- see the note in scripts/seed.py.
        Index("ix_order_items_order_id", "order_id"),
    )


class UserBehaviorLog(Base):
    """Feeds the bot-detection model -- Section 10.2."""
    __tablename__ = "user_behavior_logs"

    id = uuid_pk()
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    sale_id = Column(UUID(as_uuid=True), ForeignKey("flash_sale_events.id"), nullable=False)
    # Set when the checkout attempt actually produced an order; NULL for a
    # rejected/failed attempt (sold out, version conflict, etc). This is
    # what lets score_sessions() tie a flagged session back to a specific
    # Order without guessing from timestamps -- important once checkouts
    # are concurrent, where "nearest order by time" can pick the wrong one.
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=True)
    page_load_time = Column(DateTime(timezone=True))
    checkout_time = Column(DateTime(timezone=True))
    ip_address = Column(String(64))
    session_duration_ms = Column(Integer)

    __table_args__ = (
        # Both FK columns indexed for the same reason as ix_orders_user_id:
        # this table references orders and users, so leaving order_id/user_id
        # unindexed turns any cleanup DELETE on those parents into a per-row
        # seq scan of this table.
        Index("ix_user_behavior_logs_order_id", "order_id"),
        Index("ix_user_behavior_logs_user_id", "user_id"),
    )


class FlaggedOrder(Base):
    __tablename__ = "flagged_orders"

    id = uuid_pk()
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False, unique=True)
    anomaly_score = Column(Numeric(6, 4))
    flagged_at = Column(DateTime(timezone=True), default=now_utc)
    review_status = Column(String(30), default="pending")

    order = relationship("Order", back_populates="flag")


class StockAuditLog(Base):
    """Simplified write-ahead log used by the recovery demo -- Section 7."""
    __tablename__ = "stock_audit_log"

    id = uuid_pk()
    inventory_id = Column(UUID(as_uuid=True), ForeignKey("inventory.id"), nullable=False)
    operation = Column(String(50), nullable=False)
    before_value = Column(Integer)
    after_value = Column(Integer)
    timestamp = Column(DateTime(timezone=True), default=now_utc)
    committed = Column(String(10), default="false")  # "true"/"false" -- drives redo/undo demo
