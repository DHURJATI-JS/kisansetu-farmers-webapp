"""Farm shops: products, timestamped inventory ledger, idempotent orders, reviews, AI-assisted Q&A."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, CheckConstraint, Float, Integer, Numeric, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base
from app.models.base import Created, Timestamped, fk_uuid, pk, ts

PRODUCT_CATEGORIES = ("grains", "pulses", "vegetables", "fruits", "spices", "dairy", "seeds", "plants",
                      "organic", "honey", "oilseeds", "flowers", "fodder", "other")
UNITS = ("kg", "quintal", "tonne", "g", "litre", "dozen", "piece", "bundle", "bag")


class Shop(Timestamped, Base):
    __tablename__ = "shops"

    id: Mapped[uuid.UUID] = pk()
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open")
    courier_base_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=40)
    courier_per_km: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=5)
    delivery_radius_km: Mapped[int] = mapped_column(Integer, default=50)
    accepts_delivery: Mapped[bool] = mapped_column(Boolean, default=True)
    accepts_pickup: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("farm_id", name="uq_shop_farm"),)


class Product(Timestamped, Base):
    __tablename__ = "products"
    __table_args__ = (CheckConstraint("stock >= 0", name="ck_stock_nonneg"),)

    id: Mapped[uuid.UUID] = pk()
    shop_id: Mapped[uuid.UUID] = fk_uuid("shops.id")
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    branch_id: Mapped[uuid.UUID] = fk_uuid("branches.id")
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32), default="other", index=True)
    unit: Mapped[str] = mapped_column(String(16), default="kg")
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    stock: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    min_order_qty: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=1)
    images: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    embedding = mapped_column(Vector(settings.EMBEDDING_DIM), nullable=True)


class InventoryMovement(Created, Base):
    __tablename__ = "inventory_movements"

    id: Mapped[uuid.UUID] = pk()
    product_id: Mapped[uuid.UUID] = fk_uuid("products.id")
    delta: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    stock_after: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    reason: Mapped[str] = mapped_column(String(24))
    order_id: Mapped[uuid.UUID | None] = fk_uuid("orders.id", nullable=True)
    actor_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    client_ts: Mapped[datetime | None] = ts()


class Order(Timestamped, Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("buyer_id", "idempotency_key", name="uq_order_idem"),)

    id: Mapped[uuid.UUID] = pk()
    order_no: Mapped[str] = mapped_column(String(24), unique=True)
    buyer_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    buyer_farm_id: Mapped[uuid.UUID | None] = fk_uuid("farms.id", nullable=True, index=False)
    buyer_name: Mapped[str] = mapped_column(String(120))
    buyer_phone: Mapped[str] = mapped_column(String(48))
    shop_id: Mapped[uuid.UUID] = fk_uuid("shops.id")
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    branch_id: Mapped[uuid.UUID] = fk_uuid("branches.id")
    fulfilment: Mapped[str] = mapped_column(String(16))  # delivery|pickup
    status: Mapped[str] = mapped_column(String(20), default="placed", index=True)
    payment_method: Mapped[str] = mapped_column(String(16))  # cod|pay_at_pickup
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    courier_charge: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    delivery_address: Mapped[str | None] = mapped_column(Text)
    buyer_lat: Mapped[float | None] = mapped_column(Float)
    buyer_lon: Mapped[float | None] = mapped_column(Float)
    distance_km: Mapped[float | None] = mapped_column(Float)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    client_ts: Mapped[datetime | None] = ts()
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    cancelled_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[uuid.UUID] = pk()
    order_id: Mapped[uuid.UUID] = fk_uuid("orders.id", ondelete="CASCADE")
    product_id: Mapped[uuid.UUID] = fk_uuid("products.id")
    product_name: Mapped[str] = mapped_column(String(120))
    unit: Mapped[str] = mapped_column(String(16))
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class OrderEvent(Created, Base):
    __tablename__ = "order_events"

    id: Mapped[uuid.UUID] = pk()
    order_id: Mapped[uuid.UUID] = fk_uuid("orders.id", ondelete="CASCADE")
    status: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    note: Mapped[str | None] = mapped_column(String(300))


class Review(Created, Base):
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("product_id", "user_id", "order_id", name="uq_review_once"),)

    id: Mapped[uuid.UUID] = pk()
    product_id: Mapped[uuid.UUID] = fk_uuid("products.id")
    order_id: Mapped[uuid.UUID] = fk_uuid("orders.id", index=False)
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    author_name: Mapped[str] = mapped_column(String(120))
    rating: Mapped[int] = mapped_column(SmallInteger)
    text: Mapped[str | None] = mapped_column(Text)


class ProductQuestion(Created, Base):
    __tablename__ = "product_questions"

    id: Mapped[uuid.UUID] = pk()
    product_id: Mapped[uuid.UUID] = fk_uuid("products.id")
    asker_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    question: Mapped[str] = mapped_column(Text)
    ai_answer: Mapped[str | None] = mapped_column(Text)
    ai_confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="pending_owner", index=True)
    owner_answer: Mapped[str | None] = mapped_column(Text)
    answered_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    answered_at: Mapped[datetime | None] = ts()
    embedding = mapped_column(Vector(settings.EMBEDDING_DIM), nullable=True)
