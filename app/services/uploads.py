"""Upload storage: strict validation, metadata-stripping re-encode, private storage outside the web root,
AES-GCM encryption for identity documents, ClamAV scanning of every upload, per-kind access control."""
from __future__ import annotations

import asyncio
import hashlib
import re
import struct
import uuid
import zlib
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import FarmMember, Farm, Upload, User
from app.security.crypto import cipher
from app.security.sanitize import safe_filename
from app.services.images import BadImage, load_image, reencode
from app.utils import now_utc

KINDS = {
    # kind: (public, encrypted, allow_pdf)
    "photo": (True, False, False),
    "product": (True, False, False),
    "insurance": (True, False, True),
    "gst": (False, False, True),
    "payment": (False, False, False),
    "idproof": (False, True, True),
    "plant": (False, False, False),
    "expense": (False, False, True),
    "report": (False, False, False),
}
PDF_MAGIC = b"%PDF-"


class UploadError(ValueError):
    status = 400


def _root() -> Path:
    p = Path(settings.UPLOAD_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


async def _read_limited(file: UploadFile) -> bytes:
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    chunks, total = [], 0
    while True:
        chunk = await file.read(256 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise UploadError("errors.upload_too_large")
        chunks.append(chunk)
    if not total:
        raise UploadError("errors.upload_empty")
    return b"".join(chunks)


class MalwareFound(UploadError):
    status = 422

    def __init__(self, signature: str) -> None:
        super().__init__("errors.upload_malware")
        self.signature = signature


async def clamav_scan(data: bytes) -> tuple[str, str | None]:
    """clamd INSTREAM → ("clean"|"infected"|"error"|"off", signature or reason). Never raises."""
    host = settings.CLAMAV_HOST
    if not host:
        return "off", None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, settings.CLAMAV_PORT), 5)
    except Exception as exc:
        return "error", f"connect {type(exc).__name__}"
    try:
        writer.write(b"zINSTREAM\0")
        for i in range(0, len(data), 65536):
            chunk = data[i: i + 65536]
            writer.write(struct.pack(">I", len(chunk)) + chunk)
            await asyncio.wait_for(writer.drain(), 15)
        writer.write(struct.pack(">I", 0))
        await asyncio.wait_for(writer.drain(), 15)
        resp = await asyncio.wait_for(reader.readuntil(b"\0"), 60)   # z-commands answer NUL-terminated
    except asyncio.IncompleteReadError as exc:
        resp = exc.partial
    except Exception as exc:
        return "error", type(exc).__name__
    finally:
        try:
            writer.close()
        except Exception:
            pass
    text = resp.rstrip(b"\0").decode("utf-8", "replace").strip()
    if text.endswith(" OK") or text == "stream: OK":
        return "clean", None
    if text.endswith(" FOUND"):
        return "infected", text.split(":", 1)[-1][: -len(" FOUND")].strip()[:200]
    return "error", text[:200] or "empty reply"


async def clamav_ping() -> bool:
    if not settings.CLAMAV_HOST:
        return False
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(settings.CLAMAV_HOST, settings.CLAMAV_PORT), 3)
        writer.write(b"zPING\0")
        await writer.drain()
        ok = (await asyncio.wait_for(reader.readuntil(b"\0"), 3)).startswith(b"PONG")
        writer.close()
        return ok
    except Exception:
        return False


async def _report_malware(user: User | None, kind: str, raw: bytes, signature: str, ip: str | None = None) -> None:
    """Recorded in its own transaction (the upload request itself fails) + one admin alert per file."""
    from app.database import SessionLocal
    from app.models import AbuseEvent
    from app.services.notify import notify_admins

    sha = hashlib.sha256(raw).hexdigest()
    async with SessionLocal() as db:
        db.add(AbuseEvent(ip=ip, user_id=user.id if user else None, kind="malware_upload", severity="high",
                          details={"signature": signature, "kind": kind, "sha256": sha, "size": len(raw)}))
        await notify_admins(db, "malware", title_key="notif.malware.title", body_key="notif.malware.body",
                            params={"signature": signature, "kind": kind, "user": user.full_name if user else "?"},
                            link="/admin?tab=security", severity="danger", dedupe_key=f"malware:{sha[:32]}")
        await db.commit()
    from app.services.ai.moderation import abuse_monitor

    # a confirmed signature is 5 strike points (two files → ban); a heuristic guess ("Heuristics.*", "PUA.*") can catch an odd
    # but honest PDF, so it only puts the sender on watch (2 points) — repeated guesses still add up
    heuristic = signature.split(".", 1)[0] in ("Heuristics", "PUA")
    await abuse_monitor.offence(ip=ip, user_id=user.id if user else None, kind="malware_suspect" if heuristic else "malware",
                                weight=2 if heuristic else 5)


# PDF active content: names may be hex-escaped (/J#61vaScript) and hidden inside compressed object streams
_PDF_DENY = {b"javascript", b"js", b"launch", b"embeddedfile", b"embeddedfiles", b"richmedia", b"xfa", b"submitform",
             b"importdata", b"gotoe", b"gotor"}
_PDF_NAME = re.compile(rb"/([^\s/<>\[\]()%{}]+)")
_PDF_HEX = re.compile(rb"#([0-9A-Fa-f]{2})")
_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)


def _pdf_names(buf: bytes) -> set[bytes]:
    return {_PDF_HEX.sub(lambda h: bytes([int(h.group(1), 16)]), m.group(1)).lower() for m in _PDF_NAME.finditer(buf)}


def pdf_active_content(raw: bytes) -> str | None:
    """Name of the first forbidden PDF feature (scripts, launch actions, attachments, forms that submit), else None."""
    hit = _pdf_names(raw) & _PDF_DENY
    if hit:
        return sorted(hit)[0].decode()
    budget = 24 * 1024 * 1024          # total decompressed bytes inspected: decompression bombs cannot hurt
    for m in _PDF_STREAM.finditer(raw):
        if budget <= 0:
            return "too_complex"
        try:
            out = zlib.decompressobj().decompress(m.group(1), min(budget, 8 * 1024 * 1024))
        except zlib.error:
            continue
        budget -= len(out)
        hit = _pdf_names(out) & _PDF_DENY
        if hit:
            return sorted(hit)[0].decode()
    return None


async def store_bytes(db: AsyncSession, user: User | None, kind: str, raw: bytes, *, filename_hint: str = "",
                      ip: str | None = None) -> Upload:
    if kind not in KINDS:
        raise UploadError("errors.upload_kind")
    public, encrypted, allow_pdf = KINDS[kind]
    # 1) antivirus FIRST, on the exact bytes received — nothing is decoded or stored before ClamAV says clean
    verdict, info = await clamav_scan(raw)
    if verdict == "infected":
        await _report_malware(user, kind, raw, info or "?", ip)
        raise MalwareFound(info or "?")
    if verdict == "error" or (verdict == "off" and settings.is_production):
        if settings.CLAMAV_REQUIRED or settings.is_production:
            raise UploadError("errors.upload_scan_failed")
    meta: dict[str, Any] = {"original_name": safe_filename(filename_hint), "antivirus": verdict}
    # 2) type check + sanitising by reconstruction: images are decoded and re-encoded (drops metadata, trailing
    #    payloads and polyglots); PDFs must not contain active content
    if raw.startswith(PDF_MAGIC):
        if not allow_pdf:
            raise UploadError("errors.upload_type")
        if pdf_active_content(raw):
            raise UploadError("errors.upload_pdf_active")
        data, mime, ext = raw, "application/pdf", "pdf"
    else:
        try:
            im, fmt = await asyncio.to_thread(load_image, raw)
            data, mime, ext = await asyncio.to_thread(reencode, im, fmt)
            meta.update({"width": im.size[0], "height": im.size[1]})
        except BadImage as exc:
            raise UploadError("errors.upload_type") from exc
    sha = hashlib.sha256(data).hexdigest()
    uid = uuid.uuid4()
    rel = Path(kind) / f"{now_utc():%Y/%m}" / f"{uid.hex}.{'bin' if encrypted else ext}"
    path = _root() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = cipher.encrypt_bytes(data, aad=f"upload:{uid}") if encrypted else data
    await asyncio.to_thread(path.write_bytes, blob)
    up = Upload(id=uid, user_id=user.id if user else None, kind=kind, path=str(rel).replace("\\", "/"), mime=mime,
                size=len(data), sha256=sha, encrypted=encrypted, meta=meta)
    db.add(up)
    await db.flush()
    return up


async def save_upload(db: AsyncSession, user: User | None, kind: str, file: UploadFile, *, ip: str | None = None) -> Upload:
    raw = await _read_limited(file)
    return await store_bytes(db, user, kind, raw, filename_hint=file.filename or "", ip=ip)


async def read_upload_bytes(up: Upload) -> bytes:
    path = _root() / up.path
    data = await asyncio.to_thread(path.read_bytes)
    return cipher.decrypt_bytes(data, aad=f"upload:{up.id}") if up.encrypted else data


async def can_view(db: AsyncSession, user: User | None, up: Upload) -> bool:
    public = KINDS.get(up.kind, (False,))[0]
    if public:
        return True
    if user is None:
        return False
    if up.kind == "idproof":                   # only the owner — not even admins
        return up.user_id == user.id
    if user.is_admin or up.user_id == user.id:
        return True
    if up.kind == "payment":                   # employee who was paid + farm leaders
        from app.models import SalaryPayment

        sp = (await db.execute(select(SalaryPayment).where(SalaryPayment.upload_id == up.id))).scalar_one_or_none()
        if not sp:
            return False
        if sp.employee_id == user.id:
            return True
        return user.status == "active" and await _is_farm_leader(db, sp.farm_id, user.id)
    if up.kind in ("gst", "expense") and up.used_for:
        try:
            farm_id = uuid.UUID(up.used_for.split(":", 1)[1])
        except Exception:
            return False
        return user.status == "active" and await _is_farm_leader(db, farm_id, user.id)
    return False


async def _is_farm_leader(db: AsyncSession, farm_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    from app.services.perms import holds_role

    farm = await db.get(Farm, farm_id)
    if farm and farm.owner_id == user_id:
        return True
    row = (await db.execute(select(FarmMember.id).where(
        FarmMember.farm_id == farm_id, FarmMember.user_id == user_id, holds_role(),
        FarmMember.role.in_(("co_owner", "manager", "accountant"))))).first()
    return row is not None


async def owned_upload(db: AsyncSession, user: User, upload_id: uuid.UUID | str | None, kind: str, *, unused: bool = False) -> Upload:
    if not upload_id:
        raise UploadError("errors.upload_missing")
    try:
        uid = uuid.UUID(str(upload_id))
    except ValueError as exc:
        raise UploadError("errors.upload_missing") from exc
    up = await db.get(Upload, uid)
    if not up or up.user_id != user.id or up.kind != kind:
        raise UploadError("errors.upload_missing")
    if unused and up.used_for:
        raise UploadError("errors.upload_reused")
    return up
