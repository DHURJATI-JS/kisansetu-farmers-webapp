"""Worker welfare: daily wellbeing surveys, grievances, safety cases, owner warnings (always anonymised)."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import Created, fk_uuid, pk, ts

GRIEVANCE_CATEGORIES = (
    "low_salary", "unpaid_salary", "no_water", "no_food", "no_breaks", "overwork",
    "abuse", "harassment", "unsafe", "child_labour", "discrimination", "other",
)


class WellbeingSurvey(Created, Base):
    __tablename__ = "wellbeing_surveys"
    __table_args__ = (UniqueConstraint("member_id", "survey_date", name="uq_survey_day"),)

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    member_id: Mapped[uuid.UUID] = fk_uuid("farm_members.id", index=False)
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    branch_id: Mapped[uuid.UUID | None] = fk_uuid("branches.id", nullable=True)
    survey_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    day_rating: Mapped[str | None] = mapped_column(String(8))
    answers: Mapped[dict | None] = mapped_column(JSONB)
    comment: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = ts()


class Grievance(Created, Base):
    __tablename__ = "grievances"

    id: Mapped[uuid.UUID] = pk()
    reporter_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    member_id: Mapped[uuid.UUID | None] = fk_uuid("farm_members.id", nullable=True, index=False)
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    branch_id: Mapped[uuid.UUID | None] = fk_uuid("branches.id", nullable=True)
    source: Mapped[str] = mapped_column(String(16))  # survey|ai_chat|form
    categories: Mapped[list] = mapped_column(JSONB, default=list)
    description: Mapped[str | None] = mapped_column(Text)
    survey_id: Mapped[uuid.UUID | None] = fk_uuid("wellbeing_surveys.id", nullable=True, index=False)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open|valid|false|resolved
    admin_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    reviewed_at: Mapped[datetime | None] = ts()


class SafetyCase(Created, Base):
    """Opened automatically when multiple workers of a branch report problems → branch halted."""

    __tablename__ = "safety_cases"

    id: Mapped[uuid.UUID] = pk()
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    branch_id: Mapped[uuid.UUID | None] = fk_uuid("branches.id", nullable=True)
    halt_id: Mapped[uuid.UUID | None] = fk_uuid("halts.id", nullable=True, index=False)
    grievance_ids: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    legal_warning_text: Mapped[str | None] = mapped_column(Text)
    compensation_per_worker: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    decided_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    decided_at: Mapped[datetime | None] = ts()


class OwnerWarning(Created, Base):
    """Anonymised warnings for farm owners — delivered in a daily digest to prevent timing-based identification."""

    __tablename__ = "owner_warnings"

    id: Mapped[uuid.UUID] = pk()
    owner_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id", index=False)
    branch_id: Mapped[uuid.UUID | None] = fk_uuid("branches.id", nullable=True, index=False)
    kind: Mapped[str] = mapped_column(String(24))
    message_key: Mapped[str] = mapped_column(String(80))
    params: Mapped[dict | None] = mapped_column(JSONB)
    delivered_at: Mapped[datetime | None] = ts(index=True)
    acknowledged_at: Mapped[datetime | None] = ts()
