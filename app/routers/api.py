"""JSON APIs (all mutations of business state go through /api/actions → prepare → GO)."""
from __future__ import annotations

import csv
import io
import logging
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, StringConstraints, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import client_ip, current_user, get_db, lang_of, require_active, require_user
from app.errors import AppError, NotFound
from app.i18n import t
from app.models import GstinVerification, LedgerEntry, Notification, Product, Upload, User
from app.security.ratelimit import ip_key, limiter
from app.security.sanitize import clean_text, csv_safe
from app.services import actions as act
from app.services.ai import assistant
from app.services.gstin import is_valid_gstin, scan_certificate
from app.services.india_data import STATES
from app.services.jobs import OFFICIAL_PORTALS, search_external, search_internal
from app.services.notify import render_notification
from app.services.perms import require_perm
from app.services.schemes import active_schemes, evaluate, scheme_card
from app.services.settings_store import get_setting
from app.services.shop import ask_about_product, search_products
from app.services.terms import review_terms
from app.services.uploads import KINDS, UploadError, can_view, read_upload_bytes, save_upload
from app.services.weather import location_advice
from app.services.welfare import submit_survey
from app.utils import age_on, format_inr, to_jsonable

log = logging.getLogger("kisansetu.api")
router = APIRouter(tags=["api"])


# ------------------------------------------------------------------ actions (GO step)
class PrepareIn(BaseModel):
    type: str = Field(max_length=48)
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/api/actions/prepare")
@limiter.limit("80/minute")   # per account: a busy shop owner marks many orders; scripts are still stopped
async def action_prepare(request: Request, body: PrepareIn, user: User = Depends(require_active), db: AsyncSession = Depends(get_db)):
    out = await act.prepare_action(db, user, lang_of(request), client_ip(request), body.type, body.payload)
    await db.commit()
    return out


@router.post("/api/actions/{action_id}/go")
@limiter.limit("60/minute")   # 3-hour soak: 30/min throttled owners processing orders in bulk
async def action_go(request: Request, action_id: uuid.UUID, user: User = Depends(require_active), db: AsyncSession = Depends(get_db)):
    sid_hash = (getattr(request.state, "session", None) or {}).get("_h")
    out = await act.go_action(db, user, lang_of(request), client_ip(request), action_id, sid_hash=sid_hash)
    await db.commit()
    resp = JSONResponse(to_jsonable(out))
    if out.get("logout"):
        from app.security.sessions import clear_session_cookie

        clear_session_cookie(resp)
    return resp


@router.post("/api/actions/{action_id}/cancel")
async def action_cancel(action_id: uuid.UUID, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    await act.cancel_action(db, user, action_id)
    await db.commit()
    return {"ok": True}


class VoteIn(BaseModel):
    vote: Literal["approve", "reject"]


@router.post("/api/proposals/{proposal_id}/vote")
@limiter.limit("20/minute")
async def proposal_vote(request: Request, proposal_id: uuid.UUID, body: VoteIn, user: User = Depends(require_active),
                        db: AsyncSession = Depends(get_db)):
    out = await act.vote_proposal(db, user, lang_of(request), client_ip(request), proposal_id, body.vote)
    await db.commit()
    return out


# ------------------------------------------------------------------ uploads & media
@router.post("/api/uploads")
@limiter.limit("30/minute;300/day")
async def upload(request: Request, kind: str = Form(...), file: UploadFile = File(...), user: User = Depends(require_active),
                 db: AsyncSession = Depends(get_db)):
    if kind not in KINDS or kind == "photo":
        raise AppError("errors.upload_kind")
    try:
        up = await save_upload(db, user, kind, file, ip=client_ip(request))
    except UploadError as exc:
        raise AppError(str(exc), status=exc.status) from exc
    await db.commit()
    return {"upload_id": str(up.id), "url": f"/media/{up.id}", "mime": up.mime}


@router.get("/media/{upload_id}")
@limiter.limit("240/minute")
async def media(request: Request, upload_id: uuid.UUID, user: User | None = Depends(current_user), db: AsyncSession = Depends(get_db)):
    up = await db.get(Upload, upload_id)
    if not up or up.path == "purged" or not await can_view(db, user, up):
        raise NotFound()
    data = await read_upload_bytes(up)
    public = KINDS.get(up.kind, (False,))[0]
    is_pdf = up.mime == "application/pdf"
    return Response(data, media_type=up.mime, headers={
        "Cache-Control": "public, max-age=86400, immutable" if public else "private, no-store",
        "Content-Disposition": f'attachment; filename="{up.kind}-{str(up.id)[:8]}.pdf"' if is_pdf else "inline",
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
        "X-Content-Type-Options": "nosniff", "Cross-Origin-Resource-Policy": "same-origin"})


# ------------------------------------------------------------------ farms helpers
@router.post("/api/gstin/scan")
@limiter.limit("6/minute;40/day")
async def gstin_scan(request: Request, file: UploadFile = File(...), manual_gstin: str | None = Form(default=None),
                     user: User = Depends(require_active), db: AsyncSession = Depends(get_db)):
    if user.role != "owner":
        raise AppError("errors.role_not_allowed", status=403)
    try:
        up = await save_upload(db, user, "gst", file, ip=client_ip(request))
    except UploadError as exc:
        raise AppError(str(exc), status=exc.status) from exc
    data = await read_upload_bytes(up)
    res = await scan_certificate(data) if not up.mime == "application/pdf" else {"ok": False, "gstin": None, "engines": {}, "reason": "pdf"}
    gstin = res.get("gstin")
    if not res["ok"]:
        manual = clean_text(manual_gstin or "", single_line=True).upper()
        if settings.TEST_MODE and is_valid_gstin(manual):
            gstin, res["manual"] = manual, True  # TEST MODE fallback when no OCR engine could read the certificate
        else:
            await db.commit()
            raise AppError(f"errors.gstin_scan_{res.get('reason') or 'unreadable'}")
    v = GstinVerification(user_id=user.id, gstin=gstin, legal_name=res.get("legal_name"), trade_name=res.get("trade_name"),
                          state_code=gstin[:2], status="pending", ocr={k: res.get(k) for k in ("engines", "agreeing", "manual")},
                          upload_id=up.id)
    db.add(v)
    up.used_for = f"gst:{user.id}"
    await db.flush()
    await db.commit()
    return {"verification_id": str(v.id), "gstin": gstin, "engines": res.get("engines"), "legal_name": res.get("legal_name"),
            "manual": bool(res.get("manual"))}


class GeoIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    state: str | None = Field(default=None, max_length=64)


@router.post("/api/farms/recommend")
@limiter.limit("10/minute")
async def farm_recommend(request: Request, body: GeoIn, user: User = Depends(require_active)):
    from app.services.geo import check_india

    geo = await check_india(body.lat, body.lon)
    if not geo["ok"]:
        raise AppError("errors.outside_india")
    adv = await location_advice(body.lat, body.lon, geo.get("state") or body.state, lang_of(request))
    lang = lang_of(request)
    for r in adv["recommendations"]:
        r["farming_label"] = [t(f"farming.{f}", lang) for f in r["farming_types"]]
    adv["farming_type_label"] = t(f"farming.{adv['farming_type']}", lang)
    adv["place"] = {k: geo.get(k) for k in ("city", "district", "state")}
    return adv


class TermsIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


@router.post("/api/terms/check")
@limiter.limit("8/minute;60/day")
async def terms_check(request: Request, body: TermsIn, user: User = Depends(require_active), db: AsyncSession = Depends(get_db)):
    from app.services.terms import issue_text

    res = await review_terms(db, body.text)
    await db.commit()
    lang = lang_of(request)
    res["issues"] = [{**i, "problem": issue_text(i, lambda k: t(k, lang))} for i in res.get("issues") or []]
    return res


@router.get("/farms/{farm_id}/ledger.csv")
@limiter.limit("10/minute")
async def ledger_csv(request: Request, farm_id: uuid.UUID, user: User = Depends(require_active), db: AsyncSession = Depends(get_db)):
    farm, _ = await require_perm(db, farm_id, user, "ledger")
    rows = (await db.execute(select(LedgerEntry).where(LedgerEntry.farm_id == farm.id).order_by(LedgerEntry.occurred_on,
                                                                                               LedgerEntry.created_at))).scalars().all()
    lang = lang_of(request)

    def safe(v: Any) -> str:
        return csv_safe("" if v is None else str(v))

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([t(k, lang) for k in ("csv.date", "csv.recorded_at", "csv.type", "csv.direction", "csv.amount", "csv.category",
                                         "csv.description", "csv.counterparty", "csv.recorded_by", "csv.reference")])
        yield "﻿" + buf.getvalue()
        for r in rows:
            buf.seek(0)
            buf.truncate()
            w.writerow([r.occurred_on.isoformat(), r.created_at.isoformat(), r.entry_type, r.direction, str(r.amount), safe(r.category),
                        safe(r.description), safe(r.counterparty_name), safe(r.created_by_name), r.reference_id or ""])
            yield buf.getvalue()

    return StreamingResponse(gen(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="ledger-{farm.id.hex[:8]}.csv"'})


# ------------------------------------------------------------------ shop
@router.get("/api/shop/products")
@limiter.limit("60/minute")
async def shop_products(request: Request, q: str | None = None, category: str | None = None, city: str | None = None,
                        sort: str = "relevance", lat: float | None = None, lon: float | None = None, radius: float | None = None,
                        shop: uuid.UUID | None = None, page: int = 1, db: AsyncSession = Depends(get_db)):
    page = max(1, min(page, 50))
    res = await search_products(db, q=(q or "")[:100] or None, category=category, city=city, lat=lat, lon=lon,
                                radius_km=min(radius, 500) if radius else None, sort=sort, shop_id=shop, page=page)
    lang = lang_of(request)
    return {"has_more": res["has_more"], "items": [{
        "id": str(r["product"].id), "name": r["product"].name, "price": str(r["product"].price), "price_fmt": format_inr(r["product"].price),
        "unit": t(f"unit.{r['product'].unit}", lang), "unit_code": r["product"].unit, "stock": str(r["product"].stock),
        "category": t(f"cat.{r['product'].category}", lang), "image": f"/media/{r['product'].images[0]}" if r["product"].images else None,
        "shop": r["shop_name"], "farm": r["farm_name"], "city": r["city"], "state": r["state"], "rating": r["rating"],
        "reviews": r["reviews"], "distance_km": r["distance_km"], "shop_id": str(r["product"].shop_id),
        "branch_id": str(r["product"].branch_id), "min_qty": str(r["product"].min_order_qty), "version": r["product"].version,
    } for r in res["items"]]}


@router.get("/api/shop/cart")
@limiter.limit("60/minute")
async def shop_cart(request: Request, ids: str = "", db: AsyncSession = Depends(get_db)):
    """Fresh price/stock for the items in a browser cart (one query)."""
    from app.models import Branch, Shop

    try:
        pids = [uuid.UUID(x) for x in ids.split(",") if x][:30]
    except ValueError as exc:
        raise AppError("errors.invalid_input") from exc
    if not pids:
        return {"items": []}
    lang = lang_of(request)
    rows = (await db.execute(select(Product, Shop.name, Branch.name, Branch.city).join(Shop, Shop.id == Product.shop_id)
                             .join(Branch, Branch.id == Product.branch_id).where(Product.id.in_(pids)))).all()
    return {"items": [{"id": str(p.id), "name": p.name, "price": str(p.price), "price_fmt": format_inr(p.price), "stock": str(p.stock),
                       "unit": t(f"unit.{p.unit}", lang), "min_qty": str(p.min_order_qty), "active": p.status == "active",
                       "shop_id": str(p.shop_id), "shop": sn, "branch_id": str(p.branch_id), "branch": bn, "city": bc,
                       "image": f"/media/{p.images[0]}" if p.images else None} for p, sn, bn, bc in rows]}


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)


@router.post("/api/shop/products/{product_id}/ask")
@limiter.limit("6/minute;60/day")
async def product_ask(request: Request, product_id: uuid.UUID, body: AskIn, user: User | None = Depends(current_user),
                      db: AsyncSession = Depends(get_db)):
    from app.services.ai.moderation import abuse_monitor

    prod = await db.get(Product, product_id)
    if not prod or prod.status != "active":
        raise NotFound("errors.product_not_found")
    from app.services.ai.moderation import ensure_can_post

    ensure_can_post(user)
    v = await abuse_monitor.check_text(body.question, user_id=user.id if user else None, ip=client_ip(request), channel="product_qa")
    if not v.allow:
        raise AppError("errors.content_blocked")
    res = await ask_about_product(db, user, prod, body.question, lang_of(request))
    await db.commit()
    lang = lang_of(request)
    res["message"] = t("msg.question_forwarded" if res["forwarded_to_owner"] else "msg.ai_answered", lang)
    return res


# ------------------------------------------------------------------ jobs
@router.get("/api/jobs/search")
@limiter.limit("60/minute")
async def jobs_search(request: Request, q: str | None = None, city: str | None = None, lat: float | None = None,
                      lon: float | None = None, min_salary: Decimal | None = None, sort: str = "distance",
                      source: Literal["all", "internal", "external"] = "all", db: AsyncSession = Depends(get_db)):
    lang = lang_of(request)
    radius = float(await get_setting(db, "max_join_radius_km"))
    internal = [] if source == "external" else await search_internal(db, lat=lat, lon=lon, radius_km=radius, q=q, city=city,
                                                                      min_daily=min_salary, sort=sort)
    external = []
    if source == "external" or (source == "all" and not internal):
        external = await search_external(db, lat=lat, lon=lon, q=q, city=city, min_daily=min_salary, sort=sort)
    return {
        "radius_km": radius,
        "internal": [{"id": str(r["job"].id), "title": r["job"].title, "farm": r["farm_name"], "branch": r["branch"].name,
                      "city": r["branch"].city, "state": r["branch"].state, "distance_km": r["distance_km"],
                      "salary": format_inr(r["job"].salary_amount), "unit": t(f"salary.{r['job'].salary_type}", lang),
                      "positions": r["job"].positions, "hours": f"{r['branch'].work_start:%H:%M}–{r['branch'].work_end:%H:%M}",
                      "farming_type": t(f"farming.{r['farming_type']}", lang), "crops": r["crops"],
                      "custom_terms": r["terms_mode"] == "custom", "role": t(f"role.{r['job'].role}", lang)} for r in internal],
        "external": [{"title": r["job"].title, "employer": r["job"].employer, "city": r["job"].city, "state": r["job"].state,
                      "salary": r["job"].salary_text, "site": r["job"].source_site, "url": r["job"].source_url,
                      "snippet": r["job"].snippet, "distance_km": r["distance_km"], "fetched": r["job"].fetched_at.isoformat()}
                     for r in external],
        "portals": OFFICIAL_PORTALS if not internal else [],
    }


# ------------------------------------------------------------------ schemes
class SchemeIn(BaseModel):
    age: int | None = Field(default=None, ge=0, le=120)
    landholding_hectares: Decimal | None = Field(default=None, ge=0, le=100000)
    monthly_income: Decimal | None = Field(default=None, ge=0, le=Decimal("100000000"))
    is_income_tax_payer: bool | None = None
    has_bank_account: bool | None = None
    is_govt_employee: bool | None = None
    has_epf_esic_nps: bool | None = None
    is_rural: bool | None = None
    state: str | None = Field(default=None, max_length=64)
    role: Literal["farmer", "owner", "buyer"] | None = None


@router.post("/api/schemes/check")
@limiter.limit("20/minute")
async def schemes_check(request: Request, body: SchemeIn, user: User | None = Depends(current_user),
                        db: AsyncSession = Depends(get_db)):
    profile = body.model_dump()
    if profile.get("state") and profile["state"] not in STATES:
        profile["state"] = None
    if user:
        if profile["age"] is None:
            profile["age"] = age_on(user.dob)
        for k in ("landholding_hectares", "is_income_tax_payer", "has_bank_account", "is_govt_employee", "has_epf_esic_nps",
                  "is_rural", "state"):
            if profile.get(k) is None:
                profile[k] = getattr(user, k)
        if profile.get("monthly_income") is None and user.annual_income is not None:
            profile["monthly_income"] = Decimal(user.annual_income) / 12
    role = body.role or (user.role if user else None)
    lang = lang_of(request)
    schemes = await active_schemes(db)
    by_id = {x["id"]: x for x in schemes}
    out = []
    for r in evaluate(profile, role, schemes):
        card = scheme_card(by_id[r["id"]], lang)
        out.append({**r, "name": card["name"], "desc": card["desc"], "translate": card["translate"], "badges": card["badges"],
                    "rules": card["rules"], "link_status": card["link_status"],
                    "status_label": t(f"scheme.status.{r['status']}", lang),
                    "reasons": [t(x, lang) for x in r["reasons"]], "missing": [t(f"field.{m}", lang) for m in r["missing"]],
                    "go": f"/schemes/go/{r['id']}"})
    return {"results": out}


# ------------------------------------------------------------------ welfare
class SurveyIn(BaseModel):
    rating: Literal["good", "bad"]
    answers: dict[str, bool] = Field(default_factory=dict)
    comment: str | None = Field(default=None, max_length=1000)


@router.post("/api/welfare/surveys/{survey_id}")
@limiter.limit("10/minute")
async def survey_answer(request: Request, survey_id: uuid.UUID, body: SurveyIn, user: User = Depends(require_active),
                        db: AsyncSession = Depends(get_db)):
    res = await submit_survey(db, user, survey_id, body.rating, body.answers, body.comment, client_ip(request))
    await db.commit()
    lang = lang_of(request)
    key = "msg.survey_escalated" if res.get("escalated") else "msg.survey_reported" if res.get("reported") else "msg.survey_thanks"
    return {**res, "message": t(key, lang)}


# ------------------------------------------------------------------ notifications
@router.get("/api/notifications")
async def notifications(request: Request, limit: int = 30, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Notification).where(Notification.user_id == user.id)
                             .order_by(Notification.created_at.desc()).limit(max(1, min(limit, 100))))).scalars().all()
    return {"items": [render_notification(n, lang_of(request)) for n in rows]}


class ReadIn(BaseModel):
    ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    all: bool = False


@router.post("/api/notifications/read")
async def notifications_read(body: ReadIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    from app.utils import now_utc

    stmt = update(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))
    if not body.all:
        stmt = stmt.where(Notification.id.in_(body.ids))
    await db.execute(stmt.values(read_at=now_utc()))
    await db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ AI
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=1500)
    session: str | None = Field(default=None, max_length=40, pattern=r"^[A-Za-z0-9_\-]*$")
    page_keys: list[Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{1,40}$")]] | None = Field(default=None, max_length=30)


@router.post("/api/ai/chat")
@limiter.limit("12/minute;300/day")
async def ai_chat(request: Request, body: ChatIn, user: User | None = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if user and user.status != "active":
        raise AppError("errors.account_inactive", status=403)
    if user is None:  # anonymous login/sign-up guide: stricter limits, separate session namespace
        from app.security.crypto import hmac_digest

        session_key = "g:" + hmac_digest(f"{client_ip(request)}:{body.session or ''}", "guide")[:40]
    else:
        session_key = f"u:{user.id}"
    res = await assistant.chat(db, user, lang_of(request), body.message, session_key=session_key, ip=client_ip(request),
                               page_keys=body.page_keys)
    await db.commit()
    return res


@router.post("/api/ai/plant")
@limiter.limit("4/minute;40/day")
async def ai_plant(request: Request, file: UploadFile = File(...), question: str | None = Form(default=None),
                   user: User = Depends(require_active), db: AsyncSession = Depends(get_db)):
    try:
        up = await save_upload(db, user, "plant", file, ip=client_ip(request))
    except UploadError as exc:
        raise AppError(str(exc), status=exc.status) from exc
    res = await assistant.plant_doctor(db, user, lang_of(request), await read_upload_bytes(up), clean_text(question or "") or None,
                                       client_ip(request))
    up.used_for = f"plant:{user.id}"
    await db.commit()
    return {**res, "image": f"/media/{up.id}"}


# ------------------------------------------------------------------ i18n
class TranslateIn(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=40)


@router.post("/api/i18n/translate")
@limiter.limit("30/minute;600/day")
async def translate(request: Request, body: TranslateIn):
    from app.services.i18n_ai import translate_texts

    lang = lang_of(request)
    return {"lang": lang, "texts": await translate_texts([s[:2000] for s in body.texts], lang, client=client_ip(request))}


# ------------------------------------------------------------------ CSP reports / health
@router.post("/api/csp-report")
@limiter.limit("30/minute", key_func=ip_key)
async def csp_report(request: Request):
    body = (await request.body())[:4000]
    log.warning("CSP violation from %s: %s", client_ip(request), body.decode("utf-8", "ignore")[:1000])
    return Response(status_code=204)


@router.get("/healthz")
@limiter.exempt
async def healthz():
    return {"ok": True}


@router.get("/internal/metrics")
@limiter.exempt
async def internal_metrics(request: Request):
    """Runtime metrics for the load-test sampler / ops. Loopback only (never through the public proxy)."""
    import ipaddress

    try:
        ip = ipaddress.ip_address(client_ip(request))
        allowed = any(ip in ipaddress.ip_network(n.strip(), strict=False) for n in settings.METRICS_ALLOW.split(",") if n.strip())
    except ValueError:
        allowed = False
    if not allowed or request.headers.get("x-forwarded-for"):
        raise NotFound()
    import json as _json

    from app.services.redis_client import kv

    workers = []
    from app.services.loopmon import HOST

    async for key in kv().scan_iter(match=f"ks:metrics:{HOST}:*"):
        raw = await kv().get(key)
        if raw:
            workers.append(_json.loads(raw))
    return {"workers": len(workers),
            "db_pool": {"checked_out": sum(w["db_out"] for w in workers), "overflow": sum(w["db_overflow"] for w in workers)},
            "ai_queue": {"queued": sum(w["ai"]["queued"] for w in workers), "active": sum(w["ai"]["active"] for w in workers)},
            "loop_lag_ms": max((w["lag_ms"] for w in workers), default=0.0),
            "loop_lag_max_ms": max((w["lag_max_ms"] for w in workers), default=0.0),
            "cpu_s": round(sum(w.get("cpu_s") or 0 for w in workers), 3),
            "rss_mb": round(sum(w.get("rss_mb") or 0 for w in workers), 1),
            "per_worker": sorted(workers, key=lambda w: w["pid"])}


@router.get("/readyz")
@limiter.exempt
async def readyz(db: AsyncSession = Depends(get_db)):
    from sqlalchemy import text

    from app.services.ai.providers import router as ai_router
    from app.services.ai.queue import ai_queue
    from app.services.redis_client import MODE, kv

    from app.services.uploads import clamav_ping

    await db.execute(text("SELECT 1"))
    await kv().ping()
    av = await clamav_ping()
    return {"ok": av or not settings.CLAMAV_HOST, "redis": MODE, "antivirus": "ok" if av else ("off" if not settings.CLAMAV_HOST else "down"),
            "ai": [{k: p[k] for k in ("name", "available")} for p in ai_router.status()], "queue": ai_queue.stats()}
