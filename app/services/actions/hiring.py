"""Hiring & membership: postings, applications (≤30 km), invitations, salaries (owner-set, ≥ minimum wage),
1-month notice (no sudden kick-outs), resignation."""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.errors import AppError, Conflict, Forbidden, NotFound
from app.models import Branch, Farm, FarmMember, Invitation, JobApplication, JobPosting, TermsAcceptance, User
from app.models.farm import JOB_ROLES, WORKER_ROLES
from app.services.actions.base import Action, Ctx, Payload, Prepared, register
from app.services.actions.fields import Lat, Lon, LongText, Name, Phone, PositiveMoney, ShortText
from app.services.geo import check_india, haversine_km
from app.services.halts import is_halted
from app.services.notify import notify, notify_farm_leaders, notify_many
from app.services.payroll import ensure_accruals_for_farm, farm_money
from app.services.perms import RANK, coowner_ids, outranks, require_perm
from app.services.settings_store import get_all
from app.services.terms import strong_flags
from app.services.wages import check_min_wage
from app.utils import age_on, format_inr, normalize_phone, now_utc, today_ist


async def _assert_branch_hiring(ctx: Ctx, farm: Farm, branch: Branch) -> None:
    if farm.status != "active":
        raise AppError("errors.farm_not_active", status=409)
    if branch.status != "active" or not branch.hiring_open:
        raise AppError("errors.hiring_closed", status=409)
    if await is_halted(ctx.db, farm.id, branch.id):
        raise AppError("errors.branch_halted", status=409)
    if farm.terms_mode == "custom" and strong_flags(farm.terms_text or ""):
        raise AppError("errors.farm_terms_blocked", status=409)     # e.g. "pay 1 lakh to join" — fix the terms first


async def _assert_one_working_farm(ctx: Ctx, u: User, *, exclude_farm: uuid.UUID | None = None, own: bool = True) -> None:
    """Work happens on ONE farm at a time — employee, supervisor, manager or accountant. Owning or co-owning farms is not
    work and never counts. During a NOTICE period the person may join a new farm: that job then ends (_leave_notice_jobs).
    `own`: the person themselves is asking (else their future employer sees the refusal)."""
    stmt = select(FarmMember.id).where(FarmMember.user_id == u.id, FarmMember.status == "active", FarmMember.role.in_(JOB_ROLES))
    if exclude_farm:
        stmt = stmt.where(FarmMember.farm_id != exclude_farm)
    if (await ctx.db.execute(stmt.limit(1))).first():
        raise AppError("errors.one_job_at_a_time" if own else "errors.has_job_elsewhere", status=409)


async def _notice_jobs(ctx: Ctx, user_id: uuid.UUID, exclude_farm: uuid.UUID) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    """(member id, farm id, farm name) of the jobs this person is serving notice on, elsewhere."""
    return [tuple(r) for r in (await ctx.db.execute(select(FarmMember.id, Farm.id, Farm.name).join(Farm, Farm.id == FarmMember.farm_id)
                                                     .where(FarmMember.user_id == user_id, FarmMember.status == "notice",
                                                            FarmMember.role.in_(JOB_ROLES), FarmMember.farm_id != exclude_farm))).all()]


async def _leave_notice_jobs(ctx: Ctx, user_id: uuid.UUID, new_farm: Farm) -> None:
    """Joining a new farm ends a job the person is serving notice on, TODAY: that farm's salary stops (the rest of the
    notice period is no longer owed). Pay for the days already worked stays owed and is paid as usual — the farm is told
    how much, and so is the person."""
    olds = await _notice_jobs(ctx, user_id, new_farm.id)
    if not olds:
        return
    today = today_ist()
    for _mid, fid, _name in olds:          # accruals first, then the member row (the same lock order as payroll)
        await ensure_accruals_for_farm(ctx.db, fid)
    ended = set((await ctx.db.execute(update(FarmMember).where(
        FarmMember.id.in_([o[0] for o in olds]), FarmMember.status == "notice")
        .values(status="ended", ended_on=today, end_reason="joined_other_farm").returning(FarmMember.id))).scalars())
    worker = await ctx.db.get(User, user_id)
    for mid, fid, name in olds:
        if mid not in ended:
            continue
        m = await ctx.db.get(FarmMember, mid)
        await ctx.db.refresh(m)
        due = (await farm_money(ctx.db, fid, members=[m]))[mid].due
        await notify_farm_leaders(ctx.db, fid, "left_for_other_farm", title_key="notif.left_for_other_farm.title",
                                  body_key="notif.left_for_other_farm.body", params={"name": worker.full_name, "amount": format_inr(due)},
                                  link=f"/farms/{fid}?tab=payroll", severity="warning", roles=("co_owner", "manager", "accountant"))
        await notify(ctx.db, user_id, "notice_job_ended", title_key="notif.notice_job_ended.title",
                     body_key="notif.notice_job_ended.body" if due > 0 else "notif.notice_job_ended.body_settled",
                     params={"farm": name, "amount": format_inr(due)}, link="/dashboard", severity="info")


async def _assert_not_own_farm(ctx: Ctx, farm: Farm, u: User, key: str = "errors.already_member") -> None:
    """Nobody takes a job (or a second place) on a farm they own, co-own or already belong to."""
    if farm.owner_id == u.id or (await ctx.db.execute(select(FarmMember.id).where(
            FarmMember.farm_id == farm.id, FarmMember.user_id == u.id, FarmMember.status != "ended").limit(1))).first():
        raise AppError(key, status=409)


async def _assert_worker_eligible(ctx: Ctx, u: User, *, exclude_farm: uuid.UUID | None = None, own: bool = True) -> None:
    cfg = await get_all(ctx.db)
    if u.status != "active":
        raise AppError("errors.account_inactive")
    if not u.photo_verified:
        raise AppError("errors.photo_required")
    if not u.dob or (age_on(u.dob) or 0) < int(cfg["min_worker_age"]):
        raise AppError("errors.age_required", age=cfg["min_worker_age"])
    await _assert_one_working_farm(ctx, u, exclude_farm=exclude_farm, own=own)


async def _hiring_capacity(ctx: Ctx, farm: Farm) -> int:
    employed = (await ctx.db.execute(select(func.count()).select_from(FarmMember).where(
        FarmMember.farm_id == farm.id, FarmMember.status != "ended", FarmMember.role.in_(WORKER_ROLES)))).scalar_one()
    return farm.hiring_limit - int(employed)


async def _salary_lines(ctx: Ctx, state: str | None, salary_type: str, amount: Decimal) -> list[str]:
    ok, minimum = await check_min_wage(ctx.db, state, salary_type, amount)
    if not ok:
        raise AppError("errors.below_min_wage", minimum=format_inr(minimum), unit=ctx.t(f"salary.{salary_type}"))
    return [ctx.t("sum.salary", amount=format_inr(amount), unit=ctx.t(f"salary.{salary_type}"), minimum=format_inr(minimum))]


def _terms_text(ctx: Ctx, farm: Farm) -> str:
    return ctx.t("sum.terms_accept_custom", version=farm.terms_version) if farm.terms_mode == "custom" else ctx.t("sum.terms_accept_boiler")


@register
class JobCreate(Action):
    type = "job.create"

    class Payload(Payload):
        farm_id: uuid.UUID
        branch_id: uuid.UUID
        title: Name
        role: Literal["employee", "supervisor"] = "employee"
        salary_type: Literal["daily", "monthly"]
        salary_amount: PositiveMoney
        positions: int = Field(ge=1, le=500)
        description: LongText | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        farm, _ = await require_perm(ctx.db, p.farm_id, ctx.user, "settings")  # salary is decided by owners
        branch = await ctx.db.get(Branch, p.branch_id)
        if not branch or branch.farm_id != farm.id:
            raise NotFound("errors.branch_not_found")
        await _assert_branch_hiring(ctx, farm, branch)
        lines = [ctx.t("sum.job", title=p.title, branch=branch.name, positions=p.positions),
                 *await _salary_lines(ctx, branch.state, p.salary_type, p.salary_amount),
                 ctx.t("sum.job_hours", start=branch.work_start.strftime("%H:%M"), end=branch.work_end.strftime("%H:%M")),
                 ctx.t("sum.job_radius")]
        cap = await _hiring_capacity(ctx, farm)
        warnings = [ctx.t("warn.hiring_limit", n=cap)] if cap < p.positions else []
        return Prepared(lines, warnings=warnings, farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        job = JobPosting(farm_id=p.farm_id, branch_id=p.branch_id, title=p.title, role=p.role, salary_type=p.salary_type,
                         salary_amount=p.salary_amount, positions=p.positions, description=p.description, created_by_id=ctx.user.id)
        ctx.db.add(job)
        await ctx.db.flush()
        return {"message": ctx.t("msg.job_posted"), "job_id": str(job.id)}


@register
class JobClose(Action):
    type = "job.close"

    class Payload(Payload):
        job_id: uuid.UUID

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        job = await ctx.db.get(JobPosting, p.job_id)
        if not job:
            raise NotFound()
        farm, _ = await require_perm(ctx.db, job.farm_id, ctx.user, "hiring")
        if job.status != "open":
            raise Conflict("errors.no_changes")
        return Prepared([ctx.t("sum.job_close", title=job.title)], farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        job = await ctx.db.get(JobPosting, p.job_id)
        job.status = "closed"
        return {"message": ctx.t("msg.saved")}


async def _worker_location(ctx: Ctx, lat: float | None, lon: float | None) -> tuple[float, float, str, str]:
    """Where the join-distance rule is measured from: a live position if the app sent one, otherwise the location saved
    in the profile — shared by GPS or typed by hand (both were checked to be in India when saved), so turning location
    off never blocks joining. Returns (lat, lon, source, summary line); source live | gps | typed is shown to the farm."""
    if lat is not None and lon is not None:
        geo = await check_india(lat, lon)
        if not geo["ok"]:
            raise AppError("errors.outside_india")
        return lat, lon, "live", ctx.t("sum.location_from.live")
    u = ctx.user
    if u.lat is None or u.lon is None:
        raise AppError("errors.location_required")
    source = "gps" if u.location_verified_at else "typed"
    place = ", ".join(dict.fromkeys(x for x in (u.city, u.district, u.state) if x)) or f"{u.lat:.3f}, {u.lon:.3f}"
    return u.lat, u.lon, source, ctx.t(f"sum.location_from.{source}", place=place)


def _too_far(source: str, km: float, max_km: float) -> AppError:
    """Too far for the join rule; when measured from the saved location, say how to fix a stale one."""
    return AppError("errors.too_far" if source == "live" else "errors.too_far_saved", km=round(km, 1), max=int(max_km))


@register
class JobApply(Action):
    type = "job.apply"
    roles = {"farmer", "owner"}          # owning farms is fine — working happens on one farm at a time

    class Payload(Payload):
        job_id: uuid.UUID
        lat: Lat | None = None      # live position (optional) — otherwise the saved location is used
        lon: Lon | None = None
        accept_terms: Literal[True]
        note: ShortText | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        job = await ctx.db.get(JobPosting, p.job_id)
        if not job or job.status != "open":
            raise NotFound("errors.job_not_open")
        farm, branch = await ctx.db.get(Farm, job.farm_id), await ctx.db.get(Branch, job.branch_id)
        await _assert_not_own_farm(ctx, farm, ctx.user, "errors.own_farm_job")
        await _assert_branch_hiring(ctx, farm, branch)
        await _assert_worker_eligible(ctx, ctx.user)
        lat, lon, source, where = await _worker_location(ctx, p.lat, p.lon)
        cfg = await get_all(ctx.db)
        dist = haversine_km(lat, lon, branch.lat, branch.lon)
        if dist > float(cfg["max_join_radius_km"]):
            raise _too_far(source, dist, float(cfg["max_join_radius_km"]))
        dup = (await ctx.db.execute(select(JobApplication.id).where(JobApplication.posting_id == job.id,
                                                                    JobApplication.applicant_id == ctx.user.id,
                                                                    JobApplication.status.in_(("applied", "accepted"))))).first()
        if dup:
            raise Conflict("errors.already_applied")
        lines = [ctx.t("sum.apply", title=job.title, farm=farm.name, branch=branch.name, km=round(dist, 1)), where,
                 ctx.t("sum.salary_plain", amount=format_inr(job.salary_amount), unit=ctx.t(f"salary.{job.salary_type}")),
                 ctx.t("sum.job_hours", start=branch.work_start.strftime("%H:%M"), end=branch.work_end.strftime("%H:%M")),
                 _terms_text(ctx, farm), ctx.t("sum.worker_rights")]
        warnings = [ctx.t("warn.typed_location_shared")] if source == "typed" else []
        warnings += [ctx.t("warn.apply_ends_notice", farm=name) for _m, _f, name in await _notice_jobs(ctx, ctx.user.id, farm.id)]
        return Prepared(lines, warnings=warnings, farm_id=farm.id, data={"dist": dist, "source": source})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        job = await ctx.db.get(JobPosting, p.job_id)
        farm = await ctx.db.get(Farm, job.farm_id)
        app_ = JobApplication(posting_id=job.id, farm_id=job.farm_id, branch_id=job.branch_id, applicant_id=ctx.user.id,
                              distance_km=round(prep.data["dist"], 2), location_source=prep.data.get("source"),
                              terms_version=farm.terms_version, terms_accepted_at=now_utc(), note=p.note)
        ctx.db.add(app_)
        ctx.db.add(TermsAcceptance(user_id=ctx.user.id, farm_id=farm.id, version=farm.terms_version, ip=ctx.ip))
        await ctx.db.flush()
        await notify_farm_leaders(ctx.db, farm.id, "application", title_key="notif.application.title",
                                  body_key="notif.application.body", params={"title": job.title, "name": ctx.user.full_name},
                                  link=f"/farms/{farm.id}?tab=hiring", severity="info")
        return {"message": ctx.t("msg.applied")}


async def _create_member(ctx: Ctx, *, farm: Farm, branch_id: uuid.UUID | None, user_id: uuid.UUID, role: str, salary_type: str,
                         salary_amount: Decimal, terms_version: str | None, source: str, own: bool = True) -> FarmMember:
    m = FarmMember(farm_id=farm.id, branch_id=branch_id, user_id=user_id, role=role, salary_type=salary_type,
                   salary_amount=salary_amount, joined_on=today_ist(), status="active", terms_version=terms_version,
                   terms_accepted_at=now_utc() if terms_version else None, source=source)
    if role in JOB_ROLES:
        await _leave_notice_jobs(ctx, user_id, farm)
    try:
        async with ctx.db.begin_nested():
            ctx.db.add(m)
            await ctx.db.flush()
    except IntegrityError as exc:          # two farms took the same person at the same moment: the second one is refused
        msg = str(getattr(exc, "orig", exc))
        if "uq_one_working_farm" in msg:
            raise AppError("errors.one_job_at_a_time" if own else "errors.has_job_elsewhere", status=409) from None
        if "uq_member_active" in msg:
            raise AppError("errors.already_member", status=409) from None
        raise
    return m


@register
class ApplicationDecide(Action):
    type = "application.decide"

    class Payload(Payload):
        application_id: uuid.UUID
        decision: Literal["accept", "reject"]
        note: ShortText | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        a = await ctx.db.get(JobApplication, p.application_id)
        if not a:
            raise NotFound()
        farm, _ = await require_perm(ctx.db, a.farm_id, ctx.user, "hiring")
        if a.status != "applied":
            raise Conflict("errors.no_changes")
        job = await ctx.db.get(JobPosting, a.posting_id)
        applicant = await ctx.db.get(User, a.applicant_id)
        if p.decision == "accept":
            branch = await ctx.db.get(Branch, a.branch_id)
            await _assert_not_own_farm(ctx, farm, applicant)
            await _assert_branch_hiring(ctx, farm, branch)
            await _assert_worker_eligible(ctx, applicant, own=False)
            if await _hiring_capacity(ctx, farm) <= 0:
                raise AppError("errors.hiring_limit_reached")
            lines = [ctx.t("sum.hire", name=applicant.full_name, title=job.title, branch=branch.name),
                     ctx.t("sum.salary_plain", amount=format_inr(job.salary_amount), unit=ctx.t(f"salary.{job.salary_type}")),
                     ctx.t("sum.join_today", date=today_ist().isoformat())]
        else:
            lines = [ctx.t("sum.reject", name=applicant.full_name, title=job.title)]
        return Prepared(lines, farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        a = await ctx.db.get(JobApplication, p.application_id)
        job = await ctx.db.get(JobPosting, a.posting_id)
        farm = await ctx.db.get(Farm, a.farm_id)
        a.status, a.decided_by_id, a.decided_at = ("accepted" if p.decision == "accept" else "rejected"), ctx.user.id, now_utc()
        if p.decision == "accept":
            await _create_member(ctx, own=False, farm=farm, branch_id=a.branch_id, user_id=a.applicant_id, role=job.role,
                                 salary_type=job.salary_type, salary_amount=job.salary_amount, terms_version=a.terms_version,
                                 source="application")
            hired = (await ctx.db.execute(select(func.count()).select_from(JobApplication).where(
                JobApplication.posting_id == job.id, JobApplication.status == "accepted"))).scalar_one()
            if hired >= job.positions:
                job.status = "filled"
        await notify(ctx.db, a.applicant_id, "application_result", title_key=f"notif.application_{a.status}.title",
                     body_key=f"notif.application_{a.status}.body", params={"title": job.title, "farm": farm.name},
                     link="/dashboard", severity="success" if a.status == "accepted" else "info", channels=("sms",))
        return {"message": ctx.t("msg.saved")}


@register
class MemberInvite(Action):
    type = "member.invite"

    class Payload(Payload):
        farm_id: uuid.UUID
        branch_id: uuid.UUID | None = None
        phone: Phone
        role: Literal["manager", "accountant", "supervisor", "employee"]
        salary_type: Literal["daily", "monthly"]
        salary_amount: PositiveMoney

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        farm, _ = await require_perm(ctx.db, p.farm_id, ctx.user, "salary")  # owner decides salaries
        if farm.status != "active":
            raise AppError("errors.farm_not_active", status=409)
        phone = normalize_phone(p.phone)
        if not phone:
            raise AppError("errors.phone_invalid")
        invitee = (await ctx.db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
        if invitee:
            await _assert_not_own_farm(ctx, farm, invitee)
        branch = None
        if p.branch_id:                 # every role: only a branch of THIS farm (it is copied onto the membership)
            branch = await ctx.db.get(Branch, p.branch_id)
            if not branch or branch.farm_id != farm.id:
                raise NotFound("errors.branch_not_found")
        if p.role in WORKER_ROLES:
            if branch is None:
                raise AppError("errors.branch_required")
            await _assert_branch_hiring(ctx, farm, branch)
            if await _hiring_capacity(ctx, farm) <= 0:
                raise AppError("errors.hiring_limit_reached")
        state = branch.state if branch else None
        lines = [ctx.t("sum.invite", phone=phone, role=ctx.t(f"role.{p.role}"), farm=farm.name),
                 *await _salary_lines(ctx, state, p.salary_type, p.salary_amount)]
        return Prepared(lines, farm_id=farm.id, data={"phone": phone})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        phone = prep.data["phone"]
        u = (await ctx.db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
        inv = Invitation(farm_id=p.farm_id, branch_id=p.branch_id, phone=phone, invitee_id=u.id if u else None, role=p.role,
                         salary_type=p.salary_type, salary_amount=p.salary_amount, invited_by_id=ctx.user.id,
                         expires_at=now_utc() + timedelta(days=14))
        ctx.db.add(inv)
        await ctx.db.flush()
        farm = await ctx.db.get(Farm, p.farm_id)
        if u:
            await notify(ctx.db, u.id, "invitation", title_key="notif.invitation.title", body_key="notif.invitation.body",
                         params={"farm": farm.name, "role": p.role}, link="/dashboard#invitations", channels=("sms", "whatsapp"))
        return {"message": ctx.t("msg.invited")}


@register
class InvitationRespond(Action):
    type = "invitation.respond"

    class Payload(Payload):
        invitation_id: uuid.UUID
        accept: bool
        lat: Lat | None = None
        lon: Lon | None = None
        accept_terms: bool = False

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        inv = await ctx.db.get(Invitation, p.invitation_id)
        if not inv or inv.phone != ctx.user.phone:
            raise NotFound()
        if inv.status != "pending" or (inv.expires_at and inv.expires_at < now_utc()):
            raise Conflict("errors.invitation_closed")
        farm = await ctx.db.get(Farm, inv.farm_id)
        if not p.accept:
            return Prepared([ctx.t("sum.invite_decline", farm=farm.name)], farm_id=farm.id)
        if not p.accept_terms:
            raise AppError("errors.terms_required")
        await _assert_not_own_farm(ctx, farm, ctx.user)
        lines = [ctx.t("sum.invite_accept", farm=farm.name, role=ctx.t(f"role.{inv.role}"))]
        if inv.role in JOB_ROLES and inv.role not in WORKER_ROLES:      # manager / accountant: work as well
            await _assert_one_working_farm(ctx, ctx.user)
        if inv.role in WORKER_ROLES:
            branch = await ctx.db.get(Branch, inv.branch_id)
            await _assert_branch_hiring(ctx, farm, branch)
            await _assert_worker_eligible(ctx, ctx.user)
            lat, lon, source, where = await _worker_location(ctx, p.lat, p.lon)
            dist = haversine_km(lat, lon, branch.lat, branch.lon)
            max_km = float((await get_all(ctx.db))["max_join_radius_km"])
            if dist > max_km:
                raise _too_far(source, dist, max_km)
            lines += [ctx.t("sum.distance", km=round(dist, 1)), where]
        if inv.salary_amount and inv.salary_amount > 0:
            lines.append(ctx.t("sum.salary_plain", amount=format_inr(inv.salary_amount), unit=ctx.t(f"salary.{inv.salary_type}")))
        lines.append(_terms_text(ctx, farm))
        warnings = ([ctx.t("warn.join_ends_notice", farm=name) for _m, _f, name in await _notice_jobs(ctx, ctx.user.id, farm.id)]
                    if inv.role in JOB_ROLES else [])
        return Prepared(lines, warnings=warnings, farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        inv = await ctx.db.get(Invitation, p.invitation_id)
        farm = await ctx.db.get(Farm, inv.farm_id)
        inv.responded_at, inv.invitee_id = now_utc(), ctx.user.id
        if not p.accept:
            inv.status = "declined"
            return {"message": ctx.t("msg.saved")}
        inv.status = "accepted"
        await _create_member(ctx, farm=farm, branch_id=inv.branch_id, user_id=ctx.user.id, role=inv.role,
                             salary_type=inv.salary_type, salary_amount=inv.salary_amount, terms_version=farm.terms_version,
                             source="invitation")
        ctx.db.add(TermsAcceptance(user_id=ctx.user.id, farm_id=farm.id, version=farm.terms_version, ip=ctx.ip))
        await notify(ctx.db, inv.invited_by_id, "invitation_accepted", title_key="notif.invitation_accepted.title",
                     body_key="notif.invitation_accepted.body", params={"name": ctx.user.full_name, "farm": farm.name},
                     link=f"/farms/{farm.id}?tab=team", severity="success")
        return {"message": ctx.t("msg.joined", farm=farm.name), "redirect": "/dashboard"}


async def _member_for_leader(ctx: Ctx, member_id: uuid.UUID, perm: str, *, lock: bool = True) -> tuple[FarmMember, Farm, User, str]:
    """Target member + farm + target user + the ACTOR's effective role.

    `lock`: the member row stays locked for the rest of the transaction (prepare runs again at GO, in the same transaction
    as execute) — two leaders acting on one person at the same moment serialise, and the second re-checks the new state.
    Salary accruals are always inserted BEFORE a member row is locked (payroll does the same) — no lock-order cycle."""
    stmt = select(FarmMember).where(FarmMember.id == member_id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    m = (await ctx.db.execute(stmt)).scalar_one_or_none()
    if not m:
        raise NotFound()
    farm, role = await require_perm(ctx.db, m.farm_id, ctx.user, perm)
    return m, farm, await ctx.db.get(User, m.user_id), role


def _guard_target(ctx: Ctx, farm: Farm, m: FarmMember, actor_role: str) -> None:
    """Nobody acts on themselves (that is 'Leave job'), on the owner, on a co-owner (needs a co-owner proposal) or on a
    peer / someone above them."""
    if m.user_id == ctx.user.id:
        raise Forbidden("errors.not_on_yourself")
    if m.role == "co_owner":
        raise Forbidden("errors.coowner_via_proposal")
    if m.user_id == farm.owner_id or not outranks(actor_role, m.role):
        raise Forbidden("errors.rank_too_low")


async def _tell_owners(ctx: Ctx, farm: Farm, kind: str, key: str, params: dict) -> None:
    """Oversight: the owner and co-owners hear about every people decision somebody ELSE takes."""
    ids = [i for i in dict.fromkeys([farm.owner_id, *await coowner_ids(ctx.db, farm.id)]) if i != ctx.user.id]
    if ids:
        await notify_many(ctx.db, ids, kind, title_key=f"{key}.title", body_key=f"{key}.body", params=params,
                          link=f"/farms/{farm.id}?tab=team", severity="info")


async def _locked_member(ctx: Ctx, member_id: uuid.UUID) -> FarmMember:
    return (await ctx.db.execute(select(FarmMember).where(FarmMember.id == member_id).with_for_update()
                                 .execution_options(populate_existing=True))).scalar_one()


@register
class MemberSalary(Action):
    type = "member.update_salary"

    class Payload(Payload):
        member_id: uuid.UUID
        salary_type: Literal["daily", "monthly"]
        salary_amount: PositiveMoney

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        m, farm, u, _ = await _member_for_leader(ctx, p.member_id, "salary", lock=False)
        if m.status == "ended":
            raise Conflict("errors.member_ended")
        branch = await ctx.db.get(Branch, m.branch_id) if m.branch_id else None
        lines = [ctx.t("sum.salary_change", name=u.full_name, old=format_inr(m.salary_amount), old_unit=ctx.t(f"salary.{m.salary_type}"),
                       new=format_inr(p.salary_amount), new_unit=ctx.t(f"salary.{p.salary_type}")),
                 *await _salary_lines(ctx, branch.state if branch else None, p.salary_type, p.salary_amount),
                 ctx.t("sum.salary_from_tomorrow")]
        return Prepared(lines, farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        farm_id = (await ctx.db.get(FarmMember, p.member_id)).farm_id
        await ensure_accruals_for_farm(ctx.db, farm_id)  # lock in past days at the old salary — before the row lock
        m = await _locked_member(ctx, p.member_id)
        if m.status == "ended":
            raise Conflict("errors.member_ended")
        m.salary_type, m.salary_amount = p.salary_type, p.salary_amount
        farm = await ctx.db.get(Farm, m.farm_id)
        await notify(ctx.db, m.user_id, "salary_changed", title_key="notif.salary_changed.title", body_key="notif.salary_changed.body",
                     params={"farm": farm.name, "amount": format_inr(p.salary_amount), "unit__key": f"salary.{p.salary_type}"},
                     channels=("sms",))
        return {"message": ctx.t("msg.saved")}


@register
class MemberNotice(Action):
    """One-month notice (nobody is removed suddenly). Only someone strictly above the person can give it; a leader being
    let go keeps working and is paid, but their leadership powers pause at once (see perms.effective_role)."""
    type = "member.give_notice"

    class Payload(Payload):
        member_id: uuid.UUID
        reason: ShortText

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        m, farm, u, role = await _member_for_leader(ctx, p.member_id, "members")
        _guard_target(ctx, farm, m, role)
        if m.status != "active":
            raise Conflict("errors.no_changes")
        days = int((await get_all(ctx.db))["notice_days"])
        end = today_ist() + timedelta(days=days)
        lines = [ctx.t("sum.notice_1", name=u.full_name, date=end.isoformat(), days=days), ctx.t("sum.notice_2"),
                 ctx.t("sum.notice_3", date=end.isoformat()), ctx.t("sum.reason", reason=p.reason)]
        if m.role != "employee":
            lines.append(ctx.t("sum.notice_powers", name=u.full_name, role=ctx.t(f"role.{m.role}")))
        return Prepared(lines, farm_id=farm.id, data={"end": end.isoformat(), "name": u.full_name})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        m = await ctx.db.get(FarmMember, p.member_id)        # row locked by prepare (same transaction)
        farm = await ctx.db.get(Farm, m.farm_id)
        m.status, m.notice_given_at, m.notice_by_id, m.notice_kind = "notice", now_utc(), ctx.user.id, "individual"
        m.notice_end_date, m.notice_reason = date.fromisoformat(prep.data["end"]), p.reason
        await notify(ctx.db, m.user_id, "notice", title_key="notif.notice.title", body_key="notif.notice.body_reason",
                     params={"farm": farm.name, "date": prep.data["end"], "reason": p.reason}, severity="warning",
                     channels=("sms", "whatsapp"))
        await _tell_owners(ctx, farm, "notice_given", "notif.notice_given",
                           {"name": prep.data["name"], "by": ctx.user.full_name, "date": prep.data["end"], "reason": p.reason})
        return {"message": ctx.t("msg.notice_given", date=prep.data["end"])}


@register
class MemberCancelNotice(Action):
    """Only an individual notice can be withdrawn (a closure notice ends with the closure) — by the person who gave it,
    or by the owner / a co-owner, and only from above."""
    type = "member.cancel_notice"

    class Payload(Payload):
        member_id: uuid.UUID

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        m, farm, u, role = await _member_for_leader(ctx, p.member_id, "members")
        _guard_target(ctx, farm, m, role)
        if m.status != "notice":
            raise Conflict("errors.no_changes")
        branch = await ctx.db.get(Branch, m.branch_id) if m.branch_id else None
        if m.notice_kind != "individual" or farm.status == "closing" or (branch and branch.status == "closing"):
            raise Conflict("errors.notice_is_closure")
        if m.notice_by_id != ctx.user.id and role not in ("owner", "co_owner"):
            raise Forbidden("errors.notice_not_yours")
        return Prepared([ctx.t("sum.notice_cancel", name=u.full_name)], farm_id=farm.id, data={"name": u.full_name})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        m = await ctx.db.get(FarmMember, p.member_id)
        farm = await ctx.db.get(Farm, m.farm_id)
        m.status, m.notice_end_date, m.notice_given_at, m.notice_reason = "active", None, None, None
        m.notice_kind, m.notice_by_id = None, None
        await notify(ctx.db, m.user_id, "notice_cancelled", title_key="notif.notice_cancelled.title",
                     body_key="notif.notice_cancelled.body", severity="success", channels=("sms",))
        await _tell_owners(ctx, farm, "notice_cancelled", "notif.notice_cancelled_owner",
                           {"name": prep.data["name"], "by": ctx.user.full_name})
        return {"message": ctx.t("msg.saved")}


ROLE_CHOICES = Literal["manager", "accountant", "supervisor", "employee"]


@register
class MemberChangeRole(Action):
    """Promotion / demotion — decided by the owner and co-owners together (a proposal on farms with co-owners).
    `expected_role` pins the role the deciders saw: if it changed meanwhile, the change is refused instead of guessed."""
    type = "member.change_role"
    consensus = True

    class Payload(Payload):
        member_id: uuid.UUID
        role: ROLE_CHOICES
        expected_role: ROLE_CHOICES
        branch_id: uuid.UUID | None = None
        reason: ShortText | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        m, farm, u, actor = await _member_for_leader(ctx, p.member_id, "settings")
        _guard_target(ctx, farm, m, actor)
        if farm.status != "active":
            raise AppError("errors.farm_not_active", status=409)
        if m.status != "active":
            raise Conflict("errors.role_change_active_only")
        if m.role != p.expected_role:
            raise Conflict("errors.role_changed_meanwhile")
        if p.role == m.role:
            raise Conflict("errors.no_changes")
        old, new = m.role, p.role
        branch = None
        if new in WORKER_ROLES or p.branch_id:
            bid = p.branch_id or m.branch_id
            if not bid:
                raise AppError("errors.branch_required")
            branch = await ctx.db.get(Branch, bid)
            if not branch or branch.farm_id != farm.id:
                raise NotFound("errors.branch_not_found")
            if branch.status != "active":
                raise AppError("errors.branch_not_active", status=409)
        if new in WORKER_ROLES and old not in WORKER_ROLES:
            # becoming a farm worker: same rules as any hire (age, photo, one job at a time, the farm's hiring limit)
            await _assert_worker_eligible(ctx, u, exclude_farm=farm.id, own=False)
            if await _hiring_capacity(ctx, farm) <= 0:
                raise AppError("errors.hiring_limit_reached")
        up = RANK[new] > RANK[old]
        lines = [ctx.t("sum.role_promote" if up else "sum.role_demote", name=u.full_name, old=ctx.t(f"role.{old}"), new=ctx.t(f"role.{new}")),
                 ctx.t(f"sum.role_powers.{new}"),
                 ctx.t("sum.role_salary_same", amount=format_inr(m.salary_amount), unit=ctx.t(f"salary.{m.salary_type}"))]
        if branch and branch.id != m.branch_id:
            lines.append(ctx.t("sum.role_branch", branch=branch.name))
        if p.reason:
            lines.append(ctx.t("sum.reason", reason=p.reason))
        warnings = [ctx.t("warn.role_demotion", name=u.full_name)] if not up else []
        return Prepared(lines, warnings=warnings, farm_id=farm.id,
                        data={"branch_id": str(branch.id) if branch else None, "name": u.full_name, "old": old})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        m = await ctx.db.get(FarmMember, p.member_id)
        farm = await ctx.db.get(Farm, m.farm_id)
        old = m.role
        m.role = p.role
        if prep.data.get("branch_id"):
            m.branch_id = uuid.UUID(prep.data["branch_id"])
        await notify(ctx.db, m.user_id, "role_changed", title_key="notif.role_changed.title", body_key="notif.role_changed.body",
                     params={"farm": farm.name, "old__key": f"role.{old}", "new__key": f"role.{p.role}"},
                     link="/dashboard", severity="info", channels=("sms",))
        await _tell_owners(ctx, farm, "role_changed", "notif.role_changed_owner",
                           {"name": prep.data["name"], "by": ctx.user.full_name, "old__key": f"role.{old}", "new__key": f"role.{p.role}"})
        return {"message": ctx.t("msg.role_changed", name=prep.data["name"], role=ctx.t(f"role.{p.role}"))}


@register
class CoownerRemove(Action):
    """Removing a co-owner needs the owner and every OTHER co-owner; the person being removed has no vote on it.
    (A co-owner can always leave by themselves.) Money owed to them stays owed."""
    type = "coowner.remove"
    consensus = True

    class Payload(Payload):
        member_id: uuid.UUID
        reason: ShortText

    async def excluded_voters(self, db, payload: dict) -> set[uuid.UUID]:
        try:
            mid = uuid.UUID(str(payload.get("member_id")))
        except ValueError:
            return set()
        uid = (await db.execute(select(FarmMember.user_id).where(FarmMember.id == mid))).scalar_one_or_none()
        return {uid} if uid else set()

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        m, farm, u, _ = await _member_for_leader(ctx, p.member_id, "settings")
        if m.role != "co_owner":
            raise Conflict("errors.no_changes")
        if m.status == "ended":
            raise Conflict("errors.member_ended")
        if m.user_id == ctx.user.id:
            raise Forbidden("errors.not_on_yourself")
        return Prepared([ctx.t("sum.coowner_remove", name=u.full_name, farm=farm.name), ctx.t("sum.reason", reason=p.reason),
                         ctx.t("sum.coowner_remove_vote")], farm_id=farm.id, data={"name": u.full_name})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        from app.services.actions.base import settle_proposals

        m = await ctx.db.get(FarmMember, p.member_id)
        farm = await ctx.db.get(Farm, m.farm_id)
        m.status, m.ended_on, m.end_reason = "ended", today_ist(), "removed"
        await ctx.db.flush()
        await notify(ctx.db, m.user_id, "coowner_removed", title_key="notif.coowner_removed.title", body_key="notif.coowner_removed.body",
                     params={"farm": farm.name, "reason": p.reason}, severity="warning", channels=("sms", "email"))
        await _tell_owners(ctx, farm, "coowner_removed", "notif.coowner_removed_owner",
                           {"name": prep.data["name"], "by": ctx.user.full_name, "reason": p.reason})
        await settle_proposals(ctx.db, farm.id, ctx.lang, ctx.ip)
        return {"message": ctx.t("msg.coowner_removed", name=prep.data["name"])}


@register
class MemberResign(Action):
    type = "member.resign"

    class Payload(Payload):
        member_id: uuid.UUID

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        m = await ctx.db.get(FarmMember, p.member_id)
        if not m or m.user_id != ctx.user.id:
            raise NotFound()
        if m.status == "ended":
            raise Conflict("errors.no_changes")
        farm = await ctx.db.get(Farm, m.farm_id)
        await ensure_accruals_for_farm(ctx.db, farm.id)      # accruals first, then the row lock (same order as payroll)
        m = await _locked_member(ctx, p.member_id)
        if m.status == "ended":
            raise Conflict("errors.no_changes")
        money = (await farm_money(ctx.db, farm.id, members=[m]))[m.id]
        return Prepared([ctx.t("sum.resign", farm=farm.name), ctx.t("sum.resign_dues", amount=format_inr(money.due))], farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        from app.services.actions.base import settle_proposals

        m = await ctx.db.get(FarmMember, p.member_id)
        farm = await ctx.db.get(Farm, m.farm_id)
        was_coowner = m.role == "co_owner"
        m.status, m.ended_on, m.end_reason = "ended", today_ist(), "resigned"
        await ctx.db.flush()
        await notify_farm_leaders(ctx.db, farm.id, "resigned", title_key="notif.resigned.title", body_key="notif.resigned.body",
                                  params={"name": ctx.user.full_name}, link=f"/farms/{farm.id}?tab=payroll", severity="warning",
                                  roles=("co_owner", "manager", "accountant"))
        if was_coowner:
            await settle_proposals(ctx.db, farm.id, ctx.lang, ctx.ip)
        return {"message": ctx.t("msg.resigned"), "redirect": "/dashboard"}
