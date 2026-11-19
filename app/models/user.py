"""Users, identity documents (encrypted), OTPs, unban appeals, deletions."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, Float, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import Created, Timestamped, fk_uuid, pk, ts

ROLES = ("farmer", "owner", "buyer", "superadmin")


class User(Timestamped, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = pk()
    full_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), index=True)
    language: Mapped[str] = mapped_column(String(8), default="en")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active|banned|deleted
    phone_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    dob: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(String(16))
    photo_path: Mapped[str | None] = mapped_column(String(255))
    photo_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    photo_check: Mapped[dict | None] = mapped_column(JSONB)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    location_verified_at: Mapped[datetime | None] = ts()
    state: Mapped[str | None] = mapped_column(String(64))
    district: Mapped[str | None] = mapped_column(String(96))
    city: Mapped[str | None] = mapped_column(String(96))
    address: Mapped[str | None] = mapped_column(String(300))
    # opt-in: phone / email appear in the community directory only after the person turns this on in the account
    contact_visible: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    skills: Mapped[str | None] = mapped_column(String(300))

    # optional profile used for government-scheme eligibility checks
    landholding_hectares: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    annual_income: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    social_category: Mapped[str | None] = mapped_column(String(16))
    is_income_tax_payer: Mapped[bool | None] = mapped_column(Boolean)
    has_bank_account: Mapped[bool | None] = mapped_column(Boolean)
    is_govt_employee: Mapped[bool | None] = mapped_column(Boolean)
    has_epf_esic_nps: Mapped[bool | None] = mapped_column(Boolean)
    is_rural: Mapped[bool | None] = mapped_column(Boolean)

    # security
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    # one login per account: SHA-256 of the key minted at the latest login; every request's session must match it
    session_key_hash: Mapped[str | None] = mapped_column(String(64))
    session_started_at: Mapped[datetime | None] = ts()
    session_device: Mapped[str | None] = mapped_column(String(120))
    session_ip: Mapped[str | None] = mapped_column(String(64))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = ts()
    last_login_at: Mapped[datetime | None] = ts()
    last_login_ip: Mapped[str | None] = mapped_column(String(64))
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # consent records (DPDP Act 2023)
    terms_version: Mapped[str | None] = mapped_column(String(32))
    terms_accepted_at: Mapped[datetime | None] = ts()
    privacy_accepted_at: Mapped[datetime | None] = ts()

    # moderation / lifecycle
    banned_at: Mapped[datetime | None] = ts()
    ban_reason: Mapped[str | None] = mapped_column(Text)
    posting_banned_until: Mapped[datetime | None] = ts()   # spam: no chat / questions / reviews until then
    false_report_warnings: Mapped[int] = mapped_column(Integer, default=0)
    deleted_at: Mapped[datetime | None] = ts()
    purge_after: Mapped[datetime | None] = ts()
    purged_at: Mapped[datetime | None] = ts()

    @property
    def is_admin(self) -> bool:
        return self.role == "superadmin"

    @property
    def is_banned(self) -> bool:
        return self.status == "banned"

    @property
    def display_name(self) -> str:
        return self.full_name if self.status != "deleted" or not self.purged_at else "Deleted user"


class IdentityDocument(Timestamped, Base):
    """Optional ID proofs. Numbers & files are AES-256-GCM encrypted; only the owner can reveal them."""

    __tablename__ = "identity_documents"
    __table_args__ = (UniqueConstraint("doc_type", "number_blind_index", name="uq_iddoc_blind"),)

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id", ondelete="CASCADE")
    doc_type: Mapped[str] = mapped_column(String(32))
    number_enc: Mapped[str] = mapped_column(Text)
    number_blind_index: Mapped[str] = mapped_column(String(64), index=True)
    number_last4_enc: Mapped[str | None] = mapped_column(Text)
    upload_id: Mapped[uuid.UUID | None] = fk_uuid("uploads.id", nullable=True, ondelete="SET NULL")


class OtpCode(Created, Base):
    __tablename__ = "otp_codes"

    id: Mapped[uuid.UUID] = pk()
    target: Mapped[str] = mapped_column(String(254), index=True)
    purpose: Mapped[str] = mapped_column(String(24))
    code_hash: Mapped[str] = mapped_column(String(128))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = ts(nullable=False)
    consumed_at: Mapped[datetime | None] = ts()


class UnbanRequest(Created, Base):
    __tablename__ = "unban_requests"

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id", ondelete="CASCADE")
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    decided_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    decision_note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = ts()


class AccountDeletion(Created, Base):
    __tablename__ = "account_deletions"

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    purge_after: Mapped[datetime] = ts(nullable=False)
    export_path: Mapped[str | None] = mapped_column(String(300))
    sent_channels: Mapped[list | None] = mapped_column(JSONB)
    purged_at: Mapped[datetime | None] = ts()
