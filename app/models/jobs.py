"""Hiring: job postings, applications, invitations, externally sourced jobs."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Float, Index, Integer, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import Created, Timestamped, fk_uuid, pk, ts


class JobPosting(Timestamped, Base):
    __tablename__ = "job_postings"

    id: Mapped[uuid.UUID] = pk()
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    branch_id: Mapped[uuid.UUID] = fk_uuid("branches.id")
    title: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(16), default="employee")
    salary_type: Mapped[str] = mapped_column(String(8), default="daily")
    salary_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    positions: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    created_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)


class JobApplication(Timestamped, Base):
    __tablename__ = "job_applications"
    __table_args__ = (
        Index("uq_application_live", "posting_id", "applicant_id", unique=True,
              postgresql_where=text("status IN ('applied','accepted')")),
    )

    id: Mapped[uuid.UUID] = pk()
    posting_id: Mapped[uuid.UUID] = fk_uuid("job_postings.id")
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    branch_id: Mapped[uuid.UUID] = fk_uuid("branches.id")
    applicant_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    status: Mapped[str] = mapped_column(String(16), default="applied", index=True)
    distance_km: Mapped[float | None] = mapped_column(Float)
    location_source: Mapped[str | None] = mapped_column(String(8))   # live | gps (saved) | typed (saved, not GPS)
    terms_version: Mapped[str | None] = mapped_column(String(32))
    terms_accepted_at: Mapped[datetime | None] = ts()
    note: Mapped[str | None] = mapped_column(Text)
    decided_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    decided_at: Mapped[datetime | None] = ts()


class Invitation(Timestamped, Base):
    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = pk()
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    branch_id: Mapped[uuid.UUID | None] = fk_uuid("branches.id", nullable=True)
    phone: Mapped[str] = mapped_column(String(48), index=True)
    invitee_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    role: Mapped[str] = mapped_column(String(16))
    salary_type: Mapped[str] = mapped_column(String(8), default="daily")
    salary_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    invited_by_id: Mapped[uuid.UUID] = fk_uuid("users.id", index=False)
    responded_at: Mapped[datetime | None] = ts()
    expires_at: Mapped[datetime | None] = ts()


class ExternalJob(Created, Base):
    """Jobs found on authentic external sites. Governed by the source site's policies — NOT ours."""

    __tablename__ = "external_jobs"

    id: Mapped[uuid.UUID] = pk()
    title: Mapped[str] = mapped_column(String(200))
    employer: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(96), index=True)
    state: Mapped[str | None] = mapped_column(String(64))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    salary_text: Mapped[str | None] = mapped_column(String(120))
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    source_site: Mapped[str] = mapped_column(String(120))
    source_url: Mapped[str] = mapped_column(String(700), unique=True)
    snippet: Mapped[str | None] = mapped_column(Text)
    posted_text: Mapped[str | None] = mapped_column(String(60))
    fetched_at: Mapped[datetime] = ts(nullable=False)
    expires_at: Mapped[datetime | None] = ts(index=True)
