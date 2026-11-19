"""Farms, branches, crops, members, insurance, halts, co-owner proposals, calendars."""
from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import Boolean, Date, Float, Index, Integer, Numeric, String, Text, Time, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import Created, Timestamped, fk_uuid, pk, ts

FARMING_TYPES = (
    "intensive", "extensive", "plantation", "organic", "mixed", "horticulture",
    "dryland", "agroforestry", "subsistence", "hydroponic",
)
MEMBER_ROLES = ("co_owner", "manager", "accountant", "supervisor", "employee")
WORKER_ROLES = ("supervisor", "employee")          # farmers bound by 30 km / 8 h rules
# paid work on a farm: ONE working farm per person at a time (notice period included). Owning a farm or co-owning one is
# not work — anyone may own farms and still work on one farm.
JOB_ROLES = ("manager", "accountant", "supervisor", "employee")
INSURANCE_TYPES = ("salary", "health", "crop", "field", "comprehensive", "accident", "equipment", "livestock")


class GstinVerification(Timestamped, Base):
    __tablename__ = "gstin_verifications"
    __table_args__ = (
        Index("uq_gstin_verified", "gstin", unique=True, postgresql_where=text("status = 'verified'")),
    )

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    gstin: Mapped[str] = mapped_column(String(15), index=True)
    legal_name: Mapped[str | None] = mapped_column(String(200))
    trade_name: Mapped[str | None] = mapped_column(String(200))
    state_code: Mapped[str] = mapped_column(String(2))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    method: Mapped[str | None] = mapped_column(String(32))
    ocr: Mapped[dict | None] = mapped_column(JSONB)
    api_response: Mapped[dict | None] = mapped_column(JSONB)
    upload_id: Mapped[uuid.UUID | None] = fk_uuid("uploads.id", nullable=True, index=False)
    verified_at: Mapped[datetime | None] = ts()
    reject_reason: Mapped[str | None] = mapped_column(Text)


class Farm(Timestamped, Base):
    __tablename__ = "farms"

    id: Mapped[uuid.UUID] = pk()
    owner_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    gstin_verification_id: Mapped[uuid.UUID] = fk_uuid("gstin_verifications.id", index=False)
    gstin: Mapped[str] = mapped_column(String(15))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    farming_type: Mapped[str] = mapped_column(String(32), default="mixed")
    status: Mapped[str] = mapped_column(String(20), default="pending_approval", index=True)
    hiring_limit: Mapped[int] = mapped_column(Integer, default=20)
    pay_day: Mapped[int] = mapped_column(Integer, default=5)
    payment_method: Mapped[str] = mapped_column(String(16), default="upi")
    terms_mode: Mapped[str] = mapped_column(String(16), default="boilerplate")
    terms_text: Mapped[str | None] = mapped_column(Text)
    terms_version: Mapped[str] = mapped_column(String(32))
    terms_review: Mapped[dict | None] = mapped_column(JSONB)
    approval_requested_at: Mapped[datetime | None] = ts()
    approved_at: Mapped[datetime | None] = ts()
    approved_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    closure_notice_at: Mapped[datetime | None] = ts()
    closure_date: Mapped[date | None] = mapped_column(Date)
    closed_at: Mapped[datetime | None] = ts()
    banned_at: Mapped[datetime | None] = ts()
    ban_reason: Mapped[str | None] = mapped_column(Text)


class Branch(Timestamped, Base):
    __tablename__ = "branches"

    id: Mapped[uuid.UUID] = pk()
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    name: Mapped[str] = mapped_column(String(120))
    is_main: Mapped[bool] = mapped_column(Boolean, default=False)
    address: Mapped[str | None] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(96))
    district: Mapped[str | None] = mapped_column(String(96))
    state: Mapped[str | None] = mapped_column(String(64))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    area_acres: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    work_start: Mapped[time] = mapped_column(Time, default=time(8, 0))
    work_end: Mapped[time] = mapped_column(Time, default=time(16, 0))
    weekly_offs: Mapped[list] = mapped_column(JSONB, default=lambda: [6])  # python weekday: 0=Mon … 6=Sun
    hiring_open: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active|closing|closed
    closure_notice_at: Mapped[datetime | None] = ts()
    closure_date: Mapped[date | None] = mapped_column(Date)
    closed_at: Mapped[datetime | None] = ts()


class BranchCrop(Timestamped, Base):
    __tablename__ = "branch_crops"

    id: Mapped[uuid.UUID] = pk()
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    branch_id: Mapped[uuid.UUID] = fk_uuid("branches.id")
    crop_key: Mapped[str | None] = mapped_column(String(48))
    name: Mapped[str] = mapped_column(String(80))
    variety: Mapped[str | None] = mapped_column(String(80))
    area_acres: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    planted_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="growing", index=True)  # growing|harvested|removed
    added_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    removed_at: Mapped[datetime | None] = ts()


class FarmMember(Timestamped, Base):
    __tablename__ = "farm_members"
    __table_args__ = (
        Index("uq_member_active", "farm_id", "user_id", unique=True, postgresql_where=text("status <> 'ended'")),
        # two acceptances racing can't give anyone a second working farm, whatever the application code does
        Index("uq_one_working_farm", "user_id", unique=True, postgresql_where=text(
            "status <> 'ended' AND role IN ('manager', 'accountant', 'supervisor', 'employee')")),
    )

    id: Mapped[uuid.UUID] = pk()
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    branch_id: Mapped[uuid.UUID | None] = fk_uuid("branches.id", nullable=True)
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    role: Mapped[str] = mapped_column(String(16))
    salary_type: Mapped[str] = mapped_column(String(8), default="daily")  # daily|monthly
    salary_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    joined_on: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active|notice|ended
    notice_given_at: Mapped[datetime | None] = ts()
    notice_end_date: Mapped[date | None] = mapped_column(Date)
    notice_reason: Mapped[str | None] = mapped_column(Text)
    notice_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    # individual = this person is being let go (leadership powers paused) | closure = the farm/branch is closing
    notice_kind: Mapped[str | None] = mapped_column(String(16))
    ended_on: Mapped[date | None] = mapped_column(Date)
    end_reason: Mapped[str | None] = mapped_column(String(32))
    terms_version: Mapped[str | None] = mapped_column(String(32))
    terms_accepted_at: Mapped[datetime | None] = ts()
    source: Mapped[str] = mapped_column(String(16), default="owner")


class InsurancePolicy(Timestamped, Base):
    __tablename__ = "insurance_policies"

    id: Mapped[uuid.UUID] = pk()
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    branch_id: Mapped[uuid.UUID | None] = fk_uuid("branches.id", nullable=True)
    policy_type: Mapped[str] = mapped_column(String(24))
    insurer: Mapped[str] = mapped_column(String(120))
    policy_number: Mapped[str] = mapped_column(String(64))
    sum_insured: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    covers_salaries: Mapped[bool] = mapped_column(Boolean, default=False)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date] = mapped_column(Date)
    upload_id: Mapped[uuid.UUID | None] = fk_uuid("uploads.id", nullable=True, index=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|verified|rejected|expired
    review: Mapped[dict | None] = mapped_column(JSONB)
    added_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)


class Halt(Created, Base):
    """An active halt blocks hiring / shop / operations on a branch (or whole farm when branch_id is NULL)."""

    __tablename__ = "halts"

    id: Mapped[uuid.UUID] = pk()
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    branch_id: Mapped[uuid.UUID | None] = fk_uuid("branches.id", nullable=True)
    reason: Mapped[str] = mapped_column(String(32), index=True)
    message_key: Mapped[str] = mapped_column(String(80))
    params: Mapped[dict | None] = mapped_column(JSONB)
    details: Mapped[dict | None] = mapped_column(JSONB)  # admin-only
    created_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    lifted_at: Mapped[datetime | None] = ts(index=True)
    lifted_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    lift_note: Mapped[str | None] = mapped_column(Text)


class FarmProposal(Timestamped, Base):
    """Decisions needing unanimous approval of owner + all co-owners."""

    __tablename__ = "farm_proposals"

    id: Mapped[uuid.UUID] = pk()
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    action_type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSONB)
    summary: Mapped[list | None] = mapped_column(JSONB)
    proposed_by_id: Mapped[uuid.UUID] = fk_uuid("users.id", index=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    decided_at: Mapped[datetime | None] = ts()
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)


class ProposalVote(Created, Base):
    __tablename__ = "proposal_votes"
    __table_args__ = (UniqueConstraint("proposal_id", "user_id", name="uq_vote"),)

    id: Mapped[uuid.UUID] = pk()
    proposal_id: Mapped[uuid.UUID] = fk_uuid("farm_proposals.id", ondelete="CASCADE")
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id", index=False)
    vote: Mapped[str] = mapped_column(String(8))


class BranchDay(Base):
    __tablename__ = "branch_days"
    __table_args__ = (UniqueConstraint("branch_id", "day", name="uq_branch_day"),)

    id: Mapped[uuid.UUID] = pk()
    branch_id: Mapped[uuid.UUID] = fk_uuid("branches.id")
    day: Mapped[date] = mapped_column(Date)
    is_holiday: Mapped[bool] = mapped_column(Boolean, default=False)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(16), default="owner")
    marked_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)


class Holiday(Created, Base):
    __tablename__ = "holidays"

    id: Mapped[uuid.UUID] = pk()
    day: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(64))  # NULL = national


class TermsAcceptance(Created, Base):
    __tablename__ = "terms_acceptances"

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    farm_id: Mapped[uuid.UUID | None] = fk_uuid("farms.id", nullable=True)
    version: Mapped[str] = mapped_column(String(32))
    ip: Mapped[str | None] = mapped_column(String(64))


class TermsReview(Created, Base):
    __tablename__ = "terms_reviews"

    id: Mapped[uuid.UUID] = pk()
    text_hash: Mapped[str] = mapped_column(String(64), unique=True)
    verdict: Mapped[str] = mapped_column(String(16))
    issues: Mapped[list | None] = mapped_column(JSONB)
    provider: Mapped[str | None] = mapped_column(String(48))


class WeatherReport(Created, Base):
    __tablename__ = "weather_reports"
    __table_args__ = (UniqueConstraint("branch_id", "report_date", name="uq_weather_day"),)

    id: Mapped[uuid.UUID] = pk()
    branch_id: Mapped[uuid.UUID] = fk_uuid("branches.id")
    farm_id: Mapped[uuid.UUID] = fk_uuid("farms.id")
    report_date: Mapped[date] = mapped_column(Date)
    forecast: Mapped[dict | None] = mapped_column(JSONB)
    alerts: Mapped[list | None] = mapped_column(JSONB)
    crop_warnings: Mapped[list | None] = mapped_column(JSONB)
    calamities: Mapped[list | None] = mapped_column(JSONB)
    insurance_status: Mapped[str | None] = mapped_column(String(32))
    recommendations: Mapped[list | None] = mapped_column(JSONB)
    advisory: Mapped[str | None] = mapped_column(Text)


class CalamityEvent(Created, Base):
    __tablename__ = "calamity_events"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_calamity_src"),)

    id: Mapped[uuid.UUID] = pk()
    source: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(300))
    severity: Mapped[str] = mapped_column(String(16), default="moderate")
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    occurred_at: Mapped[datetime | None] = ts()
    url: Mapped[str | None] = mapped_column(String(500))
