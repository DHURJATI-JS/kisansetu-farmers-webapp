"""Notifications: in-app (DB + Socket.IO after commit) and outbound email / SMS / WhatsApp (queued).

Outbound messages are rendered in the recipient's language. In TEST_MODE (or when a channel isn't configured)
messages are stored as 'simulated' and visible to the superadmin in the Outbox.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import SessionLocal
from app.i18n import t
from app.security.sanitize import clean_text
from app.models import Farm, FarmMember, Notification, OutboundMessage, User
from app.realtime import queue_emit
from app.services.http import http
from app.utils import now_utc, to_jsonable

log = logging.getLogger("kisansetu.notify")


def _params(raw: dict[str, Any] | None, lang: str) -> dict[str, Any]:
    """`{"scheme__key": "scheme.pm_kisan.name"}` → `{"scheme": <name in the reader's language>}`."""
    out = {k: v for k, v in (raw or {}).items() if not k.endswith("__key")}
    for k, v in (raw or {}).items():
        if k.endswith("__key") and isinstance(v, str):
            out[k[:-5]] = t(v, lang)
    return out


def render_notification(n: Notification, lang: str) -> dict[str, Any]:
    params = _params(n.params, lang)
    title = t(n.title_key, lang, **params) if n.title_key else (n.title or "")
    body = t(n.body_key, lang, **params) if n.body_key else (n.body or "")
    return {
        "id": str(n.id), "kind": n.kind, "title": title, "body": body, "link": n.link,
        "severity": n.severity, "read": n.read_at is not None,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "raw": n.title_key is None,  # raw (AI/user) text → client may translate on the fly
    }


async def notify(
    db: AsyncSession,
    user_id: uuid.UUID,
    kind: str,
    *,
    title_key: str | None = None,
    body_key: str | None = None,
    params: dict[str, Any] | None = None,
    title: str | None = None,
    body: str | None = None,
    link: str | None = None,
    severity: str = "info",
    channels: Iterable[str] = (),
    dedupe_key: str | None = None,
    attachment_path: str | None = None,
    media_url: str | None = None,
    public_link: str | None = None,
) -> bool:
    """Create an in-app notification (+ optional outbound channels). Returns False when deduplicated."""
    params = to_jsonable(params or {})
    values = dict(
        id=uuid.uuid4(), user_id=user_id, kind=kind, title_key=title_key, body_key=body_key, params=params,
        title=title, body=body, link=link, severity=severity, dedupe_key=dedupe_key,
    )
    stmt = pg_insert(Notification).values(**values)
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "dedupe_key"], index_where=text("dedupe_key IS NOT NULL"))
    res = await db.execute(stmt.returning(Notification.id))
    if res.scalar_one_or_none() is None:
        return False

    user = await db.get(User, user_id)
    lang = user.language if user else "en"
    n = Notification(**values)
    payload = render_notification(n, lang)
    payload["created_at"] = now_utc().isoformat()
    queue_emit(db, f"user:{user_id}", "notify", payload)

    if user and user.status != "deleted":
        text_title = payload["title"]
        text_body = payload["body"]
        link_full = f"{settings.BASE_URL.rstrip('/')}{link}" if link else ""
        for ch in channels:
            if ch == "email" and user.email:
                db.add(OutboundMessage(user_id=user.id, channel="email", to=user.email, subject=f"{settings.APP_NAME}: {text_title}",
                                       body=f"{text_body}\n\n{link_full}".strip(), attachment_path=attachment_path))
            elif ch in ("sms", "whatsapp") and user.phone and user.phone.startswith("+"):
                # phones cannot sign in from a text message: a signed, expiring public link when one is given
                db.add(OutboundMessage(user_id=user.id, channel=ch, to=user.phone, subject=None,
                                       media_url=media_url if ch == "whatsapp" else None,
                                       body=f"{settings.APP_NAME}: {text_title}\n{text_body}\n{public_link or link_full}".strip()[:1500]))
    return True


async def notify_many(
    db: AsyncSession,
    user_ids: Iterable[uuid.UUID],
    kind: str,
    *,
    title_key: str | None = None,
    body_key: str | None = None,
    params: dict[str, Any] | None = None,
    title: str | None = None,
    body: str | None = None,
    link: str | None = None,
    severity: str = "info",
    channels: Iterable[str] = (),
    dedupe_key: str | None = None,
) -> int:
    """Same notification to many users: ONE multi-row insert + ONE user query (no per-user round trips)."""
    ids = list(dict.fromkeys(u for u in user_ids if u))
    if not ids:
        return 0
    params = to_jsonable(params or {})
    rows = [dict(id=uuid.uuid4(), user_id=u, kind=kind, title_key=title_key, body_key=body_key, params=params, title=title,
                 body=body, link=link, severity=severity, dedupe_key=dedupe_key) for u in ids]
    stmt = pg_insert(Notification).values(rows)
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "dedupe_key"], index_where=text("dedupe_key IS NOT NULL"))
    inserted = set((await db.execute(stmt.returning(Notification.user_id))).scalars())
    if not inserted:
        return 0
    users = (await db.execute(select(User).where(User.id.in_(list(inserted))))).scalars().all()
    channels = tuple(channels)
    link_full = f"{settings.BASE_URL.rstrip('/')}{link}" if link else ""
    for u in users:
        n = Notification(**next(r for r in rows if r["user_id"] == u.id))
        payload = render_notification(n, u.language or "en")
        payload["created_at"] = now_utc().isoformat()
        queue_emit(db, f"user:{u.id}", "notify", payload)
        if u.status == "deleted":
            continue
        for ch in channels:
            if ch == "email" and u.email:
                db.add(OutboundMessage(user_id=u.id, channel="email", to=u.email, subject=f"{settings.APP_NAME}: {payload['title']}",
                                       body=f"{payload['body']}\n\n{link_full}".strip()))
            elif ch in ("sms", "whatsapp") and u.phone and u.phone.startswith("+"):
                db.add(OutboundMessage(user_id=u.id, channel=ch, to=u.phone,
                                       body=f"{settings.APP_NAME}: {payload['title']}\n{payload['body']}\n{link_full}".strip()[:1500]))
    return len(inserted)


async def admin_ids(db: AsyncSession) -> list[uuid.UUID]:
    return list((await db.execute(select(User.id).where(User.role == "superadmin", User.status == "active"))).scalars())


async def notify_admins(db: AsyncSession, kind: str, **kw: Any) -> int:
    kw.setdefault("channels", ("email",))
    return await notify_many(db, await admin_ids(db), kind, **kw)


async def farm_leader_ids(db: AsyncSession, farm_id: uuid.UUID, roles: Iterable[str] = ("co_owner", "manager")) -> list[uuid.UUID]:
    farm = await db.get(Farm, farm_id)
    ids = [farm.owner_id] if farm else []
    from app.services.perms import holds_role

    rows = await db.execute(select(FarmMember.user_id).where(   # a leader being let go no longer gets the farm's alerts
        FarmMember.farm_id == farm_id, holds_role(), FarmMember.role.in_(list(roles))))
    ids = list(dict.fromkeys([*ids, *rows.scalars()]))
    if not ids:
        return []
    active = set((await db.execute(select(User.id).where(User.id.in_(ids), User.status == "active"))).scalars())
    return [i for i in ids if i in active]      # banned / deleted leaders get no farm alerts (orders, pay, applications)


async def notify_farm_leaders(db: AsyncSession, farm_id: uuid.UUID, kind: str, roles: Iterable[str] = ("co_owner", "manager"), **kw: Any) -> int:
    return await notify_many(db, await farm_leader_ids(db, farm_id, roles), kind, **kw)


# ------------------------------------------------------------------ outbound delivery
async def _send_email(msg: OutboundMessage) -> None:
    import aiosmtplib

    em = EmailMessage()
    em["From"] = settings.SMTP_FROM
    em["To"] = msg.to
    em["Subject"] = clean_text(msg.subject or settings.APP_NAME, single_line=True, max_len=200)
    em.set_content(clean_text(msg.body))
    if msg.attachment_path and Path(msg.attachment_path).exists():
        data = Path(msg.attachment_path).read_bytes()
        ctype = mimetypes.guess_type(msg.attachment_path)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        em.add_attachment(data, maintype=maintype, subtype=subtype, filename=Path(msg.attachment_path).name)
    await aiosmtplib.send(em, hostname=settings.SMTP_HOST, port=settings.SMTP_PORT, username=settings.SMTP_USER or None,
                          password=settings.SMTP_PASSWORD or None, start_tls=settings.SMTP_STARTTLS, timeout=30)


async def _send_twilio(msg: OutboundMessage) -> None:
    sender = settings.TWILIO_WHATSAPP_FROM if msg.channel == "whatsapp" else settings.TWILIO_SMS_FROM
    to = f"whatsapp:{msg.to}" if msg.channel == "whatsapp" else msg.to
    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json"
    data = {"From": sender, "To": to, "Body": msg.body}
    if msg.channel == "whatsapp" and msg.media_url:
        data["MediaUrl"] = msg.media_url          # WhatsApp shows the picture itself (e.g. the payment screenshot)
    r = await http().post(url, data=data, auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN))
    if r.status_code >= 300:
        raise RuntimeError(f"twilio {r.status_code}: {r.text[:200]}")


def _channel_configured(channel: str) -> bool:
    if channel == "email":
        return bool(settings.SMTP_HOST)
    if channel == "sms":
        return bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_SMS_FROM)
    if channel == "whatsapp":
        return bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_WHATSAPP_FROM)
    return False


async def deliver_outbound_batch(limit: int = 20) -> int:
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(OutboundMessage).where(OutboundMessage.status == "pending").order_by(OutboundMessage.created_at)
            .limit(limit).with_for_update(skip_locked=True)
        )).scalars().all()
        for msg in rows:
            msg.attempts += 1
            if settings.TEST_MODE or not _channel_configured(msg.channel):
                msg.status = "simulated"
                msg.sent_at = now_utc()
                log.info("[simulated %s → %s] %s", msg.channel, msg.to, (msg.subject or msg.body)[:120])
                continue
            try:
                if msg.channel == "email":
                    await _send_email(msg)
                else:
                    await _send_twilio(msg)
                msg.status = "sent"
                msg.sent_at = now_utc()
            except Exception as exc:
                msg.error = str(exc)[:500]
                msg.status = "failed" if msg.attempts >= 3 else "pending"
        await db.commit()
        return len(rows)


async def outbound_worker(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            n = await deliver_outbound_batch()
        except Exception as exc:
            log.warning("outbound worker error: %s", exc)
            n = 0
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0 if n else 4.0)
        except asyncio.TimeoutError:
            pass
