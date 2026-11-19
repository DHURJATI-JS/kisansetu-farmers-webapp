"""Authentication: OTP (SMS/WhatsApp), sign-up (India-only, 18+ workers), login (lockout, rehash, session rotation),
logout, password reset, superadmin TOTP 2FA."""
from __future__ import annotations

import base64
import hmac
import io
import secrets
from datetime import date, timedelta
from typing import Literal

import pyotp
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import client_ip, current_user, get_db, lang_of, require_user
from app.errors import AppError, Conflict
from app.i18n import is_supported, t
from app.models import AbuseEvent, OtpCode, OutboundMessage, TermsAcceptance, User
from app.security.crypto import cipher, hmac_digest
from app.security.passwords import hash_password, hash_password_sync, needs_rehash, password_problems, verify_password
from app.security.ratelimit import ip_key, limiter
from app.security.sessions import (
    clear_session_cookie, create_session, describe_device, destroy_session, destroy_user_sessions, mask_ip, new_login_key,
    prune_stale_sessions, set_session_cookie, update_session,
)
from app.services.audit import audit
from app.services.actions.fields import TypedAddress
from app.services.geo import check_india, locate_address
from app.services.notify import notify
from app.services.redis_client import kv
from app.services.settings_store import get_setting
from app.utils import age_on, normalize_phone, now_utc

router = APIRouter(prefix="/api/auth", tags=["auth"])
PLATFORM_TERMS_VERSION = "platform-v1"
_DUMMY_HASH = hash_password_sync("dummy-password-for-timing")


async def issue_otp(db: AsyncSession, target: str, purpose: str, lang: str) -> str:
    recent = (await db.execute(select(func.count()).select_from(OtpCode).where(
        OtpCode.target == target, OtpCode.created_at >= now_utc() - timedelta(hours=1)))).scalar_one()
    if recent >= 6:
        raise AppError("errors.otp_too_many", status=429)
    code = f"{secrets.randbelow(10**6):06d}"
    db.add(OtpCode(target=target, purpose=purpose, code_hash=hmac_digest(f"{target}:{purpose}:{code}", "otp"),
                   expires_at=now_utc() + timedelta(minutes=10)))
    body = t("otp.sms", lang, code=code, app=settings.APP_NAME)
    db.add(OutboundMessage(channel="sms", to=target, body=body))
    db.add(OutboundMessage(channel="whatsapp", to=target, body=body))
    return code


async def verify_otp(db: AsyncSession, target: str, purpose: str, code: str) -> bool:
    row = (await db.execute(select(OtpCode).where(OtpCode.target == target, OtpCode.purpose == purpose, OtpCode.consumed_at.is_(None),
                                                  OtpCode.expires_at > now_utc()).order_by(OtpCode.created_at.desc())
                            .with_for_update())).scalars().first()
    if not row or row.attempts >= 5:
        return False
    row.attempts += 1
    ok = hmac.compare_digest(row.code_hash, hmac_digest(f"{target}:{purpose}:{(code or '').strip()}", "otp"))
    if ok:
        row.consumed_at = now_utc()
    return ok


class OtpIn(BaseModel):
    phone: str = Field(max_length=20)
    purpose: Literal["signup", "reset"]


@router.post("/otp")
@limiter.limit("4/minute;20/hour", key_func=ip_key)
async def send_otp(request: Request, body: OtpIn, db: AsyncSession = Depends(get_db)):
    phone = normalize_phone(body.phone)
    if not phone:
        raise AppError("errors.phone_invalid")
    exists = (await db.execute(select(User.id).where(User.phone == phone))).first()
    if body.purpose == "signup" and exists:
        raise Conflict("errors.phone_taken")
    code = None
    if body.purpose == "signup" or exists:  # reset: silently do nothing for unknown numbers (no enumeration)
        code = await issue_otp(db, phone, body.purpose, lang_of(request))
    await db.commit()
    out = {"ok": True, "message": t("msg.otp_sent", lang_of(request))}
    if settings.TEST_MODE and code:
        out["test_code"] = code  # TEST MODE ONLY — shown on screen because SMS is simulated
    return out


class SignupIn(BaseModel):
    role: Literal["farmer", "owner", "buyer"]
    full_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(max_length=20)
    otp: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    password: str = Field(min_length=6, max_length=128)
    email: EmailStr | None = None
    language: str = "en"
    dob: date | None = None
    lat: float | None = Field(None, ge=-90, le=90)       # GPS …
    lon: float | None = Field(None, ge=-180, le=180)
    address: TypedAddress | None = None                  # … or typed by hand (no GPS, or a wrong fix)
    accept_terms: Literal[True]
    accept_privacy: Literal[True]


@router.post("/signup")
@limiter.limit("5/minute;30/hour", key_func=ip_key)
async def signup(request: Request, body: SignupIn, db: AsyncSession = Depends(get_db)):
    lang = body.language if is_supported(body.language) else lang_of(request)
    phone = normalize_phone(body.phone)
    if not phone:
        raise AppError("errors.phone_invalid")
    # location before any database work: the map service is never awaited inside an open transaction
    typed = body.address is not None
    if typed:
        geo = await locate_address(**body.address.model_dump())
        if not geo:
            raise AppError("errors.place_not_found")
        lat, lon = geo["lat"], geo["lon"]
    else:
        if body.lat is None or body.lon is None:
            raise AppError("errors.location_required")
        geo = await check_india(body.lat, body.lon)
        if not geo["ok"]:
            raise AppError("errors.outside_india")
        lat, lon = body.lat, body.lon
    if (await db.execute(select(User.id).where(User.phone == phone))).first():
        raise Conflict("errors.phone_taken")
    email = body.email.lower() if body.email else None
    if body.role == "owner" and not email:
        raise AppError("errors.owner_email_required")
    if email and (await db.execute(select(User.id).where(func.lower(User.email) == email))).first():
        raise Conflict("errors.email_taken")
    min_age = int(await get_setting(db, "min_worker_age"))
    if body.role in ("farmer", "owner"):
        if not body.dob:
            raise AppError("errors.dob_required")
        if (age_on(body.dob) or 0) < min_age:
            raise AppError("errors.too_young", age=min_age)
    problems = password_problems(body.password, body.role)
    if problems:
        raise AppError(problems[0])
    if not await verify_otp(db, phone, "signup", body.otp):
        await db.commit()
        raise AppError("errors.otp_invalid")
    now = now_utc()
    user = User(full_name=body.full_name.strip(), phone=phone, email=email, password_hash=await hash_password(body.password),
                role=body.role, language=lang, phone_verified=True, dob=body.dob, lat=lat, lon=lon,
                # a typed address is not GPS-verified: jobs still ask for the live location when applying
                location_verified_at=None if typed else now,
                state=geo.get("state"), district=geo.get("district"), city=geo.get("city"),
                terms_version=PLATFORM_TERMS_VERSION, terms_accepted_at=now, privacy_accepted_at=now, last_login_at=now,
                last_login_ip=client_ip(request))
    key, key_hash = new_login_key()
    user.session_key_hash, user.session_started_at = key_hash, now
    user.session_device, user.session_ip = describe_device(request.headers.get("user-agent")), client_ip(request)
    db.add(user)
    await db.flush()
    db.add(TermsAcceptance(user_id=user.id, version=PLATFORM_TERMS_VERSION, ip=client_ip(request)))
    await audit(db, user.id, "auth.signup", "user", user.id, {"role": body.role, "location": "typed" if typed else "gps"},
                client_ip(request))
    await db.commit()
    sid = await create_session(user.id, user.token_version, client_ip(request), request.headers.get("user-agent"), extra={"lk": key})
    from app.services.ai.moderation import note_ip_account

    await note_ip_account(client_ip(request), user.id)
    resp = JSONResponse({"ok": True, "redirect": "/onboarding"})
    set_session_cookie(resp, sid)
    resp.set_cookie("ks_lang", lang, max_age=365 * 86400, samesite="lax", secure=settings.https)
    return resp


class LoginIn(BaseModel):
    phone: str = Field(max_length=20)
    password: str = Field(min_length=1, max_length=128)
    totp: str | None = Field(default=None, max_length=8)


@router.post("/login")
@limiter.limit("8/minute;60/hour", key_func=ip_key)
async def login(request: Request, body: LoginIn, db: AsyncSession = Depends(get_db)):
    ip = client_ip(request)
    phone = normalize_phone(body.phone)
    user = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none() if phone else None
    if user is None or user.status == "deleted":
        await verify_password(_DUMMY_HASH, body.password)  # equalise timing
        raise AppError("errors.invalid_login", status=401)
    if user.locked_until and user.locked_until > now_utc():   # set only by an administrator
        raise AppError("errors.account_locked", status=429, minutes=settings.LOGIN_LOCK_MINUTES)
    # Wrong-password lockout is per account AND per IP: an attacker guessing from elsewhere locks out only themselves,
    # never the real owner (the load test's password guessing used to lock the superadmin out for everyone).
    fail_key = f"ks:loginfail:{user.id}:{ip}"
    if int(await kv().get(fail_key) or 0) >= settings.LOGIN_MAX_ATTEMPTS:
        raise AppError("errors.account_locked", status=429, minutes=settings.LOGIN_LOCK_MINUTES)
    if not await verify_password(user.password_hash, body.password):
        fails = int(await kv().incr(fail_key))
        if fails == 1:
            await kv().expire(fail_key, settings.LOGIN_LOCK_MINUTES * 60)
        if fails == settings.LOGIN_MAX_ATTEMPTS:
            db.add(AbuseEvent(ip=ip, user_id=user.id, kind="login_lockout", severity="medium", details={"phone": user.phone[-4:]}))
            await db.commit()
            from app.services.ai.moderation import abuse_monitor

            # the strike is on the GUESSING IP, never on the account being guessed
            await abuse_monitor.offence(ip=ip, user_id=None, kind="login_abuse", weight=2)
        raise AppError("errors.invalid_login", status=401)
    await kv().delete(fail_key)
    mfa = False
    if user.is_admin and user.totp_enabled:
        if not body.totp:
            return JSONResponse({"ok": False, "totp_required": True}, status_code=200)
        secret = cipher.decrypt_str(user.totp_secret_enc, f"totp:{user.id}")
        if not pyotp.TOTP(secret).verify(body.totp, valid_window=1):
            raise AppError("errors.totp_invalid", status=401)
        mfa = True
    now = now_utc()
    ua = request.headers.get("user-agent")
    user.failed_logins, user.locked_until = 0, None
    user.last_login_at, user.last_login_ip = now, ip
    if needs_rehash(user.password_hash):
        user.password_hash = await hash_password(body.password)
    # ONE login per account: a fresh key replaces the previous login; every other device is signed out (told why)
    key, key_hash = new_login_key()
    device = describe_device(ua)
    user.session_key_hash, user.session_started_at, user.session_device, user.session_ip = key_hash, now, device, ip
    old = getattr(request.state, "session", None)
    if old:
        await destroy_session(sid_hash=old.get("_h"), user_id=old.get("uid"))  # session fixation defence
    replaced = await destroy_user_sessions(user.id, reason="replaced",
                                           details={"device": device, "ip": mask_ip(ip), "at": int(now.timestamp())})
    if replaced:
        await notify(db, user.id, "security", title_key="notif.new_login.title", body_key="notif.new_login.body",
                     params={"device": device, "ip": mask_ip(ip), "count": replaced}, link="/account", severity="warning",
                     channels=("email",))
    await audit(db, user.id, "auth.login", "user", user.id, {"device": device, "signed_out_devices": replaced}, ip)
    await db.commit()
    sid = await create_session(user.id, user.token_version, ip, ua, extra={"mfa": mfa, "lk": key})
    from app.services.ai.moderation import note_ip_account

    await note_ip_account(ip, user.id)
    # two logins racing for the same account: whichever key the database kept wins, every other session ends now
    latest = (await db.execute(select(User.session_key_hash).where(User.id == user.id))).scalar_one_or_none()
    await prune_stale_sessions(user.id, latest, {"device": device, "ip": mask_ip(ip), "at": int(now.timestamp())})
    redirect = "/banned" if user.status == "banned" else ("/admin/2fa" if user.is_admin and not mfa else
                                                          "/admin" if user.is_admin else "/dashboard")
    resp = JSONResponse({"ok": True, "redirect": redirect})
    set_session_cookie(resp, sid)
    resp.set_cookie("ks_lang", user.language or "en", max_age=365 * 86400, samesite="lax", secure=settings.https)
    return resp


@router.post("/logout")
async def logout(request: Request):
    sess = getattr(request.state, "session", None)
    if sess:
        await destroy_session(sid_hash=sess.get("_h"), user_id=sess.get("uid"))
    resp = JSONResponse({"ok": True, "redirect": "/"})
    clear_session_cookie(resp)
    return resp


class ResetIn(BaseModel):
    phone: str = Field(max_length=20)
    otp: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    new_password: str = Field(min_length=6, max_length=128)


@router.post("/password/reset")
@limiter.limit("5/minute;20/hour", key_func=ip_key)
async def reset_password(request: Request, body: ResetIn, db: AsyncSession = Depends(get_db)):
    phone = normalize_phone(body.phone)
    user = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none() if phone else None
    if not user or user.status == "deleted" or not await verify_otp(db, phone, "reset", body.otp):
        await db.commit()
        raise AppError("errors.otp_invalid")
    problems = password_problems(body.new_password, user.role)
    if problems:
        raise AppError(problems[0])
    user.password_hash = await hash_password(body.new_password)
    user.token_version += 1
    user.session_key_hash = None
    user.failed_logins, user.locked_until = 0, None
    await audit(db, user.id, "auth.password_reset", "user", user.id, {}, client_ip(request))
    await db.commit()
    await destroy_user_sessions(user.id, reason="password_reset")
    return {"ok": True, "message": t("msg.password_changed", lang_of(request)), "redirect": "/login"}


class LocationIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


@router.post("/location")
@limiter.limit("20/minute")
async def location_check(request: Request, body: LocationIn):
    geo = await check_india(body.lat, body.lon)
    return {"ok": geo["ok"], "state": geo.get("state"), "district": geo.get("district"), "city": geo.get("city"),
            "message": None if geo["ok"] else t("errors.outside_india", lang_of(request))}


@router.post("/location/typed")
@limiter.limit("10/minute;60/hour", key_func=ip_key)
async def location_typed(request: Request, body: TypedAddress):
    """No GPS (or a wrong fix): find a typed village / district / PIN code in India — a preview before it is saved."""
    geo = await locate_address(**body.model_dump())
    if not geo:
        return {"ok": False, "message": t("errors.place_not_found", lang_of(request))}
    return {"ok": True, **{k: geo[k] for k in ("state", "district", "city", "precision", "approx", "found")}}


# ------------------------------------------------------------------ superadmin TOTP
@router.post("/totp/setup")
@limiter.limit("10/minute")
async def totp_setup(request: Request, user: User = Depends(require_user)):
    if not user.is_admin:
        raise AppError("errors.admin_only", status=403)
    if user.totp_enabled:
        raise Conflict("errors.totp_already")
    secret = pyotp.random_base32()
    sess = request.state.session
    await update_session(sess["_h"], totp_pending=cipher.encrypt(secret, f"totp-pending:{user.id}"))
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.phone, issuer_name=settings.APP_NAME)
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    return {"qr": "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode(), "secret": secret}


class TotpIn(BaseModel):
    code: str = Field(min_length=6, max_length=8, pattern=r"^\d+$")


@router.post("/totp/verify")
@limiter.limit("6/minute")
async def totp_verify(request: Request, body: TotpIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    if not user.is_admin:
        raise AppError("errors.admin_only", status=403)
    sess = request.state.session
    if user.totp_enabled:
        secret = cipher.decrypt_str(user.totp_secret_enc, f"totp:{user.id}")
    else:
        pending = sess.get("totp_pending")
        if not pending:
            raise AppError("errors.totp_setup_first")
        secret = cipher.decrypt_str(pending, f"totp-pending:{user.id}")
    if not pyotp.TOTP(secret).verify(body.code, valid_window=1):
        raise AppError("errors.totp_invalid", status=401)
    if not user.totp_enabled:
        user.totp_secret_enc, user.totp_enabled = cipher.encrypt(secret, f"totp:{user.id}"), True
        await audit(db, user.id, "auth.totp_enabled", "user", user.id, {}, client_ip(request))
        await db.commit()
    await update_session(sess["_h"], mfa=True, totp_pending=None)
    return {"ok": True, "redirect": "/admin"}


@router.get("/me")
async def me(user: User | None = Depends(current_user)):
    if not user:
        return {"user": None}
    return {"user": {"id": str(user.id), "name": user.full_name, "role": user.role, "status": user.status, "language": user.language}}
