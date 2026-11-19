"""Farm, branch, crop, insurance, terms, emergency and closure actions."""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import func, select, update

from app.config import settings
from app.errors import AppError, Conflict, Forbidden, NotFound
from app.models import (
    Branch, BranchCrop, BranchDay, Farm, FarmMember, GstinVerification, InsurancePolicy, Invitation, JobPosting, User,
)
from app.models.farm import FARMING_TYPES, INSURANCE_TYPES
from app.services.actions.base import Action, Ctx, Payload, Prepared, register
from app.services.actions.fields import Acres, LongText, Lat, Lon, Name, Phone, PositiveMoney, ShortText
from app.services.crops import find_crop
from app.services.geo import check_india
from app.services.gstin import verify_via_api
from app.services.halts import ensure_halt, lift_halts
from app.services.notify import notify, notify_admins, notify_many
from app.services.payroll import Calendar, recompute_day, total_farm_due
from app.services.perms import can, coowner_ids, require_perm, roles_by_farm
from app.services.settings_store import get_all, get_setting
from app.services.shop import cancel_open_orders, open_order_count
from app.services.terms import issue_text, review_terms, terms_version_for
from app.services.uploads import owned_upload
from app.utils import format_inr, normalize_phone, now_utc, today_ist

WEEKDAY_KEYS = ["day.mon", "day.tue", "day.wed", "day.thu", "day.fri", "day.sat", "day.sun"]


class CropIn(Payload):
    name: str = Field(min_length=2, max_length=80)
    variety: str | None = Field(default=None, max_length=80)
    area_acres: Acres | None = None
    planted_on: date | None = None


class BranchIn(Payload):
    name: Name
    lat: Lat
    lon: Lon
    address: ShortText | None = None
    area_acres: Acres | None = None
    work_start: time = time(8, 0)
    work_end: time = time(16, 0)
    weekly_offs: list[int] = Field(default_factory=lambda: [6], max_length=3)
    crops: list[CropIn] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def _offs(self):
        if any(d < 0 or d > 6 for d in self.weekly_offs):
            raise ValueError("weekly_offs must be 0..6")
        return self


class InsuranceIn(Payload):
    policy_type: Literal[INSURANCE_TYPES]  # type: ignore[valid-type]
    insurer: str = Field(min_length=2, max_length=120)
    policy_number: str = Field(min_length=3, max_length=64)
    sum_insured: PositiveMoney
    covers_salaries: bool = False
    valid_from: date
    valid_to: date
    upload_id: uuid.UUID


# ------------------------------------------------------------------ helpers
async def _work_hours_ok(ctx: Ctx, start: time, end: time) -> float:
    max_h = float(await get_setting(ctx.db, "max_work_hours"))
    d = date(2000, 1, 1)
    hours = (datetime.combine(d, end) - datetime.combine(d, start)).total_seconds() / 3600
    if hours <= 0:
        raise AppError("errors.work_hours_order")
    if hours > max_h:
        raise AppError("errors.work_hours_max", max=int(max_h))
    return hours


async def _check_insurance(ctx: Ctx, items: list[InsuranceIn], *, mandatory: bool) -> list[str]:
    today = today_ist()
    lines = []
    for it in items:
        if it.valid_to <= it.valid_from:
            raise AppError("errors.insurance_dates")
        if it.valid_to < today or it.valid_from > today + timedelta(days=30):
            raise AppError("errors.insurance_not_current", number=it.policy_number)
        await owned_upload(ctx.db, ctx.user, it.upload_id, "insurance", unused=True)
        lines.append(ctx.t("sum.insurance_item", type=ctx.t(f"insurance.{it.policy_type}"), insurer=it.insurer,
                           number=it.policy_number, amount=format_inr(it.sum_insured), to=it.valid_to.isoformat()))
    if mandatory:
        salary_ok = any(i.policy_type == "salary" or i.covers_salaries for i in items)
        health_ok = any(i.policy_type in ("health", "comprehensive") for i in items)
        if not salary_ok:
            raise AppError("errors.insurance_salary_required")
        if not health_ok:
            raise AppError("errors.insurance_health_required")
    return lines


def _add_policies(ctx: Ctx, farm_id: uuid.UUID, items: list[InsuranceIn], branch_id: uuid.UUID | None = None) -> None:
    for it in items:
        ctx.db.add(InsurancePolicy(farm_id=farm_id, branch_id=branch_id, policy_type=it.policy_type, insurer=it.insurer,
                                   policy_number=it.policy_number, sum_insured=it.sum_insured, covers_salaries=it.covers_salaries,
                                   valid_from=it.valid_from, valid_to=it.valid_to, upload_id=it.upload_id,
                                   status="verified" if settings.TEST_MODE else "pending",
                                   review={"auto": "test_mode"} if settings.TEST_MODE else None, added_by_id=ctx.user.id))


async def _mark_uploads(ctx: Ctx, items: list[InsuranceIn], farm_id: uuid.UUID) -> None:
    for it in items:
        up = await owned_upload(ctx.db, ctx.user, it.upload_id, "insurance")
        up.used_for = f"insurance:{farm_id}"


def _crop_rows(farm_id: uuid.UUID, branch_id: uuid.UUID, crops: list[CropIn], by: uuid.UUID) -> list[BranchCrop]:
    out = []
    for c in crops:
        kb = find_crop(c.name)
        out.append(BranchCrop(farm_id=farm_id, branch_id=branch_id, crop_key=kb.key if kb else None,
                              name=kb.name if kb else c.name.strip().title(), variety=c.variety, area_acres=c.area_acres,
                              planted_on=c.planted_on, added_by_id=by))
    return out


async def _branch_lines(ctx: Ctx, b: BranchIn) -> tuple[list[str], dict]:
    geo = await check_india(b.lat, b.lon)
    if not geo["ok"]:
        raise AppError("errors.outside_india")
    hours = await _work_hours_ok(ctx, b.work_start, b.work_end)
    place = ", ".join(x for x in (geo.get("city"), geo.get("district"), geo.get("state")) if x) or f"{b.lat:.4f}, {b.lon:.4f}"
    offs = ", ".join(ctx.t(WEEKDAY_KEYS[d]) for d in sorted(set(b.weekly_offs))) or ctx.t("common.none")
    lines = [ctx.t("sum.branch", name=b.name, place=place),
             ctx.t("sum.work_hours", start=b.work_start.strftime("%H:%M"), end=b.work_end.strftime("%H:%M"), hours=round(hours, 1)),
             ctx.t("sum.weekly_offs", days=offs),
             ctx.t("sum.crops", crops=", ".join(c.name for c in b.crops))]
    return lines, geo


def _new_branch(farm_id: uuid.UUID, b: BranchIn, geo: dict, is_main: bool) -> Branch:
    return Branch(id=uuid.uuid4(), farm_id=farm_id, name=b.name, is_main=is_main, address=b.address, city=geo.get("city"),
                  district=geo.get("district"), state=geo.get("state"), lat=b.lat, lon=b.lon, area_acres=b.area_acres,
                  work_start=b.work_start, work_end=b.work_end, weekly_offs=sorted(set(b.weekly_offs)))


async def resolve_branch(ctx: Ctx, perm: str, *, branch_id: uuid.UUID | None = None, branch_name: str | None = None,
                         farm_id: uuid.UUID | None = None) -> tuple[Farm, Branch, str]:
    """Find a branch the user may act on — by id, by (fuzzy) name, or the only one available (AI-friendly)."""
    if branch_id:
        b = await ctx.db.get(Branch, branch_id)
        if not b:
            raise NotFound("errors.branch_not_found")
        farm, role = await require_perm(ctx.db, b.farm_id, ctx.user, perm)
        return farm, b, role
    roles = {fid: r for fid, r in (await roles_by_farm(ctx.db, ctx.user)).items() if can(r, perm)}
    if farm_id:
        roles = {k: v for k, v in roles.items() if k == farm_id}
    if not roles:
        raise Forbidden("errors.no_farm_permission")
    stmt = select(Branch).where(Branch.farm_id.in_(list(roles)), Branch.status != "closed")
    branches = list((await ctx.db.execute(stmt)).scalars())
    if branch_name:
        n = branch_name.strip().casefold()
        exact = [b for b in branches if b.name.casefold() == n]
        part = exact or [b for b in branches if n in b.name.casefold() or b.name.casefold() in n]
        branches = part
    if not branches:
        raise NotFound("errors.branch_not_found")
    if len(branches) > 1:
        mains = [b for b in branches if b.is_main] if not branch_name else []
        if len(mains) == 1 and len(roles) == 1:
            branches = mains
        else:
            raise AppError("errors.branch_ambiguous", names=", ".join(b.name for b in branches[:6]))
    b = branches[0]
    farm = await ctx.db.get(Farm, b.farm_id)
    return farm, b, roles[b.farm_id]


def _require_operational(farm: Farm) -> None:
    if farm.status not in ("active",):
        raise AppError("errors.farm_not_active", status=409)


# ------------------------------------------------------------------ GSTIN
@register
class GstinVerify(Action):
    type = "gstin.verify"
    roles = {"owner"}

    class Payload(Payload):
        verification_id: uuid.UUID

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        v = await ctx.db.get(GstinVerification, p.verification_id)
        if not v or v.user_id != ctx.user.id:
            raise NotFound()
        if v.status == "verified":
            raise Conflict("errors.gstin_already_verified")
        taken = (await ctx.db.execute(select(GstinVerification.id).where(
            GstinVerification.gstin == v.gstin, GstinVerification.status == "verified"))).first()
        if taken:
            raise Conflict("errors.gstin_taken")
        mode = ctx.t("sum.gstin_mode_test") if settings.TEST_MODE else ctx.t("sum.gstin_mode_api")
        return Prepared([ctx.t("sum.gstin_verify", gstin=v.gstin), mode])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        v = await ctx.db.get(GstinVerification, p.verification_id)
        res = await verify_via_api(v.gstin)
        v.api_response, v.method = {k: res.get(k) for k in ("method", "status", "legal_name", "error")}, res.get("method")
        if res["ok"]:
            v.status, v.verified_at = "verified", now_utc()
            v.legal_name = v.legal_name or res.get("legal_name")
            return {"message": ctx.t("msg.gstin_verified", gstin=v.gstin), "verified": True}
        v.status, v.reject_reason = "rejected", res.get("error") or res.get("status")
        return {"message": ctx.t("msg.gstin_rejected"), "verified": False}


# ------------------------------------------------------------------ farm lifecycle
@register
class FarmCreate(Action):
    type = "farm.create"
    roles = {"owner"}

    class Payload(Payload):
        gstin_verification_id: uuid.UUID
        name: Name
        description: LongText | None = None
        farming_type: Literal[FARMING_TYPES]  # type: ignore[valid-type]
        hiring_limit: int = Field(ge=1, le=10000)
        pay_day: int = Field(ge=1, le=28)
        payment_method: Literal["upi", "bank", "cash"]
        branch: BranchIn
        insurance: list[InsuranceIn] = Field(min_length=1, max_length=10)
        terms_mode: Literal["boilerplate", "custom"] = "boilerplate"
        terms_text: str | None = Field(default=None, max_length=8000)
        accept_platform_terms: Literal[True]

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        u = ctx.user
        if not u.email:
            raise AppError("errors.owner_email_required")
        if not u.photo_verified:
            raise AppError("errors.photo_required")
        v = await ctx.db.get(GstinVerification, p.gstin_verification_id)
        if not v or v.user_id != u.id or v.status != "verified":
            raise AppError("errors.gstin_not_verified")
        cfg = await get_all(ctx.db)
        if p.pay_day > int(cfg["billing_day"]):
            raise AppError("errors.pay_day_after_billing", billing_day=cfg["billing_day"])
        branch_lines, geo = await _branch_lines(ctx, p.branch)
        ins_lines = await _check_insurance(ctx, p.insurance, mandatory=True)
        review = None
        if p.terms_mode == "custom":
            review = await review_terms(ctx.db, p.terms_text or "")
            if not review["ok"]:
                issues = "; ".join(issue_text(i, ctx.t)[:120] for i in review["issues"][:4])
                raise AppError("errors.terms_unlawful", issues=issues)
        lines = [ctx.t("sum.farm_name", name=p.name), ctx.t("sum.gstin", gstin=v.gstin),
                 ctx.t("sum.farming_type", type=ctx.t(f"farming.{p.farming_type}")),
                 ctx.t("sum.hiring_limit", n=p.hiring_limit),
                 ctx.t("sum.pay_day", day=p.pay_day, method=ctx.t(f"pay.{p.payment_method}"), billing=cfg["billing_day"]),
                 *branch_lines, *ins_lines,
                 ctx.t("sum.terms_custom") if p.terms_mode == "custom" else ctx.t("sum.terms_boilerplate"),
                 ctx.t("sum.approval_test") if settings.TEST_MODE else ctx.t("sum.approval_wait", sla=cfg["approval_sla_hours"])]
        return Prepared(lines, data={"geo": geo, "review": review, "gstin": v.gstin})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        auto = settings.TEST_MODE
        farm = Farm(id=uuid.uuid4(), owner_id=ctx.user.id, gstin_verification_id=p.gstin_verification_id, gstin=prep.data["gstin"],
                    name=p.name, description=p.description, farming_type=p.farming_type,
                    status="active" if auto else "pending_approval", hiring_limit=p.hiring_limit, pay_day=p.pay_day,
                    payment_method=p.payment_method, terms_mode=p.terms_mode,
                    terms_text=p.terms_text if p.terms_mode == "custom" else None,
                    terms_version=terms_version_for(p.terms_mode, p.terms_text), terms_review=prep.data["review"],
                    approval_requested_at=now_utc(), approved_at=now_utc() if auto else None)
        ctx.db.add(farm)
        await ctx.db.flush()
        branch = _new_branch(farm.id, p.branch, prep.data["geo"], True)
        ctx.db.add(branch)
        await ctx.db.flush()
        ctx.db.add_all(_crop_rows(farm.id, branch.id, p.branch.crops, ctx.user.id))
        _add_policies(ctx, farm.id, p.insurance)
        await _mark_uploads(ctx, p.insurance, farm.id)
        if auto:
            await notify(ctx.db, ctx.user.id, "farm_approved", title_key="notif.farm_approved.title", body_key="notif.farm_approved.body",
                         params={"farm": farm.name}, link=f"/farms/{farm.id}", severity="success")
        else:
            await notify_admins(ctx.db, "farm_pending", title_key="notif.farm_pending_admin.title",
                                body_key="notif.farm_pending_admin.body", params={"farm": farm.name, "owner": ctx.user.full_name},
                                link="/admin?tab=farms")
        return {"message": ctx.t("msg.farm_created_auto" if auto else "msg.farm_created_wait", farm=farm.name),
                "redirect": f"/farms/{farm.id}", "farm_id": str(farm.id)}


@register
class FarmRename(Action):
    type = "farm.rename"
    consensus = True

    class Payload(Payload):
        farm_id: uuid.UUID
        name: Name

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        farm, _ = await require_perm(ctx.db, p.farm_id, ctx.user, "settings")
        if farm.name == p.name:
            raise AppError("errors.no_changes")
        return Prepared([ctx.t("sum.rename", old=farm.name, new=p.name)], farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        farm = await ctx.db.get(Farm, p.farm_id)
        farm.name = p.name
        return {"message": ctx.t("msg.saved")}


@register
class FarmSettings(Action):
    type = "farm.update_settings"
    consensus = True

    class Payload(Payload):
        farm_id: uuid.UUID
        hiring_limit: int | None = Field(default=None, ge=1, le=10000)
        pay_day: int | None = Field(default=None, ge=1, le=28)
        payment_method: Literal["upi", "bank", "cash"] | None = None
        description: LongText | None = None
        farming_type: Literal[FARMING_TYPES] | None = None  # type: ignore[valid-type]

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        farm, _ = await require_perm(ctx.db, p.farm_id, ctx.user, "settings")
        changes = {k: v for k, v in p.model_dump(exclude_unset=True).items() if k != "farm_id" and getattr(farm, k) != v}
        if not changes:
            raise AppError("errors.no_changes")
        if "pay_day" in changes and changes["pay_day"] > int(await get_setting(ctx.db, "billing_day")):
            raise AppError("errors.pay_day_after_billing", billing_day=await get_setting(ctx.db, "billing_day"))
        if "hiring_limit" in changes:
            employed = (await ctx.db.execute(select(func.count()).select_from(FarmMember).where(
                FarmMember.farm_id == farm.id, FarmMember.status != "ended", FarmMember.role.in_(("employee", "supervisor"))))).scalar_one()
            if changes["hiring_limit"] < employed:
                raise AppError("errors.hiring_limit_below_staff", n=employed)
        lines = [ctx.t("sum.setting_change", field=ctx.t(f"field.{k}"), old=getattr(farm, k), new=v) for k, v in changes.items()]
        return Prepared(lines, farm_id=farm.id, data={"changes": changes})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        farm = await ctx.db.get(Farm, p.farm_id)
        for k, v in prep.data["changes"].items():
            setattr(farm, k, v)
        return {"message": ctx.t("msg.saved")}


@register
class FarmTerms(Action):
    type = "farm.update_terms"
    consensus = True

    class Payload(Payload):
        farm_id: uuid.UUID
        terms_mode: Literal["boilerplate", "custom"]
        terms_text: str | None = Field(default=None, max_length=8000)

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        farm, _ = await require_perm(ctx.db, p.farm_id, ctx.user, "settings")
        review = None
        if p.terms_mode == "custom":
            review = await review_terms(ctx.db, p.terms_text or "")
            if not review["ok"]:
                raise AppError("errors.terms_unlawful", issues="; ".join(issue_text(i, ctx.t)[:120] for i in review["issues"][:4]))
        return Prepared([ctx.t("sum.terms_custom") if p.terms_mode == "custom" else ctx.t("sum.terms_boilerplate"),
                         ctx.t("sum.terms_new_hires")], farm_id=farm.id, data={"review": review})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        farm = await ctx.db.get(Farm, p.farm_id)
        farm.terms_mode, farm.terms_text = p.terms_mode, (p.terms_text if p.terms_mode == "custom" else None)
        farm.terms_version, farm.terms_review = terms_version_for(p.terms_mode, p.terms_text), prep.data["review"]
        workers = (await ctx.db.execute(select(FarmMember.user_id).where(FarmMember.farm_id == farm.id, FarmMember.status != "ended"))).scalars()
        await notify_many(ctx.db, list(workers), "terms_changed", title_key="notif.terms_changed.title", body_key="notif.terms_changed.body",
                          params={"farm": farm.name}, link=f"/f/{farm.id}#terms")
        return {"message": ctx.t("msg.saved")}


async def _start_closure(ctx: Ctx, farm: Farm, branches: list[Branch], reason: str, closure_date: date, *, whole_farm: bool) -> int:
    branch_ids = [b.id for b in branches]
    for b in branches:
        b.status, b.hiring_open, b.closure_notice_at, b.closure_date = "closing", False, now_utc(), closure_date
    await ctx.db.execute(update(JobPosting).where(JobPosting.branch_id.in_(branch_ids), JobPosting.status == "open").values(status="closed"))
    stmt = select(FarmMember).where(FarmMember.farm_id == farm.id, FarmMember.status == "active")
    if not whole_farm:  # a branch closure affects only that branch's people; a farm closure affects everyone incl. farm-wide roles
        stmt = stmt.where(FarmMember.branch_id.in_(branch_ids))
    members = list((await ctx.db.execute(stmt)).scalars())
    for m in members:
        m.status, m.notice_given_at, m.notice_by_id, m.notice_kind = "notice", now_utc(), ctx.user.id, "closure"
        m.notice_end_date, m.notice_reason = closure_date, reason
    await notify_many(ctx.db, [m.user_id for m in members], "closure_notice", title_key="notif.closure_notice.title",
                      body_key="notif.closure_notice.body", params={"farm": farm.name, "date": closure_date.isoformat()},
                      severity="warning", channels=("sms", "whatsapp"))
    return len(members)


@register
class FarmClose(Action):
    type = "farm.close"
    consensus = True

    class Payload(Payload):
        farm_id: uuid.UUID
        reason: ShortText

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        farm, _ = await require_perm(ctx.db, p.farm_id, ctx.user, "settings")
        if farm.status in ("closing", "closed"):
            raise Conflict("errors.already_closing")
        days = int(await get_setting(ctx.db, "notice_days"))
        cdate = today_ist() + timedelta(days=days)
        due = await total_farm_due(ctx.db, farm.id)
        orders = await open_order_count(ctx.db, farm.id)
        lines = [ctx.t("sum.close_1", farm=farm.name, date=cdate.isoformat(), days=days), ctx.t("sum.close_2"),
                 ctx.t("sum.close_3", date=(cdate - timedelta(days=1)).isoformat()),
                 ctx.t("sum.close_4", due=format_inr(due), orders=orders)]
        return Prepared(lines, warnings=[ctx.t("warn.close_dues")], farm_id=farm.id, data={"date": cdate.isoformat()})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        farm = await ctx.db.get(Farm, p.farm_id)
        cdate = date.fromisoformat(prep.data["date"])
        branches = list((await ctx.db.execute(select(Branch).where(Branch.farm_id == farm.id, Branch.status != "closed"))).scalars())
        n = await _start_closure(ctx, farm, branches, p.reason, cdate, whole_farm=True)
        farm.status, farm.closure_notice_at, farm.closure_date = "closing", now_utc(), cdate
        await notify_admins(ctx.db, "farm_closing", title_key="notif.farm_closing_admin.title", body_key="notif.farm_closing_admin.body",
                            params={"farm": farm.name, "date": cdate.isoformat(), "workers": n}, link="/admin?tab=farms")
        return {"message": ctx.t("msg.closure_started", date=cdate.isoformat(), n=n)}


# ------------------------------------------------------------------ branches
@register
class BranchCreate(Action):
    type = "branch.create"

    class Payload(Payload):
        farm_id: uuid.UUID
        branch: BranchIn

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        farm, _ = await require_perm(ctx.db, p.farm_id, ctx.user, "settings")
        _require_operational(farm)
        lines, geo = await _branch_lines(ctx, p.branch)
        return Prepared([ctx.t("sum.branch_new")] + lines, farm_id=farm.id, data={"geo": geo})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        b = _new_branch(p.farm_id, p.branch, prep.data["geo"], False)
        ctx.db.add(b)
        await ctx.db.flush()
        ctx.db.add_all(_crop_rows(p.farm_id, b.id, p.branch.crops, ctx.user.id))
        return {"message": ctx.t("msg.branch_created", name=b.name), "branch_id": str(b.id)}


@register
class BranchUpdate(Action):
    type = "branch.update"

    class Payload(Payload):
        branch_id: uuid.UUID
        name: Name | None = None
        address: ShortText | None = None
        area_acres: Acres | None = None
        work_start: time | None = None
        work_end: time | None = None
        weekly_offs: list[int] | None = Field(default=None, max_length=3)
        hiring_open: bool | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        b = await ctx.db.get(Branch, p.branch_id)
        if not b:
            raise NotFound("errors.branch_not_found")
        farm, _ = await require_perm(ctx.db, b.farm_id, ctx.user, "members")
        changes = {k: v for k, v in p.model_dump(exclude_unset=True).items() if k != "branch_id" and getattr(b, k) != v}
        if not changes:
            raise AppError("errors.no_changes")
        if "work_start" in changes or "work_end" in changes:
            await _work_hours_ok(ctx, changes.get("work_start", b.work_start), changes.get("work_end", b.work_end))
        if changes.get("hiring_open") and b.status != "active":
            raise AppError("errors.branch_closing")
        lines = [ctx.t("sum.setting_change", field=ctx.t(f"field.{k}"), old=getattr(b, k), new=v) for k, v in changes.items()]
        return Prepared(lines, farm_id=farm.id, data={"changes": {k: (v.isoformat() if isinstance(v, time) else v) for k, v in changes.items()}})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        b = await ctx.db.get(Branch, p.branch_id)
        for k, v in prep.data["changes"].items():
            setattr(b, k, time.fromisoformat(v) if k in ("work_start", "work_end") else (sorted(set(v)) if k == "weekly_offs" else v))
        return {"message": ctx.t("msg.saved")}


@register
class BranchClose(Action):
    type = "branch.close"
    consensus = True

    class Payload(Payload):
        branch_id: uuid.UUID
        reason: ShortText

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        b = await ctx.db.get(Branch, p.branch_id)
        if not b:
            raise NotFound("errors.branch_not_found")
        farm, _ = await require_perm(ctx.db, b.farm_id, ctx.user, "settings")
        if b.is_main:
            raise AppError("errors.main_branch_close")
        if b.status != "active":
            raise Conflict("errors.already_closing")
        days = int(await get_setting(ctx.db, "notice_days"))
        cdate = today_ist() + timedelta(days=days)
        due = await total_farm_due(ctx.db, farm.id, b.id)
        orders = await open_order_count(ctx.db, farm.id, b.id)
        return Prepared([ctx.t("sum.close_1", farm=b.name, date=cdate.isoformat(), days=days), ctx.t("sum.close_2"),
                         ctx.t("sum.close_3", date=(cdate - timedelta(days=1)).isoformat()),
                         ctx.t("sum.close_4", due=format_inr(due), orders=orders)],
                        farm_id=farm.id, data={"date": cdate.isoformat()})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        b = await ctx.db.get(Branch, p.branch_id)
        farm = await ctx.db.get(Farm, b.farm_id)
        cdate = date.fromisoformat(prep.data["date"])
        n = await _start_closure(ctx, farm, [b], p.reason, cdate, whole_farm=False)
        return {"message": ctx.t("msg.closure_started", date=cdate.isoformat(), n=n)}


EMERGENCY_KINDS = ("earthquake", "flood", "cyclone", "storm", "fire", "landslide", "drought", "epidemic", "other")


@register
class BranchEmergency(Action):
    type = "branch.emergency"
    ai_allowed = True
    ai_hint = '{"branch_name": str|null (null = whole farm), "kind": "earthquake|flood|cyclone|storm|fire|landslide|drought|epidemic|other", "note": str}'

    class Payload(Payload):
        farm_id: uuid.UUID | None = None
        branch_id: uuid.UUID | None = None
        branch_name: str | None = Field(default=None, max_length=120)
        whole_farm: bool = False
        kind: Literal[EMERGENCY_KINDS]  # type: ignore[valid-type]
        note: ShortText | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        if p.whole_farm or (not p.branch_id and not p.branch_name and p.farm_id):
            farm, _ = await require_perm(ctx.db, p.farm_id, ctx.user, "emergency") if p.farm_id else (None, None)
            if farm is None:
                farm, b, _ = await resolve_branch(ctx, "emergency")
            target, bid = farm.name, None
        else:
            farm, b, _ = await resolve_branch(ctx, "emergency", branch_id=p.branch_id, branch_name=p.branch_name, farm_id=p.farm_id)
            target, bid = b.name, b.id
        orders = await open_order_count(ctx.db, farm.id, bid)
        lines = [ctx.t("sum.emergency_1", kind=ctx.t(f"emergency.{p.kind}"), target=target), ctx.t("sum.emergency_2", orders=orders),
                 ctx.t("sum.emergency_3"), ctx.t("sum.emergency_4")]
        return Prepared(lines, farm_id=farm.id, data={"farm_id": str(farm.id), "branch_id": str(bid) if bid else None, "target": target})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        farm = await ctx.db.get(Farm, uuid.UUID(prep.data["farm_id"]))
        bid = uuid.UUID(prep.data["branch_id"]) if prep.data["branch_id"] else None
        await ensure_halt(ctx.db, farm.id, bid, "emergency", params={"kind": p.kind, "target": prep.data["target"]},
                          details={"note": p.note}, by=ctx.user.id)
        n = await cancel_open_orders(ctx.db, farm_id=farm.id, branch_id=bid, reason_key="order.cancel_reason.emergency",
                                     actor_id=ctx.user.id, farm_name=farm.name)
        stmt = select(FarmMember.user_id).where(FarmMember.farm_id == farm.id, FarmMember.status != "ended")
        if bid:
            stmt = stmt.where(FarmMember.branch_id == bid)
        await notify_many(ctx.db, list((await ctx.db.execute(stmt)).scalars()), "emergency", title_key="notif.emergency_worker.title",
                          body_key="notif.emergency_worker.body", params={"kind": p.kind, "target": prep.data["target"]},
                          severity="danger", channels=("sms", "whatsapp"))
        await notify_admins(ctx.db, "emergency", title_key="notif.emergency_admin.title", body_key="notif.emergency_admin.body",
                            params={"farm": farm.name, "target": prep.data["target"], "kind": p.kind, "note": p.note or ""})
        return {"message": ctx.t("msg.emergency_declared", orders=n)}


@register
class BranchResume(Action):
    type = "branch.resume"
    ai_allowed = True
    ai_hint = '{"branch_name": str|null (null = whole farm)}'

    class Payload(Payload):
        farm_id: uuid.UUID | None = None
        branch_id: uuid.UUID | None = None
        branch_name: str | None = Field(default=None, max_length=120)

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        if p.branch_id or p.branch_name:
            farm, b, _ = await resolve_branch(ctx, "emergency", branch_id=p.branch_id, branch_name=p.branch_name, farm_id=p.farm_id)
            bid, target = b.id, b.name
        else:
            farm, _ = await require_perm(ctx.db, p.farm_id, ctx.user, "emergency") if p.farm_id else (None, None)
            if farm is None:
                farm, _b, _ = await resolve_branch(ctx, "emergency")
            bid, target = None, farm.name
        return Prepared([ctx.t("sum.resume", target=target)], farm_id=farm.id,
                        data={"farm_id": str(farm.id), "branch_id": str(bid) if bid else None})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        fid = uuid.UUID(prep.data["farm_id"])
        bid = uuid.UUID(prep.data["branch_id"]) if prep.data["branch_id"] else None
        n = await lift_halts(ctx.db, fid, "emergency", branch_id=bid, by=ctx.user.id, note="resumed by farm")
        return {"message": ctx.t("msg.resumed", n=n)}


@register
class BranchMarkDay(Action):
    type = "branch.mark_day"

    class Payload(Payload):
        branch_id: uuid.UUID
        day: date
        is_open: bool

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        b = await ctx.db.get(Branch, p.branch_id)
        if not b:
            raise NotFound("errors.branch_not_found")
        farm, _ = await require_perm(ctx.db, b.farm_id, ctx.user, "members")
        today = today_ist()
        if not (today - timedelta(days=7) <= p.day <= today + timedelta(days=90)):
            raise AppError("errors.day_out_of_range")
        cal = await Calendar.load(ctx.db, [b], p.day, p.day)
        holiday = cal.is_holiday(b, p.day)
        pct = await get_setting(ctx.db, "holiday_premium_pct")
        lines = [ctx.t("sum.mark_day_open" if p.is_open else "sum.mark_day_closed", branch=b.name, day=p.day.isoformat())]
        if holiday and p.is_open:
            lines.append(ctx.t("sum.holiday_premium", pct=pct))
        elif not holiday and not p.is_open:
            lines.append(ctx.t("sum.closed_still_paid"))
        return Prepared(lines, farm_id=farm.id, data={"holiday": holiday})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        b = await ctx.db.get(Branch, p.branch_id)
        await ctx.db.execute(pg_insert(BranchDay).values(id=uuid.uuid4(), branch_id=b.id, day=p.day, is_holiday=prep.data["holiday"],
                                                         is_open=p.is_open, source="owner", marked_by_id=ctx.user.id)
                             .on_conflict_do_update(index_elements=["branch_id", "day"],
                                                    set_={"is_open": p.is_open, "source": "owner", "marked_by_id": ctx.user.id}))
        await ctx.db.flush()
        if p.day <= today_ist():
            await recompute_day(ctx.db, b, p.day)
        return {"message": ctx.t("msg.saved")}


# ------------------------------------------------------------------ crops
@register
class CropAdd(Action):
    type = "crop.add"
    ai_allowed = True
    ai_hint = '{"branch_name": str|null, "crop": str, "variety": str|null, "area_acres": number|null, "planted_on": "YYYY-MM-DD"|null}'

    class Payload(Payload):
        farm_id: uuid.UUID | None = None
        branch_id: uuid.UUID | None = None
        branch_name: str | None = Field(default=None, max_length=120)
        crop: str = Field(min_length=2, max_length=80)
        variety: str | None = Field(default=None, max_length=80)
        area_acres: Acres | None = None
        planted_on: date | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        farm, b, _ = await resolve_branch(ctx, "crops", branch_id=p.branch_id, branch_name=p.branch_name, farm_id=p.farm_id)
        if p.planted_on and p.planted_on > today_ist() + timedelta(days=1):
            raise AppError("errors.planted_future")
        kb = find_crop(p.crop)
        name = kb.name if kb else p.crop.strip().title()
        lines = [ctx.t("sum.crop_add", crop=name, branch=b.name, farm=farm.name)]
        warnings = [] if kb else [ctx.t("warn.crop_unknown")]
        return Prepared(lines, warnings=warnings, farm_id=farm.id, data={"branch_id": str(b.id), "farm_id": str(farm.id)})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        ctx.db.add_all(_crop_rows(uuid.UUID(prep.data["farm_id"]), uuid.UUID(prep.data["branch_id"]),
                                  [CropIn(name=p.crop, variety=p.variety, area_acres=p.area_acres, planted_on=p.planted_on)], ctx.user.id))
        return {"message": ctx.t("msg.crop_added", crop=p.crop)}


@register
class CropRemove(Action):
    type = "crop.remove"
    ai_allowed = True
    ai_hint = '{"branch_name": str|null, "crop": str, "mode": "harvested|removed"}'

    class Payload(Payload):
        crop_id: uuid.UUID | None = None
        farm_id: uuid.UUID | None = None
        branch_id: uuid.UUID | None = None
        branch_name: str | None = Field(default=None, max_length=120)
        crop: str | None = Field(default=None, max_length=80)
        mode: Literal["harvested", "removed"] = "removed"

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        if p.crop_id:
            c = await ctx.db.get(BranchCrop, p.crop_id)
            if not c:
                raise NotFound()
            farm, b, _ = await resolve_branch(ctx, "crops", branch_id=c.branch_id)
        else:
            farm, b, _ = await resolve_branch(ctx, "crops", branch_id=p.branch_id, branch_name=p.branch_name, farm_id=p.farm_id)
            kb = find_crop(p.crop or "")
            rows = (await ctx.db.execute(select(BranchCrop).where(BranchCrop.branch_id == b.id, BranchCrop.status == "growing"))).scalars().all()
            match = [r for r in rows if (kb and r.crop_key == kb.key) or r.name.casefold() == (p.crop or "").casefold()]
            if not match:
                raise NotFound("errors.crop_not_found")
            c = match[0]
        growing = (await ctx.db.execute(select(func.count()).select_from(BranchCrop).where(
            BranchCrop.branch_id == b.id, BranchCrop.status == "growing"))).scalar_one()
        if c.status != "growing":
            raise Conflict("errors.no_changes")
        if growing <= 1:
            raise AppError("errors.crop_min_one")
        return Prepared([ctx.t("sum.crop_remove", crop=c.name, branch=b.name, mode=ctx.t(f"crop.mode.{p.mode}"))],
                        farm_id=farm.id, data={"crop_id": str(c.id)})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        c = await ctx.db.get(BranchCrop, uuid.UUID(prep.data["crop_id"]))
        c.status, c.removed_at = p.mode, now_utc()
        return {"message": ctx.t("msg.saved")}


# ------------------------------------------------------------------ insurance
@register
class InsuranceAdd(Action):
    type = "insurance.add"

    class Payload(Payload):
        farm_id: uuid.UUID
        branch_id: uuid.UUID | None = None
        policy: InsuranceIn

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        farm, _ = await require_perm(ctx.db, p.farm_id, ctx.user, "insurance")
        if p.branch_id:                     # only a branch of THIS farm — a policy never lands on another farm's branch
            b = await ctx.db.get(Branch, p.branch_id)
            if not b or b.farm_id != farm.id:
                raise NotFound("errors.branch_not_found")
        lines = await _check_insurance(ctx, [p.policy], mandatory=False)
        return Prepared([ctx.t("sum.insurance_add")] + lines + [ctx.t("sum.insurance_public")], farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        _add_policies(ctx, p.farm_id, [p.policy], p.branch_id)
        await _mark_uploads(ctx, [p.policy], p.farm_id)
        await ctx.db.flush()
        lifted = 0
        if p.policy.policy_type == "salary" or p.policy.covers_salaries:
            lifted += await lift_halts(ctx.db, p.farm_id, "calamity_no_insurance", by=ctx.user.id, note="salary insurance added")
        from app.services.weather import insurance_state

        st = (await insurance_state(ctx.db, [p.farm_id]))[p.farm_id]
        if st["salary"] and st["health"]:
            lifted += await lift_halts(ctx.db, p.farm_id, "insurance_expired", by=ctx.user.id, note="insurance renewed")
        return {"message": ctx.t("msg.insurance_added", lifted=lifted)}


@register
class CoownerAdd(Action):
    type = "coowner.add"
    consensus = True

    class Payload(Payload):
        farm_id: uuid.UUID
        phone: Phone

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        farm, _ = await require_perm(ctx.db, p.farm_id, ctx.user, "settings")
        phone = normalize_phone(p.phone)
        if not phone:
            raise AppError("errors.phone_invalid")
        u = (await ctx.db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
        if u and (u.id == farm.owner_id or u.id in await coowner_ids(ctx.db, farm.id)):
            raise Conflict("errors.already_member")
        return Prepared([ctx.t("sum.coowner_add", phone=phone, farm=farm.name), ctx.t("sum.coowner_powers")],
                        farm_id=farm.id, data={"phone": phone})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        phone = prep.data["phone"]
        u = (await ctx.db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
        inv = Invitation(farm_id=p.farm_id, phone=phone, invitee_id=u.id if u else None, role="co_owner", salary_type="monthly",
                         salary_amount=Decimal("0"), invited_by_id=ctx.user.id, expires_at=now_utc() + timedelta(days=14))
        ctx.db.add(inv)
        await ctx.db.flush()
        if u:
            farm = await ctx.db.get(Farm, p.farm_id)
            await notify(ctx.db, u.id, "invitation", title_key="notif.invitation.title", body_key="notif.invitation.body",
                         params={"farm": farm.name, "role": "co_owner"}, link="/dashboard#invitations", channels=("sms",))
        return {"message": ctx.t("msg.invited")}


@register
class WeatherScanNow(Action):
    type = "weather.scan"
    ai_allowed = True
    ai_hint = "{}"

    class Payload(Payload):
        farm_id: uuid.UUID | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        roles = {fid: r for fid, r in (await roles_by_farm(ctx.db, ctx.user)).items() if can(r, "crops")}
        ids = [p.farm_id] if p.farm_id and p.farm_id in roles else list(roles)
        if not ids:
            raise Forbidden("errors.no_farm_permission")
        n = (await ctx.db.execute(select(func.count()).select_from(Branch).where(Branch.farm_id.in_(ids), Branch.status != "closed"))).scalar_one()
        return Prepared([ctx.t("sum.weather_scan", n=n)], data={"ids": [str(i) for i in ids]})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        from app.services.weather import scan_farms

        stats = await scan_farms(ctx.db, [uuid.UUID(i) for i in prep.data["ids"]])
        return {"message": ctx.t("msg.weather_scanned", **{k: stats.get(k, 0) for k in ("branches", "alerts", "calamities", "halts")})}
