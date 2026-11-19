"""Account actions: profile, location, encrypted ID proofs, password, sessions, deletion."""
from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import EmailStr, Field
from sqlalchemy import func, select

from app.errors import AppError, Conflict, NotFound
from app.models import IdentityDocument, User
from app.security.crypto import blind_index, cipher
from app.security.passwords import hash_password, password_problems, verify_password
from app.security.sessions import destroy_user_sessions
from app.services.actions.base import Action, Ctx, Payload, Prepared, register
from app.services.actions.fields import Lat, Lon, Name, ShortText, TypedAddress
from app.services.deletion import deletion_blockers, request_deletion
from app.services.geo import check_india, locate_address
from app.services.settings_store import get_all, get_setting
from app.services.uploads import owned_upload
from app.utils import age_on, now_utc

ID_TYPES = ("aadhaar", "voter_id", "pan", "ration_card", "driving_licence", "job_card", "other")
_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
      [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1], [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
      [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
      [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1], [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def verhoeff_ok(num: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(num)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def validate_id_number(doc_type: str, raw: str) -> str:
    n = re.sub(r"[\s\-]", "", raw or "").upper()
    if doc_type == "aadhaar":
        if not re.fullmatch(r"[2-9]\d{11}", n) or not verhoeff_ok(n):
            raise AppError("errors.id_aadhaar_invalid")
    elif doc_type == "pan":
        if not re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", n):
            raise AppError("errors.id_pan_invalid")
    elif doc_type == "voter_id":
        if not re.fullmatch(r"[A-Z]{3}\d{7}", n):
            raise AppError("errors.id_voter_invalid")
    elif not re.fullmatch(r"[A-Z0-9/]{4,32}", n):
        raise AppError("errors.id_invalid")
    return n


@register
class ProfileUpdate(Action):
    type = "profile.update"

    class Payload(Payload):
        full_name: Name | None = None
        email: EmailStr | None = None
        contact_visible: bool | None = None
        dob: date | None = None
        gender: Literal["female", "male", "other", "prefer_not"] | None = None
        address: ShortText | None = None
        skills: ShortText | None = None
        landholding_hectares: Decimal | None = Field(default=None, ge=0, le=100000)
        annual_income: Decimal | None = Field(default=None, ge=0, le=Decimal("1000000000"))
        social_category: Literal["general", "obc", "sc", "st", "ews"] | None = None
        is_income_tax_payer: bool | None = None
        has_bank_account: bool | None = None
        is_govt_employee: bool | None = None
        has_epf_esic_nps: bool | None = None
        is_rural: bool | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        u = ctx.user
        changes = {k: v for k, v in p.model_dump(exclude_unset=True).items() if getattr(u, k) != v}
        if not changes:
            raise AppError("errors.no_changes")
        if "email" in changes:
            if changes["email"] is None and u.role == "owner":
                raise AppError("errors.owner_email_required")
            if changes["email"]:
                changes["email"] = changes["email"].lower()
                taken = (await ctx.db.execute(select(User.id).where(func.lower(User.email) == changes["email"], User.id != u.id))).first()
                if taken:
                    raise Conflict("errors.email_taken")
        if "dob" in changes and changes["dob"] and u.role == "farmer":
            if (age_on(changes["dob"]) or 0) < int(await get_setting(ctx.db, "min_worker_age")):
                raise AppError("errors.too_young")
        lines = [ctx.t("sum.profile_field", field=ctx.t(f"field.{k}"), value=ctx.t("common.hidden") if k == "email" and v else
                       (ctx.t("common.yes") if v is True else ctx.t("common.no") if v is False else (v if v is not None else "—")))
                 for k, v in changes.items()]
        return Prepared(lines, data={"changes": changes})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        for k, v in prep.data["changes"].items():
            setattr(ctx.user, k, v)
            if k == "email":
                ctx.user.email_verified = False
        return {"message": ctx.t("msg.profile_saved")}


@register
class LocationUpdate(Action):
    type = "location.update"

    class Payload(Payload):
        lat: Lat | None = None                 # GPS …
        lon: Lon | None = None
        address: TypedAddress | None = None    # … or typed by hand (no GPS, or a wrong fix)

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        if p.address is not None:
            geo = await locate_address(**p.address.model_dump())       # cached: GO's re-validation asks no one again
            if not geo:
                raise AppError("errors.place_not_found")
            place = ", ".join(dict.fromkeys(x for x in (geo["city"], geo["district"], geo["state"]) if x))
            warnings = [ctx.t("warn.location_approx", found=geo["found"])] if geo["approx"] else []
            if ctx.user.role == "farmer":       # typed ≠ GPS-checked: farms see that the distance comes from a typed address
                warnings.append(ctx.t("warn.location_typed_jobs", km=(await get_all(ctx.db))["max_join_radius_km"]))
            return Prepared([ctx.t("sum.location_typed", place=place)], warnings=warnings,
                            data={"geo": geo, "lat": geo["lat"], "lon": geo["lon"], "typed": True})
        if p.lat is None or p.lon is None:
            raise AppError("errors.location_required")
        geo = await check_india(p.lat, p.lon)
        if not geo["ok"]:
            raise AppError("errors.outside_india")
        place = ", ".join(x for x in (geo.get("city"), geo.get("district"), geo.get("state")) if x) or f"{p.lat:.3f}, {p.lon:.3f}"
        return Prepared([ctx.t("sum.location", place=place)], data={"geo": geo, "lat": p.lat, "lon": p.lon, "typed": False})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        d = prep.data
        geo = d["geo"]
        u = ctx.user
        u.lat, u.lon = d["lat"], d["lon"]
        u.location_verified_at = None if d["typed"] else now_utc()
        u.state, u.district, u.city = geo.get("state"), geo.get("district"), geo.get("city")
        return {"message": ctx.t("msg.location_saved")}


@register
class IdDocAdd(Action):
    type = "iddoc.add"

    class Payload(Payload):
        doc_type: Literal[ID_TYPES]  # type: ignore[valid-type]
        number: str = Field(min_length=4, max_length=40)
        upload_id: uuid.UUID | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        n = validate_id_number(p.doc_type, p.number)
        bi = blind_index(n, p.doc_type)
        dup = (await ctx.db.execute(select(IdentityDocument.user_id).where(
            IdentityDocument.doc_type == p.doc_type, IdentityDocument.number_blind_index == bi))).scalar_one_or_none()
        if dup:
            raise Conflict("errors.id_registered" if dup != ctx.user.id else "errors.id_already_added")
        if p.upload_id:
            await owned_upload(ctx.db, ctx.user, p.upload_id, "idproof", unused=True)
        return Prepared([ctx.t("sum.iddoc_add", doc=ctx.t(f"id.{p.doc_type}"), last4=n[-4:])],
                        warnings=[ctx.t("warn.iddoc_private")], data={"n": n, "bi": bi})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        n = prep.data["n"]
        aad = f"iddoc:{ctx.user.id}:{p.doc_type}"
        doc = IdentityDocument(user_id=ctx.user.id, doc_type=p.doc_type, number_enc=cipher.encrypt(n, aad),
                               number_blind_index=prep.data["bi"], number_last4_enc=cipher.encrypt(n[-4:], aad + ":l4"),
                               upload_id=p.upload_id)
        ctx.db.add(doc)
        if p.upload_id:
            up = await owned_upload(ctx.db, ctx.user, p.upload_id, "idproof")
            up.used_for = f"iddoc:{ctx.user.id}"
        return {"message": ctx.t("msg.iddoc_saved")}


@register
class IdDocRemove(Action):
    type = "iddoc.remove"

    class Payload(Payload):
        doc_id: uuid.UUID

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        doc = await ctx.db.get(IdentityDocument, p.doc_id)
        if not doc or doc.user_id != ctx.user.id:
            raise NotFound()
        return Prepared([ctx.t("sum.iddoc_remove", doc=ctx.t(f"id.{doc.doc_type}"))])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        doc = await ctx.db.get(IdentityDocument, p.doc_id)
        await ctx.db.delete(doc)
        return {"message": ctx.t("msg.iddoc_removed")}


@register
class PasswordChange(Action):
    type = "password.change"

    class Payload(Payload):
        current_password: str = Field(min_length=1, max_length=128)
        new_password: str = Field(min_length=6, max_length=128)

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        if not await verify_password(ctx.user.password_hash, p.current_password):
            raise AppError("errors.wrong_password")
        problems = password_problems(p.new_password, ctx.user.role)
        if problems:
            raise AppError(problems[0])
        return Prepared([ctx.t("sum.password_change")], warnings=[ctx.t("warn.other_devices_logout")])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        ctx.user.password_hash = await hash_password(p.new_password)
        await destroy_user_sessions(ctx.user.id, except_hash=ctx.sid_hash, reason="password_changed")
        return {"message": ctx.t("msg.password_changed")}


@register
class BecomeOwner(Action):
    """A farmer (or buyer) also becomes a farm owner: owners need an email and a strong password (a PIN is not enough to
    guard a farm's payroll). A job the person already has continues exactly as before."""
    type = "account.become_owner"
    roles = {"farmer", "buyer"}

    class Payload(Payload):
        email: EmailStr
        current_password: str = Field(min_length=1, max_length=128)
        new_password: str = Field(min_length=10, max_length=128)
        new_password_again: str = Field(min_length=1, max_length=128)
        dob: date | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        from app.models import Farm, FarmMember
        from app.models.farm import WORKER_ROLES

        u = ctx.user
        if not await verify_password(u.password_hash, p.current_password):
            raise AppError("errors.wrong_password")
        if p.new_password != p.new_password_again:
            raise AppError("errors.passwords_differ")
        problems = password_problems(p.new_password, "owner")
        if problems:
            raise AppError(problems[0])
        email = str(p.email).lower()
        if (await ctx.db.execute(select(User.id).where(func.lower(User.email) == email, User.id != u.id))).first():
            raise Conflict("errors.email_taken")
        dob = u.dob or p.dob
        if not dob:
            raise AppError("errors.dob_required")
        min_age = int(await get_setting(ctx.db, "min_worker_age"))
        if (age_on(dob) or 0) < min_age:
            raise AppError("errors.too_young", age=min_age)
        lines = [ctx.t("sum.become_owner"), ctx.t("sum.become_owner_login"), ctx.t("sum.become_owner_email", email=email)]
        jobs = (await ctx.db.execute(select(Farm.name).join(FarmMember, FarmMember.farm_id == Farm.id).where(
            FarmMember.user_id == u.id, FarmMember.status.in_(("active", "notice")), FarmMember.role.in_(WORKER_ROLES)))).scalars().all()
        lines += [ctx.t("sum.become_owner_job", farm=name) for name in jobs]
        return Prepared(lines, warnings=[ctx.t("warn.become_owner_no_apply"), ctx.t("warn.other_devices_logout")],
                        data={"email": email, "dob": dob.isoformat()})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        u = ctx.user
        if (u.email or "").lower() != prep.data["email"]:
            u.email, u.email_verified = prep.data["email"], False
        u.dob = u.dob or date.fromisoformat(prep.data["dob"])
        u.role = "owner"
        u.password_hash = await hash_password(p.new_password)
        await destroy_user_sessions(u.id, except_hash=ctx.sid_hash, reason="password_changed")
        return {"message": ctx.t("msg.became_owner"), "redirect": "/farms/new"}


@register
class SessionsRevoke(Action):
    type = "sessions.revoke_others"

    class Payload(Payload):
        pass

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        return Prepared([ctx.t("sum.sessions_revoke")])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        n = await destroy_user_sessions(ctx.user.id, except_hash=ctx.sid_hash, reason="signed_out_all")
        return {"message": ctx.t("msg.sessions_revoked", count=n)}


@register
class AccountDelete(Action):
    type = "account.delete"

    class Payload(Payload):
        confirm: Literal[True]

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        blockers = await deletion_blockers(ctx.db, ctx.user)
        if blockers:
            raise AppError(blockers[0])
        days = await get_setting(ctx.db, "data_retention_days")
        lines = [ctx.t("sum.delete_1"), ctx.t("sum.delete_2", days=days), ctx.t("sum.delete_3"), ctx.t("sum.delete_4", days=days)]
        return Prepared(lines, warnings=[ctx.t("warn.delete_irreversible", days=days)])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        res = await request_deletion(ctx.db, ctx.user, ctx.ip)
        return {"message": ctx.t("msg.account_deleted", channels=", ".join(res["channels"])), "redirect": "/", "logout": True}
