"""Salary accruals (per day from joining), payments (OCR-verified), due adjustments and ledgers.

Financial rows are NEVER deleted (even after account deletion) — names are snapshotted.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, Date, Float, Index, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import Created, fk_uuid, pk


class SalaryAccrual(Created, Base):
    __tablename__ = "salary_accruals"
    __table_args__ = (UniqueConstraint("member_id", "work_date", name="uq_accrual_day"),)

    id: Mapped[uuid.UUID] = pk()
    member_id: Mapped[uuid.UUID] = fk_uuid("farm_members.id")
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    branch_id: Mapped[uuid.UUID | None] = fk_uuid("branches.id", nullable=True, index=False)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    base: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    holiday_premium: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    gross: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    tds: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    net: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    is_holiday: Mapped[bool] = mapped_column(Boolean, default=False)


class SalaryPayment(Created, Base):
    __tablename__ = "salary_payments"
    __table_args__ = (
        Index("uq_payment_upi_ref", "upi_ref", unique=True, postgresql_where=text("upi_ref IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = pk()
    member_id: Mapped[uuid.UUID] = fk_uuid("farm_members.id")
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    employee_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    employee_name: Mapped[str] = mapped_column(String(120))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    tds_portion: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    method: Mapped[str] = mapped_column(String(16))
    upload_id: Mapped[uuid.UUID] = fk_uuid("uploads.id", index=False)
    image_hash: Mapped[str | None] = mapped_column(String(32), index=True)
    upi_ref: Mapped[str | None] = mapped_column(String(64))
    ocr: Mapped[dict | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    recorded_by_id: Mapped[uuid.UUID] = fk_uuid("users.id", index=False)
    recorded_by_name: Mapped[str] = mapped_column(String(120))

    __mapper_args__ = {"eager_defaults": True}


class DueAdjustment(Created, Base):
    """Extra money owed to a worker (e.g. admin-ordered compensation)."""

    __tablename__ = "due_adjustments"

    id: Mapped[uuid.UUID] = pk()
    member_id: Mapped[uuid.UUID] = fk_uuid("farm_members.id")
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    reason: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    case_id: Mapped[uuid.UUID | None] = fk_uuid("safety_cases.id", nullable=True, index=False)
    created_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)


class LedgerEntry(Created, Base):
    """Farm ledger — visible to owner, co-owners, managers and accountants."""

    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = pk()
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    branch_id: Mapped[uuid.UUID | None] = fk_uuid("branches.id", nullable=True, index=False)
    entry_type: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(4))  # in|out|info
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    category: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(300))
    counterparty_user_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    counterparty_name: Mapped[str | None] = mapped_column(String(120))
    reference_type: Mapped[str | None] = mapped_column(String(32))
    reference_id: Mapped[str | None] = mapped_column(String(64))
    occurred_on: Mapped[date] = mapped_column(Date, index=True)
    created_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    created_by_name: Mapped[str | None] = mapped_column(String(120))
    upload_id: Mapped[uuid.UUID | None] = fk_uuid("uploads.id", nullable=True, index=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True)


class PersonalLedgerEntry(Created, Base):
    """Personal statement (salary credits, TDS, purchases) — retained after account deletion."""

    __tablename__ = "personal_ledger"

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    entry_type: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(4))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    description: Mapped[str] = mapped_column(String(300))
    farm_id: Mapped[uuid.UUID | None] = fk_uuid("farms.id", nullable=True, index=False)
    farm_name: Mapped[str | None] = mapped_column(String(120))
    reference_type: Mapped[str | None] = mapped_column(String(32))
    reference_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True)
