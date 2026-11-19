"""Superadmin actions (+ the worker grievance report). Every admin change also goes through the GO step."""
from __future__ import annotations

import ipaddress
import uuid
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field
from sqlalchemy import delete, select, update

from app.errors import AppError, Conflict, NotFound
from app.models import (
    Branch, Farm, FarmMember, GstinVerification, Grievance, Halt, Holiday, IpBan, MinimumWage, SafetyCase, UnbanRequest, User,
    UserSanction,
)
from app.models.welfare import GRIEVANCE_CATEGORIES
from app.services.actions.base import Action, Ctx, Payload, Prepared, register
from app.services.actions.fields import LongText, PositiveMoney, ShortText
from app.services.ai.moderation import ban_ip, clear_strikes, lift_ip
from app.services.audit import audit
from app.services.deletion import request_deletion
from app.services.halts import ensure_halt, lift_halts
from app.services.india_data import STATES
from app.services.notify import notify
from app.services.payroll import total_farm_due
from app.services.perms import has_worker_job
from app.services.settings_store import DEFAULTS, get_setting, invalidate, set_setting
from app.services.shop import cancel_open_orders
from app.services.welfare import (
    ban_user, issue_legal_warning, mark_report, order_compensation, report_grievance, resolve_case, unban_user,
)
from app.utils import format_inr, now_utc, today_ist

ADMIN = {"superadmin"}


@register
class GrievanceReport(Action):
    type = "grievance.report"
    roles = {"farmer", "owner"}           # owners only while they still work somewhere (see prepare)
    ai_allowed = True
    ai_hint = ('{"categories": ["low_salary|unpaid_salary|no_water|no_food|no_breaks|overwork|abuse|harassment|unsafe|'
               'child_labour|discrimination|other"], "description": str}')

    class Payload(Payload):
        categories: list[Literal[GRIEVANCE_CATEGORIES]] = Field(min_length=1, max_length=8)  # type: ignore[valid-type]
        description: LongText | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        if ctx.user.role != "farmer" and not await has_worker_job(ctx.db, ctx.user.id):
            raise AppError("errors.role_not_allowed", status=403)
        cats = ", ".join(ctx.t(f"grievance.{c}") for c in p.categories)
        return Prepared([ctx.t("sum.grievance_1", cats=cats), ctx.t("sum.grievance_2"), ctx.t("sum.grievance_3")])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        res = await report_grievance(ctx.db, ctx.user, list(p.categories), p.description, "ai_chat" if ctx.source == "ai" else "form")
        return {"message": ctx.t("msg.grievance_sent"), **res}


# ------------------------------------------------------------------ farms
class _FarmTarget(Payload):
    farm_id: uuid.UUID


@register
class AdminFarmApprove(Action):
    type = "admin.farm.approve"
    roles = ADMIN

    class Payload(_FarmTarget):
        pass

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        f = await ctx.db.get(Farm, p.farm_id)
        if not f or f.status != "pending_approval":
            raise Conflict("errors.no_changes")
        return Prepared([ctx.t("sum.admin_approve", farm=f.name)])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        f = await ctx.db.get(Farm, p.farm_id)
        f.status, f.approved_at, f.approved_by_id = "active", now_utc(), ctx.user.id
        await notify(ctx.db, f.owner_id, "farm_approved", title_key="notif.farm_approved.title", body_key="notif.farm_approved.body",
                     params={"farm": f.name}, link=f"/farms/{f.id}", severity="success", channels=("email", "sms"))
        return {"message": ctx.t("msg.saved")}


@register
class AdminFarmReject(Action):
    type = "admin.farm.reject"
    roles = ADMIN

    class Payload(_FarmTarget):
        reason: ShortText

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        f = await ctx.db.get(Farm, p.farm_id)
        if not f or f.status != "pending_approval":
            raise Conflict("errors.no_changes")
        return Prepared([ctx.t("sum.admin_reject", farm=f.name, reason=p.reason)])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        f = await ctx.db.get(Farm, p.farm_id)
        f.status, f.rejection_reason = "rejected", p.reason
        v = await ctx.db.get(GstinVerification, f.gstin_verification_id)
        if v and v.status == "verified":    # a rejected farm (e.g. someone else's GSTIN) no longer blocks its real owner
            v.status, v.reject_reason = "rejected", "farm_rejected"
        await notify(ctx.db, f.owner_id, "farm_rejected", title_key="notif.farm_rejected.title", body_key="notif.farm_rejected.body",
                     params={"farm": f.name, "reason": p.reason}, severity="danger", channels=("email",))
        return {"message": ctx.t("msg.saved")}


@register
class AdminFarmBan(Action):
    type = "admin.farm.ban"
    roles = ADMIN

    class Payload(_FarmTarget):
        reason: ShortText
        unban: bool = False

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        f = await ctx.db.get(Farm, p.farm_id)
        if not f:
            raise NotFound()
        if p.unban:
            if f.status != "banned":
                raise Conflict("errors.no_changes")
            return Prepared([ctx.t("sum.admin_farm_unban", farm=f.name)])
        if f.status == "banned":
            raise Conflict("errors.no_changes")
        due = await total_farm_due(ctx.db, f.id)
        return Prepared([ctx.t("sum.admin_farm_ban", farm=f.name, reason=p.reason), ctx.t("sum.admin_ban_dues", due=format_inr(due))],
                        warnings=[ctx.t("warn.ban_workers_protected")])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        f = await ctx.db.get(Farm, p.farm_id)
        if p.unban:
            f.status, f.banned_at, f.ban_reason = "active", None, None
            await lift_halts(ctx.db, f.id, "admin_ban", by=ctx.user.id, note=p.reason)
        else:
            f.status, f.banned_at, f.ban_reason = "banned", now_utc(), p.reason
            await ensure_halt(ctx.db, f.id, None, "admin_ban", params={"farm": f.name}, by=ctx.user.id)
            await cancel_open_orders(ctx.db, farm_id=f.id, branch_id=None, reason_key="order.cancel_reason.banned",
                                     actor_id=ctx.user.id, farm_name=f.name)
        await notify(ctx.db, f.owner_id, "farm_ban", title_key="notif.farm_unbanned.title" if p.unban else "notif.farm_banned.title",
                     body_key="notif.farm_unbanned.body" if p.unban else "notif.farm_banned.body",
                     params={"farm": f.name, "reason": p.reason}, severity="success" if p.unban else "danger", channels=("email", "sms"))
        return {"message": ctx.t("msg.saved")}


@register
class AdminFarmDelete(Action):
    type = "admin.farm.delete"
    roles = ADMIN

    class Payload(_FarmTarget):
        reason: ShortText

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        f = await ctx.db.get(Farm, p.farm_id)
        if not f or f.status == "closed":
            raise NotFound()
        due = await total_farm_due(ctx.db, f.id)
        if due > 0:
            raise AppError("errors.admin_delete_dues", due=format_inr(due))
        return Prepared([ctx.t("sum.admin_farm_delete", farm=f.name), ctx.t("sum.admin_records_kept")])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        f = await ctx.db.get(Farm, p.farm_id)
        await cancel_open_orders(ctx.db, farm_id=f.id, branch_id=None, reason_key="order.cancel_reason.closed",
                                 actor_id=ctx.user.id, farm_name=f.name)
        today = today_ist()                 # business dates are Indian dates (UTC is still 'yesterday' before 05:30)
        await ctx.db.execute(update(FarmMember).where(FarmMember.farm_id == f.id, FarmMember.status != "ended")
                             .values(status="ended", ended_on=today, end_reason="farm_deleted"))
        await ctx.db.execute(update(Branch).where(Branch.farm_id == f.id).values(status="closed", closed_at=now_utc(), hiring_open=False))
        f.status, f.closed_at, f.ban_reason = "closed", now_utc(), p.reason
        return {"message": ctx.t("msg.saved")}


# ------------------------------------------------------------------ users
class _UserTarget(Payload):
    user_id: uuid.UUID


@register
class AdminUserBan(Action):
    type = "admin.user.ban"
    roles = ADMIN

    class Payload(_UserTarget):
        reason: ShortText

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        u = await ctx.db.get(User, p.user_id)
        if not u or u.status != "active" or u.is_admin:
            raise Conflict("errors.no_changes")
        return Prepared([ctx.t("sum.admin_user_ban", name=u.full_name, reason=p.reason)],
                        warnings=[ctx.t("warn.ban_workers_protected")] if u.role == "owner" else [ctx.t("warn.ban_worker_paid")])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        await ban_user(ctx.db, await ctx.db.get(User, p.user_id), p.reason, by=ctx.user)
        return {"message": ctx.t("msg.saved")}


@register
class AdminUserUnban(Action):
    type = "admin.user.unban"
    roles = ADMIN

    class Payload(_UserTarget):
        note: ShortText

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        u = await ctx.db.get(User, p.user_id)
        if not u or u.status != "banned":
            raise Conflict("errors.no_changes")
        return Prepared([ctx.t("sum.admin_user_unban", name=u.full_name)])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        await unban_user(ctx.db, await ctx.db.get(User, p.user_id), by=ctx.user, note=p.note)
        return {"message": ctx.t("msg.saved")}


@register
class AdminUserDelete(Action):
    type = "admin.user.delete"
    roles = ADMIN

    class Payload(_UserTarget):
        reason: ShortText

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        u = await ctx.db.get(User, p.user_id)
        if not u or u.status == "deleted" or u.is_admin:
            raise Conflict("errors.no_changes")
        farms = (await ctx.db.execute(select(Farm.id).where(Farm.owner_id == u.id, Farm.status != "closed"))).scalars().all()
        total = Decimal("0")
        for fid in farms:  # an owner typically has few farms
            total += await total_farm_due(ctx.db, fid)
        if total > 0:
            raise AppError("errors.admin_delete_dues", due=format_inr(total))
        return Prepared([ctx.t("sum.admin_user_delete", name=u.full_name), ctx.t("sum.admin_records_kept")])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        u = await ctx.db.get(User, p.user_id)
        farms = (await ctx.db.execute(select(Farm).where(Farm.owner_id == u.id, Farm.status != "closed"))).scalars().all()
        for f in farms:
            f.status, f.closed_at = "closed", now_utc()
            await cancel_open_orders(ctx.db, farm_id=f.id, branch_id=None, reason_key="order.cancel_reason.closed",
                                     actor_id=ctx.user.id, farm_name=f.name)
        await ctx.db.execute(update(FarmMember).where(FarmMember.user_id == u.id, FarmMember.status != "ended")
                             .values(status="ended", ended_on=today_ist(), end_reason="account_deleted"))
        await request_deletion(ctx.db, u, ctx.ip)
        return {"message": ctx.t("msg.saved")}


# ------------------------------------------------------------------ welfare decisions
@register
class AdminGrievanceMark(Action):
    type = "admin.grievance.mark"
    roles = ADMIN

    class Payload(Payload):
        grievance_id: uuid.UUID
        verdict: Literal["valid", "false", "resolved"]
        note: ShortText | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        g = await ctx.db.get(Grievance, p.grievance_id)
        if not g:
            raise NotFound()
        reporter = await ctx.db.get(User, g.reporter_id)
        lines = [ctx.t("sum.grievance_mark", verdict=ctx.t(f"verdict.{p.verdict}"))]
        warnings = []
        if p.verdict == "false":
            limit = await get_setting(ctx.db, "false_report_ban_threshold")
            n = reporter.false_report_warnings + 1
            warnings.append(ctx.t("warn.false_report", n=n, limit=limit))
        return Prepared(lines, warnings=warnings)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        res = await mark_report(ctx.db, ctx.user, p.grievance_id, p.verdict, p.note)
        return {"message": ctx.t("msg.saved"), **res}


class _CaseTarget(Payload):
    case_id: uuid.UUID


async def _case(ctx: Ctx, case_id: uuid.UUID) -> SafetyCase:
    c = await ctx.db.get(SafetyCase, case_id)
    if not c:
        raise NotFound()
    return c


@register
class AdminLegalWarning(Action):
    type = "admin.case.legal_warning"
    roles = ADMIN

    class Payload(_CaseTarget):
        text: str = Field(min_length=20, max_length=5000)

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        c = await _case(ctx, p.case_id)
        farm = await ctx.db.get(Farm, c.farm_id)
        return Prepared([ctx.t("sum.legal_warning", farm=farm.name)])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        await issue_legal_warning(ctx.db, ctx.user, await _case(ctx, p.case_id), p.text)
        return {"message": ctx.t("msg.saved")}


@register
class AdminCompensation(Action):
    type = "admin.case.compensation"
    roles = ADMIN

    class Payload(_CaseTarget):
        per_worker: PositiveMoney

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        c = await _case(ctx, p.case_id)
        farm = await ctx.db.get(Farm, c.farm_id)
        return Prepared([ctx.t("sum.compensation", farm=farm.name, amount=format_inr(p.per_worker)), ctx.t("sum.compensation_all")])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        n = await order_compensation(ctx.db, ctx.user, await _case(ctx, p.case_id), p.per_worker)
        return {"message": ctx.t("msg.compensation_ordered", n=n)}


@register
class AdminCaseResolve(Action):
    type = "admin.case.resolve"
    roles = ADMIN

    class Payload(_CaseTarget):
        note: ShortText

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        c = await _case(ctx, p.case_id)
        farm = await ctx.db.get(Farm, c.farm_id)
        return Prepared([ctx.t("sum.case_resolve", farm=farm.name)])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        await resolve_case(ctx.db, ctx.user, await _case(ctx, p.case_id), p.note)
        return {"message": ctx.t("msg.saved")}


@register
class AdminUnbanDecide(Action):
    type = "admin.unban.decide"
    roles = ADMIN

    class Payload(Payload):
        request_id: uuid.UUID
        approve: bool
        note: ShortText

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        r = await ctx.db.get(UnbanRequest, p.request_id)
        if not r or r.status != "pending":
            raise Conflict("errors.no_changes")
        u = await ctx.db.get(User, r.user_id)
        return Prepared([ctx.t("sum.unban_decide", name=u.full_name, decision=ctx.t("common.approve" if p.approve else "common.reject"))])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        r = await ctx.db.get(UnbanRequest, p.request_id)
        r.status, r.decided_by_id, r.decision_note, r.decided_at = ("approved" if p.approve else "rejected"), ctx.user.id, p.note, now_utc()
        u = await ctx.db.get(User, r.user_id)
        if p.approve and u.status == "banned":
            await unban_user(ctx.db, u, by=ctx.user, note=p.note)
        else:
            await notify(ctx.db, u.id, "unban_rejected", title_key="notif.unban_rejected.title", body_key="notif.unban_rejected.body",
                         params={"note": p.note}, severity="warning")
        return {"message": ctx.t("msg.saved")}


# ------------------------------------------------------------------ platform settings
def _validate_setting(key: str, value: Any) -> Any:
    ints = {"billing_day": (1, 28), "salary_warning_days": (0, 15), "salary_grace_days": (0, 15), "notice_days": (30, 120),
            "max_work_hours": (1, 8), "holiday_premium_pct": (10, 100), "max_join_radius_km": (1, 30),
            "calamity_radius_km": (50, 500), "bad_report_threshold": (2, 20), "bad_report_window_days": (1, 60),
            "false_report_ban_threshold": (1, 10), "min_worker_age": (18, 25), "data_retention_days": (30, 365),
            "compensation_default_days": (1, 60), "insurance_expiry_warning_days": (1, 60), "scheme_review_days": (30, 730)}
    if key in ints:
        lo, hi = ints[key]
        v = int(value)
        if not lo <= v <= hi:
            raise AppError("errors.setting_range", lo=lo, hi=hi)
        return v
    if key == "national_floor_wage":
        v = Decimal(str(value))
        if v < 100 or v > 5000:
            raise AppError("errors.setting_range", lo=100, hi=5000)
        return float(v)
    if key == "tax":
        if not isinstance(value, dict) or not isinstance(value.get("slabs"), list) or not value["slabs"]:
            raise AppError("errors.invalid_input")
        prev = 0
        for upto, pct in value["slabs"]:
            if not 0 <= float(pct) <= 50 or (upto is not None and float(upto) <= prev):
                raise AppError("errors.invalid_input")
            prev = float(upto) if upto is not None else prev
        for k in ("standard_deduction", "rebate_limit", "rebate_max", "cess_pct"):
            if float(value.get(k, 0)) < 0:
                raise AppError("errors.invalid_input")
        return value
    if key == "authentic_job_sites":
        sites = [str(s).strip().lower() for s in value if str(s).strip()]
        if not sites or any(" " in s or "/" in s for s in sites):
            raise AppError("errors.invalid_input")
        return sites[:30]
    if key == "scheme_domains":
        import re

        doms = [str(d).strip().lower().lstrip(".") for d in (value if isinstance(value, list) else []) if str(d).strip()]
        if not doms or any(not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", d) for d in doms):
            raise AppError("errors.invalid_input")
        return list(dict.fromkeys(doms))[:40]
    if key == "approval_sla_hours":
        return str(value)[:20]
    raise AppError("errors.unknown_setting")


@register
class AdminSettingUpdate(Action):
    type = "admin.settings.update"
    roles = ADMIN

    class Payload(Payload):
        key: str = Field(max_length=64)
        value: Any

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        if p.key not in DEFAULTS:
            raise AppError("errors.unknown_setting")
        new = _validate_setting(p.key, p.value)
        old = await get_setting(ctx.db, p.key)
        if old == new:
            raise AppError("errors.no_changes")
        return Prepared([ctx.t("sum.setting_change", field=ctx.t(f"setting.{p.key}"), old=old, new=new)], data={"new": new})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        await set_setting(ctx.db, p.key, prep.data["new"], ctx.user.id)
        invalidate()
        from app.realtime import after_commit
        from app.services.settings_store import bump_version

        after_commit(ctx.db, bump_version)   # other worker processes reload too
        return {"message": ctx.t("msg.saved")}


@register
class AdminMinWage(Action):
    type = "admin.minwage.set"
    roles = ADMIN

    class Payload(Payload):
        state: str = Field(max_length=64)
        daily_wage: Decimal | None = Field(default=None, ge=100, le=5000)
        effective_from: date | None = None
        note: ShortText | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        if p.state not in STATES:
            raise AppError("errors.invalid_input")
        return Prepared([ctx.t("sum.minwage", state=p.state, wage=format_inr(p.daily_wage) if p.daily_wage else ctx.t("common.floor_only"))])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        await ctx.db.execute(pg_insert(MinimumWage).values(id=uuid.uuid4(), state=p.state, daily_wage=p.daily_wage,
                                                           effective_from=p.effective_from, note=p.note, updated_at=now_utc())
                             .on_conflict_do_update(index_elements=["state"], set_={"daily_wage": p.daily_wage,
                                                                                    "effective_from": p.effective_from,
                                                                                    "note": p.note, "updated_at": now_utc()}))
        return {"message": ctx.t("msg.saved")}


@register
class AdminHoliday(Action):
    type = "admin.holiday"
    roles = ADMIN

    class Payload(Payload):
        op: Literal["add", "remove"]
        day: date | None = None
        name: str | None = Field(default=None, max_length=120)
        state: str | None = Field(default=None, max_length=64)
        holiday_id: uuid.UUID | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        if p.op == "add":
            if not p.day or not p.name or (p.state and p.state not in STATES):
                raise AppError("errors.invalid_input")
            return Prepared([ctx.t("sum.holiday_add", day=p.day.isoformat(), name=p.name, state=p.state or ctx.t("common.all_india"))])
        h = await ctx.db.get(Holiday, p.holiday_id) if p.holiday_id else None
        if not h:
            raise NotFound()
        return Prepared([ctx.t("sum.holiday_remove", day=h.day.isoformat(), name=h.name)])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        if p.op == "add":
            ctx.db.add(Holiday(day=p.day, name=p.name, state=p.state))
        else:
            await ctx.db.execute(delete(Holiday).where(Holiday.id == p.holiday_id))
        return {"message": ctx.t("msg.saved")}


@register
class AdminIp(Action):
    type = "admin.ip"
    roles = ADMIN

    class Payload(Payload):
        op: Literal["ban", "lift"]
        ip: str | None = Field(default=None, max_length=64)
        minutes: int | None = Field(default=None, ge=1, le=525600)
        reason: ShortText | None = None
        ban_id: uuid.UUID | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        if p.op == "ban":
            try:
                ipaddress.ip_address(p.ip or "")
            except ValueError as exc:
                raise AppError("errors.invalid_input") from exc
            if p.ip == ctx.ip:
                raise AppError("errors.cannot_ban_self")
            from app.services.ai.moderation import accounts_on_ip, protected_ip

            if protected_ip(p.ip):        # loopback / our own reverse proxies: banning them would lock everyone out
                raise AppError("errors.ip_protected")
            n = await accounts_on_ip(p.ip)
            return Prepared([ctx.t("sum.ip_ban", ip=p.ip, minutes=p.minutes or "∞")],
                            warnings=[ctx.t("warn.ip_shared", n=n)] if n >= 5 else [])
        b = await ctx.db.get(IpBan, p.ban_id) if p.ban_id else None
        if not b or b.lifted_at:
            raise NotFound()
        return Prepared([ctx.t("sum.ip_lift", ip=b.ip), ctx.t("sum.ip_lift_all")])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        if p.op == "ban":
            await ban_ip(p.ip, p.reason or "admin", minutes=p.minutes, source="admin", by=ctx.user.id)
            return {"message": ctx.t("msg.saved")}
        b = await ctx.db.get(IpBan, p.ban_id)
        n = await lift_ip(ctx.db, b.ip, by=ctx.user.id, note=p.reason)
        return {"message": ctx.t("msg.ip_lifted", ip=b.ip, n=n)}


@register
class AdminSanctionLift(Action):
    """Lift an account's posting ban (spam) early."""
    type = "admin.sanction.lift"
    roles = ADMIN

    class Payload(Payload):
        sanction_id: uuid.UUID
        note: ShortText | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        s = await ctx.db.get(UserSanction, p.sanction_id)
        if not s or s.lifted_at:
            raise NotFound()
        u = await ctx.db.get(User, s.user_id)
        return Prepared([ctx.t("sum.sanction_lift", name=u.full_name if u else "?")])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        from app.realtime import after_commit

        s = await ctx.db.get(UserSanction, p.sanction_id)
        # every running posting ban of the account ends (an older, superseded one must not linger as "active")
        await ctx.db.execute(update(UserSanction).where(UserSanction.user_id == s.user_id, UserSanction.lifted_at.is_(None))
                             .values(lifted_at=now_utc(), lifted_by_id=ctx.user.id, lift_note=p.note))
        u = (await ctx.db.execute(select(User).where(User.id == s.user_id).with_for_update())).scalar_one()
        u.posting_banned_until = None
        uid = u.id
        after_commit(ctx.db, lambda: clear_strikes(f"u:{uid}"))
        await notify(ctx.db, u.id, "posting_ban_lifted", title_key="notif.posting_ban_lifted.title",
                     body_key="notif.posting_ban_lifted.body", severity="success")
        return {"message": ctx.t("msg.saved")}


@register
class AdminHaltLift(Action):
    type = "admin.halt.lift"
    roles = ADMIN

    class Payload(Payload):
        halt_id: uuid.UUID
        note: ShortText

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        h = await ctx.db.get(Halt, p.halt_id)
        if not h or h.lifted_at:
            raise NotFound()
        farm = await ctx.db.get(Farm, h.farm_id)
        return Prepared([ctx.t("sum.halt_lift", farm=farm.name, reason=ctx.t(f"halt.reason.{h.reason}"))])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        h = await ctx.db.get(Halt, p.halt_id)
        h.lifted_at, h.lifted_by_id, h.lift_note = now_utc(), ctx.user.id, p.note
        await audit(ctx.db, ctx.user.id, "halt.lift", "halt", h.id, {"reason": h.reason})
        return {"message": ctx.t("msg.saved")}


@register
class AdminRunJob(Action):
    type = "admin.run_job"
    roles = ADMIN

    class Payload(Payload):
        job: Literal["farm_scan", "payroll", "surveys", "closures", "job_hunt", "purge", "digest", "translations", "schemes"]

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        return Prepared([ctx.t("sum.run_job", job=ctx.t(f"job.{p.job}"))])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        import asyncio

        from app.services.scheduler import JOBS

        asyncio.get_running_loop().create_task(JOBS[p.job](force=True))   # an admin asked: run now, whatever the schedule
        return {"message": ctx.t("msg.job_started")}
