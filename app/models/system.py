"""Platform plumbing: notifications, outbound messages, settings, security, audit, AI memory, uploads."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, Date, DateTime, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base
from app.models.base import Created, fk_uuid, pk, ts


class Upload(Created, Base):
    __tablename__ = "uploads"

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    path: Mapped[str] = mapped_column(String(300))
    mime: Mapped[str] = mapped_column(String(48))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict | None] = mapped_column(JSONB)
    used_for: Mapped[str | None] = mapped_column(String(64))


class Notification(Created, Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    kind: Mapped[str] = mapped_column(String(48), index=True)
    title_key: Mapped[str | None] = mapped_column(String(80))
    body_key: Mapped[str | None] = mapped_column(String(80))
    params: Mapped[dict | None] = mapped_column(JSONB)
    title: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(300))
    severity: Mapped[str] = mapped_column(String(12), default="info")
    dedupe_key: Mapped[str | None] = mapped_column(String(160))
    read_at: Mapped[datetime | None] = ts(index=True)

    __table_args__ = (
        Index("uq_notification_dedupe", "user_id", "dedupe_key", unique=True,
              postgresql_where=text("dedupe_key IS NOT NULL")),
    )


class OutboundMessage(Created, Base):
    __tablename__ = "outbound_messages"

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    channel: Mapped[str] = mapped_column(String(12))  # email|sms|whatsapp
    to: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    attachment_path: Mapped[str | None] = mapped_column(String(300))
    media_url: Mapped[str | None] = mapped_column(String(500))      # WhatsApp media (e.g. the salary payment screenshot)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = ts()


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | int | float | str | bool | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime | None] = ts()
    updated_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)


class MinimumWage(Base):
    __tablename__ = "minimum_wages"

    id: Mapped[uuid.UUID] = pk()
    state: Mapped[str] = mapped_column(String(64), unique=True)
    daily_wage: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    effective_from: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(300))
    updated_at: Mapped[datetime | None] = ts()


class IpBan(Created, Base):
    __tablename__ = "ip_bans"

    id: Mapped[uuid.UUID] = pk()
    ip: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16))  # ai|admin|auto
    expires_at: Mapped[datetime | None] = ts(index=True)
    created_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    lifted_at: Mapped[datetime | None] = ts()
    level: Mapped[int] = mapped_column(Integer, default=0, server_default="0")       # step on the escalation ladder
    shared: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")  # many accounts behind this IP
    user_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)  # account involved, if any
    evidence: Mapped[dict | None] = mapped_column(JSONB)     # strike points, recent offences, the AI's note
    lifted_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    lift_note: Mapped[str | None] = mapped_column(String(300))


class UserSanction(Created, Base):
    """Temporary posting ban of an ACCOUNT for spam (it keeps working, being paid and can always report problems)."""

    __tablename__ = "user_sanctions"

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    kind: Mapped[str] = mapped_column(String(16), default="posting_ban")
    reason: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16))  # ai|auto|admin
    level: Mapped[int] = mapped_column(Integer, default=0)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime | None] = ts(index=True)
    created_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    lifted_at: Mapped[datetime | None] = ts()
    lifted_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)
    lift_note: Mapped[str | None] = mapped_column(String(300))


class AbuseEvent(Created, Base):
    __tablename__ = "abuse_events"

    id: Mapped[uuid.UUID] = pk()
    ip: Mapped[str | None] = mapped_column(String(64), index=True)
    user_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(12), default="low")
    details: Mapped[dict | None] = mapped_column(JSONB)


class AuditLog(Created, Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = pk()
    actor_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(String(64))


class PendingAction(Created, Base):
    """Every change is prepared first (summary shown) and executed only after the user presses GO."""

    __tablename__ = "pending_actions"

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    action_type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSONB)
    summary: Mapped[list | None] = mapped_column(JSONB)
    warnings: Mapped[list | None] = mapped_column(JSONB)
    farm_id: Mapped[uuid.UUID | None] = fk_uuid("farms.id", nullable=True, index=False)
    source: Mapped[str] = mapped_column(String(8), default="ui")
    status: Mapped[str] = mapped_column(String(16), default="prepared", index=True)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = ts(nullable=False)


class AIMessage(Created, Base):
    __tablename__ = "ai_messages"

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    session_key: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(12))
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)


class KnowledgeChunk(Created, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        Index("ix_knowledge_embedding", "embedding", postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"embedding": "vector_cosine_ops"}),
    )

    id: Mapped[uuid.UUID] = pk()
    source: Mapped[str] = mapped_column(String(24), index=True)
    ref_id: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(Text)
    embedding = mapped_column(Vector(settings.EMBEDDING_DIM))


class TranslationCache(Created, Base):
    __tablename__ = "translation_cache"
    __table_args__ = (UniqueConstraint("lang", "source_hash", name="uq_translation"),)

    id: Mapped[uuid.UUID] = pk()
    lang: Mapped[str] = mapped_column(String(8))
    source_hash: Mapped[str] = mapped_column(String(64))
    source_text: Mapped[str] = mapped_column(Text)
    translated: Mapped[str] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(48))


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[uuid.UUID] = pk()
    job: Mapped[str] = mapped_column(String(48), index=True)
    started_at: Mapped[datetime] = ts(nullable=False)
    finished_at: Mapped[datetime | None] = ts()
    ok: Mapped[bool | None] = mapped_column(Boolean)
    details: Mapped[dict | None] = mapped_column(JSONB)


class GovScheme(Base):
    """Government welfare schemes — editable by the superadmin (added, updated, withdrawn, restored) instead of
    being hard-coded, because real schemes change: new ones launch, rules are revised, portals move, some are merged."""

    __tablename__ = "gov_schemes"

    id: Mapped[uuid.UUID] = pk()
    slug: Mapped[str] = mapped_column(String(48), unique=True)
    name: Mapped[str] = mapped_column(String(160))           # English source text
    description: Mapped[str] = mapped_column(Text)            # English source text
    url: Mapped[str] = mapped_column(String(500))             # official portal (allow-listed government domain)
    audience: Mapped[list] = mapped_column(JSONB)             # ["farmer", "owner"]
    rules: Mapped[dict] = mapped_column(JSONB)                # validated eligibility rules (see services.schemes.Rules)
    level: Mapped[str] = mapped_column(String(12), default="central")   # central|state
    states: Mapped[list] = mapped_column(JSONB, default=list)            # empty → all India
    status: Mapped[str] = mapped_column(String(12), default="active", index=True)  # active|archived
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    i18n_managed: Mapped[bool] = mapped_column(Boolean, default=False)  # unedited built-in → catalog translations
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_ref: Mapped[str | None] = mapped_column(String(500))          # guideline / gazette link backing the rules
    change_note: Mapped[str | None] = mapped_column(Text)
    review_due_on: Mapped[date | None] = mapped_column(Date, index=True)
    last_reviewed_at: Mapped[datetime | None] = ts()
    link_status: Mapped[str] = mapped_column(String(16), default="unknown")  # unknown|ok|moved|broken|unreachable
    link_http_status: Mapped[int | None] = mapped_column(Integer)
    link_final_url: Mapped[str | None] = mapped_column(String(500))
    link_checked_at: Mapped[datetime | None] = ts()
    link_failures: Mapped[int] = mapped_column(Integer, default=0)
    link_note: Mapped[str | None] = mapped_column(String(300))
    official_check: Mapped[dict | None] = mapped_column(JSONB)   # what the official page says now (services.scheme_check)
    replaced_by: Mapped[str | None] = mapped_column(String(48))
    archived_at: Mapped[datetime | None] = ts()
    archived_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_by_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True, index=False)


class SchemeSnapshot(Base):
    """Fingerprint of a scheme's official page each time its visible text changed (see services.scheme_check)."""

    __tablename__ = "scheme_snapshots"

    id: Mapped[uuid.UUID] = pk()
    scheme_id: Mapped[uuid.UUID] = fk_uuid("gov_schemes.id", ondelete="CASCADE")
    url: Mapped[str] = mapped_column(String(500))
    final_url: Mapped[str | None] = mapped_column(String(500))
    text_hash: Mapped[str] = mapped_column(String(64))
    chars: Mapped[int] = mapped_column(Integer, default=0)
    sketch: Mapped[list] = mapped_column(JSONB)
    key_sentences: Mapped[list] = mapped_column(JSONB)
    name_found: Mapped[bool] = mapped_column(Boolean, default=False)
    ended_hint: Mapped[str | None] = mapped_column(String(80))
    fetched_at: Mapped[datetime] = ts(nullable=False, index=True)


class SchemeVisit(Base):
    """Who opened a scheme's official portal through the app — so they can be told when it changes or is withdrawn."""

    __tablename__ = "scheme_visits"
    __table_args__ = (UniqueConstraint("user_id", "scheme_id", name="uq_scheme_visit"),)

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id", ondelete="CASCADE")
    scheme_id: Mapped[uuid.UUID] = fk_uuid("gov_schemes.id", ondelete="CASCADE")
    visits: Mapped[int] = mapped_column(Integer, default=1)
    last_visit_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class LoadTestRun(Created, Base):
    """Results of a Locust production test, imported by scripts/run_loadtest.py and charted at /admin/production-test."""

    __tablename__ = "load_test_runs"

    id: Mapped[uuid.UUID] = pk()
    label: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(24))            # bug_hunt|stress
    started_at: Mapped[datetime] = ts(nullable=False)
    finished_at: Mapped[datetime] = ts(nullable=False)
    duration_s: Mapped[int] = mapped_column(Integer)
    peak_users: Mapped[int] = mapped_column(Integer)
    total_requests: Mapped[int] = mapped_column(Integer)
    total_failures: Mapped[int] = mapped_column(Integer)
    passed: Mapped[bool] = mapped_column(Boolean)
    summary: Mapped[dict] = mapped_column(JSONB)
    timeline: Mapped[list] = mapped_column(JSONB)
    endpoints: Mapped[list] = mapped_column(JSONB)
    failures: Mapped[list] = mapped_column(JSONB)
    server: Mapped[list] = mapped_column(JSONB)
    invariants: Mapped[list] = mapped_column(JSONB)
    findings: Mapped[list] = mapped_column(JSONB)
    config: Mapped[dict] = mapped_column(JSONB)
