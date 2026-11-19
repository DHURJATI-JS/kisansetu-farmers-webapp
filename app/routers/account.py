"""Account APIs: photo (face-verified), encrypted ID reveal (owner only), statements, export download, appeals, language."""
from __future__ import annotations

import csv
import io
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import client_ip, current_user, get_db, lang_of, require_active, require_user
from app.errors import AppError, Conflict, NotFound
from app.i18n import is_supported, t
from app.models import AccountDeletion, IdentityDocument, PersonalLedgerEntry, UnbanRequest, User
from app.security.crypto import cipher
from app.security.ratelimit import limiter
from app.security.sanitize import csv_safe
from app.security.sessions import list_user_sessions
from app.security.tokens import verify_purpose
from app.services.audit import audit
from app.services.notify import notify_admins
from app.services.ocr import face_check
from app.services.uploads import UploadError, read_upload_bytes, save_upload

router = APIRouter(tags=["account"])


@router.post("/api/account/photo")
@limiter.limit("6/minute;30/day")
async def upload_photo(request: Request, file: UploadFile = File(...), user: User = Depends(require_active),
                       db: AsyncSession = Depends(get_db)):
    try:
        up = await save_upload(db, user, "photo", file)
    except UploadError as exc:
        raise AppError(str(exc), status=exc.status) from exc
    res = await face_check(await read_upload_bytes(up))
    if not res["ok"]:
        await db.commit()
        raise AppError(f"errors.face_{res.get('reason') or 'no_face'}")
    up.used_for = f"photo:{user.id}"
    user.photo_path, user.photo_verified, user.photo_check = str(up.id), True, res
    await audit(db, user.id, "account.photo", "upload", up.id, {"methods": res.get("methods")}, client_ip(request))
    await db.commit()
    return {"ok": True, "url": f"/media/{up.id}", "message": t("msg.photo_verified", lang_of(request)),
            "unverified": bool(res.get("unverified"))}


@router.get("/api/account/id-docs/{doc_id}/reveal")
@limiter.limit("10/minute")
async def reveal_id(request: Request, doc_id: uuid.UUID, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    doc = await db.get(IdentityDocument, doc_id)
    if not doc or doc.user_id != user.id:  # only the person themselves — never owners or admins
        raise NotFound()
    number = cipher.decrypt_str(doc.number_enc, f"iddoc:{user.id}:{doc.doc_type}")
    await audit(db, user.id, "iddoc.reveal", "iddoc", doc.id, {}, client_ip(request))
    await db.commit()
    return JSONResponse({"number": number}, headers={"Cache-Control": "no-store"})


@router.get("/account/statement.csv")
@limiter.limit("10/minute")
async def statement_csv(request: Request, month: str | None = None, user: User = Depends(require_user),
                        db: AsyncSession = Depends(get_db)):
    from datetime import datetime, time

    from app.services.statements import period
    from app.utils import IST

    stmt = select(PersonalLedgerEntry).where(PersonalLedgerEntry.user_id == user.id).order_by(PersonalLedgerEntry.created_at)
    if month:
        _key, start, end = period(month)
        stmt = stmt.where(PersonalLedgerEntry.created_at >= datetime.combine(start, time.min, IST),
                          PersonalLedgerEntry.created_at < datetime.combine(end, time.max, IST))
    rows = (await db.execute(stmt)).scalars().all()
    lang = lang_of(request)

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([t("csv.date", lang), t("csv.type", lang), t("csv.direction", lang), t("csv.amount", lang),
                    t("csv.description", lang), t("csv.farm", lang), t("csv.reference", lang)])
        yield "﻿" + buf.getvalue()
        for r in rows:
            buf.seek(0)
            buf.truncate()
            w.writerow([r.created_at.astimezone(IST).date().isoformat(), r.entry_type, r.direction, str(r.amount), csv_safe(r.description or ""),
                        csv_safe(r.farm_name or ""), r.reference_id or ""])
            yield buf.getvalue()

    return StreamingResponse(gen(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="kisansetu-statement.csv"'})


@router.get("/account/export/{token}")
@limiter.limit("10/hour")
async def export_download(request: Request, token: str, db: AsyncSession = Depends(get_db)):
    data = verify_purpose("export", token)
    if not data:
        raise NotFound()
    rec = (await db.execute(select(AccountDeletion).where(AccountDeletion.user_id == uuid.UUID(data["uid"])))).scalar_one_or_none()
    if not rec or not rec.export_path or Path(rec.export_path).name != data["file"] or rec.purged_at:
        raise NotFound()
    return FileResponse(rec.export_path, media_type="application/zip", filename="kisansetu-data-export.zip")


class AppealIn(BaseModel):
    message: str = Field(min_length=10, max_length=2000)


@router.post("/api/account/unban-request")
@limiter.limit("3/hour")
async def unban_request(request: Request, body: AppealIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    if user.status != "banned":
        raise AppError("errors.not_banned")
    if (await db.execute(select(UnbanRequest.id).where(UnbanRequest.user_id == user.id, UnbanRequest.status == "pending"))).first():
        raise Conflict("errors.appeal_pending")
    db.add(UnbanRequest(user_id=user.id, message=body.message))
    await notify_admins(db, "unban_request", title_key="notif.unban_request.title", body_key="notif.unban_request.body",
                        params={"name": user.full_name}, link="/admin?tab=appeals")
    await db.commit()
    return {"ok": True, "message": t("msg.appeal_sent", lang_of(request))}


class LangIn(BaseModel):
    lang: str = Field(max_length=8)


@router.post("/api/i18n/lang")
async def set_language(request: Request, body: LangIn, user: User | None = Depends(current_user),
                       db: AsyncSession = Depends(get_db)):
    if not is_supported(body.lang):
        raise AppError("errors.invalid_input")
    if user:  # verified like every request (a signed-out device gets 401 and changes nothing)
        user.language = body.lang
        await db.commit()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ks_lang", body.lang, max_age=365 * 86400, samesite="lax", secure=settings.https)
    return resp


@router.get("/api/account/sessions")
async def sessions(request: Request, user: User = Depends(require_user)):
    current = (getattr(request.state, "session", None) or {}).get("_h")
    items = await list_user_sessions(user.id)
    return {"sessions": [{**{k: v for k, v in s.items() if k != "hash"}, "current": s["hash"] == current} for s in items]}
