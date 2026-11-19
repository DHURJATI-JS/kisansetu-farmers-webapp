"""Account deletion (DPDP-style): export → notify → 30-day retention → anonymise.

* Owners can delete only when every farm & branch is closed (which requires all dues cleared & orders settled).
* Workers must resign first; buyers must settle open orders.
* A full export (JSON + CSV) is emailed and a signed download link is sent by SMS / WhatsApp.
* After 30 days personal data is erased; financial records (salary credits, deductions, purchases, farm ledger)
  are retained with name snapshots as required for accounting. Reviews stay visible for 30 days, then removed.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import secrets
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    AccountDeletion, AIMessage, Farm, FarmMember, IdentityDocument, Notification, Order, OutboundMessage,
    PersonalLedgerEntry, Review, SalaryPayment, Upload, User, WellbeingSurvey,
)
from app.security.sanitize import csv_safe
from app.security.sessions import destroy_user_sessions
from app.security.tokens import sign_purpose
from app.services.audit import audit
from app.services.settings_store import get_setting
from app.services.shop import OPEN
from app.utils import now_utc, to_jsonable


async def deletion_blockers(db: AsyncSession, user: User) -> list[str]:
    out = []
    if user.is_admin:
        out.append("errors.delete_admin")
    open_farms = (await db.execute(select(Farm.id).where(Farm.owner_id == user.id, Farm.status.notin_(("closed", "rejected"))))).first()
    if open_farms:
        out.append("errors.delete_close_farms_first")
    active = (await db.execute(select(FarmMember.id).where(FarmMember.user_id == user.id, FarmMember.status.in_(("active", "notice"))))).first()
    if active:
        out.append("errors.delete_resign_first")
    orders = (await db.execute(select(Order.id).where(Order.buyer_id == user.id, Order.status.in_(OPEN)))).first()
    if orders:
        out.append("errors.delete_open_orders")
    return out


def _rows_to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    for r in rows:
        w.writerow({k: csv_safe("" if v is None else v) for k, v in r.items()})   # no spreadsheet formula injection
    return buf.getvalue()


def _obj(o: Any, fields: list[str]) -> dict[str, Any]:
    return {f: to_jsonable(getattr(o, f, None)) for f in fields}


async def build_export(db: AsyncSession, user: User) -> Path:
    profile = _obj(user, ["id", "full_name", "phone", "email", "role", "language", "dob", "gender", "state", "district", "city",
                          "address", "skills", "landholding_hectares", "annual_income", "created_at", "terms_version",
                          "terms_accepted_at", "privacy_accepted_at"])
    statement = [_obj(r, ["created_at", "entry_type", "direction", "amount", "description", "farm_name", "reference_type", "reference_id"])
                 for r in (await db.execute(select(PersonalLedgerEntry).where(PersonalLedgerEntry.user_id == user.id)
                                            .order_by(PersonalLedgerEntry.created_at))).scalars()]
    payments = [_obj(r, ["created_at", "amount", "tds_portion", "method", "upi_ref", "farm_id"])
                for r in (await db.execute(select(SalaryPayment).where(SalaryPayment.employee_id == user.id))).scalars()]
    memberships = [_obj(r, ["farm_id", "branch_id", "role", "salary_type", "salary_amount", "joined_on", "status", "ended_on", "end_reason"])
                   for r in (await db.execute(select(FarmMember).where(FarmMember.user_id == user.id))).scalars()]
    orders = [_obj(r, ["order_no", "created_at", "status", "fulfilment", "subtotal", "courier_charge", "total"])
              for r in (await db.execute(select(Order).where(Order.buyer_id == user.id))).scalars()]
    reviews = [_obj(r, ["created_at", "product_id", "rating", "text"])
               for r in (await db.execute(select(Review).where(Review.user_id == user.id))).scalars()]
    surveys = [_obj(r, ["survey_date", "day_rating", "answers", "comment"])
               for r in (await db.execute(select(WellbeingSurvey).where(WellbeingSurvey.user_id == user.id))).scalars()]
    out_dir = Path(settings.EXPORT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"kisansetu-export-{user.id.hex}-{secrets.token_hex(4)}.zip"

    def _write() -> None:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("profile.json", json.dumps(profile, ensure_ascii=False, indent=2))
            z.writestr("memberships.json", json.dumps(memberships, ensure_ascii=False, indent=2))
            z.writestr("surveys.json", json.dumps(surveys, ensure_ascii=False, indent=2))
            z.writestr("statement.csv", _rows_to_csv(statement))
            z.writestr("salary_payments.csv", _rows_to_csv(payments))
            z.writestr("orders.csv", _rows_to_csv(orders))
            z.writestr("reviews.csv", _rows_to_csv(reviews))
            z.writestr("README.txt", "KisanSetu data export. Financial records are retained by the platform as required by law.\n")

    await asyncio.to_thread(_write)
    return path


async def request_deletion(db: AsyncSession, user: User, ip: str | None) -> dict[str, Any]:
    from app.i18n import t

    path = await build_export(db, user)
    days = int(await get_setting(db, "data_retention_days"))
    purge_after = now_utc() + timedelta(days=days)
    token = sign_purpose("export", {"uid": str(user.id), "file": path.name}, minutes=days * 24 * 60)
    link = f"{settings.BASE_URL.rstrip('/')}/account/export/{token}"
    lang = user.language or "en"
    channels = []
    if user.email:
        db.add(OutboundMessage(user_id=user.id, channel="email", to=user.email, subject=t("deletion.email_subject", lang),
                               body=t("deletion.email_body", lang, days=days, link=link), attachment_path=str(path)))
        channels.append("email")
    for ch in ("sms", "whatsapp"):
        db.add(OutboundMessage(user_id=user.id, channel=ch, to=user.phone, body=t("deletion.sms_body", lang, days=days, link=link)))
        channels.append(ch)
    db.add(AccountDeletion(user_id=user.id, purge_after=purge_after, export_path=str(path), sent_channels=channels))
    user.status, user.deleted_at, user.purge_after = "deleted", now_utc(), purge_after
    user.token_version += 1
    await destroy_user_sessions(user.id, reason="deleted")
    await audit(db, user.id, "account.delete", "user", user.id, {"purge_after": purge_after.isoformat()}, ip)
    return {"purge_after": purge_after, "channels": channels}


async def purge_due_accounts(db: AsyncSession) -> int:
    users = (await db.execute(select(User).where(User.status == "deleted", User.purged_at.is_(None),
                                                 User.purge_after < now_utc()).limit(200))).scalars().all()
    if not users:
        return 0
    ids = [u.id for u in users]
    uploads = (await db.execute(select(Upload).where(Upload.user_id.in_(ids), Upload.kind.in_(("photo", "idproof", "plant"))))).scalars().all()
    root = Path(settings.UPLOAD_DIR)
    for up in uploads:
        try:
            (root / up.path).unlink(missing_ok=True)
        except Exception:
            pass
    await db.execute(delete(IdentityDocument).where(IdentityDocument.user_id.in_(ids)))
    await db.execute(delete(AIMessage).where(AIMessage.user_id.in_(ids)))
    await db.execute(delete(Notification).where(Notification.user_id.in_(ids)))
    await db.execute(delete(Review).where(Review.user_id.in_(ids)))          # reviews remained 30 days → removed now
    await db.execute(update(Upload).where(Upload.id.in_([u.id for u in uploads])).values(path="purged", meta=None))
    exports = (await db.execute(select(AccountDeletion).where(AccountDeletion.user_id.in_(ids)))).scalars().all()
    for ex in exports:
        if ex.export_path:
            Path(ex.export_path).unlink(missing_ok=True)
        ex.purged_at = now_utc()
    for u in users:
        u.full_name, u.phone, u.email = "Deleted user", f"deleted:{u.id.hex}", None
        u.password_hash = "!purged:" + secrets.token_hex(16)
        u.photo_path = u.dob = u.gender = u.address = u.skills = None
        u.lat = u.lon = None
        u.state = u.district = u.city = None
        u.landholding_hectares = u.annual_income = u.social_category = None
        u.totp_secret_enc, u.totp_enabled = None, False
        u.photo_check, u.photo_verified = None, False
        u.purged_at = now_utc()
    return len(users)
