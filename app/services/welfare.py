"""Worker protection.

* After working hours every worker is asked "How did your day go?" (+ water / food / breaks / rest / overwork / respect).
* A bad day → superadmin gets FULL details; the owner gets an ANONYMISED warning in a delayed daily digest
  (prevents identifying the reporter by timing). The worker's identity is never shown to the owner.
* Several distinct workers reporting within the window → branch halted for safety + safety case
  (legal warning, compensation for ALL workers of the branch so nobody can be singled out).
* False reports → warnings → ban after the threshold; the owner is told to pay salary till date (no reason given).
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, NotFound
from app.models import (
    Branch, BranchDay, DueAdjustment, Farm, FarmMember, Grievance, OwnerWarning, SafetyCase, User, WellbeingSurvey,
)
from app.models.farm import WORKER_ROLES
from app.models.welfare import GRIEVANCE_CATEGORIES
from app.security.sessions import destroy_user_sessions
from app.services.audit import audit
from app.services.halts import ensure_halt, lift_halts
from app.services.notify import notify, notify_admins, notify_many
from app.services.payroll import Calendar, recompute_day
from app.services.settings_store import get_all
from app.utils import format_inr, now_ist, now_utc, today_ist

NEGATIVE_ANSWERS = {  # question -> (bad answer, grievance category)
    "water": (False, "no_water"),
    "food": (False, "no_food"),
    "breaks": (False, "no_breaks"),
    "rest": (False, "no_breaks"),
    "overworked": (True, "overwork"),
    "respect": (False, "abuse"),
    "paid_fairly": (False, "low_salary"),
    "safe": (False, "unsafe"),
}


async def create_due_surveys(db: AsyncSession) -> int:
    """Create today's surveys for branches whose working hours have ended (bulk; constant queries)."""
    now = now_ist()
    today = now.date()
    branches = list((await db.execute(select(Branch).join(Farm, Farm.id == Branch.farm_id).where(
        Branch.status != "closed", Farm.status.in_(("active", "closing")), Branch.work_end <= now.time().replace(tzinfo=None)))).scalars())
    if not branches:
        return 0
    cal = await Calendar.load(db, branches, today, today)
    open_ids = {b.id for b in branches if cal.is_open(b, today)}
    if not open_ids:
        return 0
    members = (await db.execute(select(FarmMember).where(
        FarmMember.branch_id.in_(list(open_ids)), FarmMember.status.in_(("active", "notice")),
        FarmMember.role.in_(WORKER_ROLES), FarmMember.joined_on <= today))).scalars().all()
    if not members:
        return 0
    rows = [{"id": uuid.uuid4(), "user_id": m.user_id, "member_id": m.id, "farm_id": m.farm_id, "branch_id": m.branch_id,
             "survey_date": today, "status": "pending"} for m in members]
    inserted = list((await db.execute(pg_insert(WellbeingSurvey).values(rows).on_conflict_do_nothing(
        index_elements=["member_id", "survey_date"]).returning(WellbeingSurvey.user_id))).scalars())
    await notify_many(db, inserted, "survey", title_key="notif.survey.title", body_key="notif.survey.body",
                      link="/survey", severity="info", dedupe_key=f"survey:{today}")
    return len(inserted)


def categories_from_answers(rating: str, answers: dict[str, Any]) -> list[str]:
    cats = [cat for q, (bad, cat) in NEGATIVE_ANSWERS.items() if q in answers and answers[q] is bad]
    if rating == "bad" and not cats:
        cats = ["other"]
    return list(dict.fromkeys(cats))


async def submit_survey(db: AsyncSession, user: User, survey_id: uuid.UUID, rating: str, answers: dict[str, Any],
                        comment: str | None, ip: str | None) -> dict[str, Any]:
    s = (await db.execute(select(WellbeingSurvey).where(WellbeingSurvey.id == survey_id).with_for_update())).scalar_one_or_none()
    if not s or s.user_id != user.id:
        raise NotFound()
    if s.status != "pending":
        raise AppError("errors.survey_done", status=409)
    if (today_ist() - s.survey_date).days > 3:
        raise AppError("errors.survey_expired", status=410)
    if rating not in ("good", "bad"):
        raise AppError("errors.invalid_input")
    clean = {k: bool(v) for k, v in (answers or {}).items() if k in NEGATIVE_ANSWERS or k == "worked_today"}
    s.day_rating, s.answers, s.comment = rating, clean, (comment or "")[:1000] or None
    s.status, s.answered_at = "answered", now_utc()

    branch = await db.get(Branch, s.branch_id) if s.branch_id else None
    if branch and clean.get("worked_today"):
        cal = await Calendar.load(db, [branch], s.survey_date, s.survey_date)
        if cal.is_holiday(branch, s.survey_date) and not cal.is_open(branch, s.survey_date):
            await db.execute(pg_insert(BranchDay).values(id=uuid.uuid4(), branch_id=branch.id, day=s.survey_date, is_holiday=True,
                                                         is_open=True, source="survey").on_conflict_do_update(
                index_elements=["branch_id", "day"], set_={"is_open": True, "source": "survey"}))
            await db.flush()
            await recompute_day(db, branch, s.survey_date)  # +holiday premium for everyone on the branch

    cats = categories_from_answers(rating, clean)
    result = {"reported": False}
    # a bad day, or ANY denied essential (even on a "good" day), is reported
    if cats and (rating == "bad" or cats != ["other"]):
        g = Grievance(reporter_id=user.id, member_id=s.member_id, farm_id=s.farm_id, branch_id=s.branch_id,
                      source="survey", categories=cats, description=s.comment, survey_id=s.id)
        db.add(g)
        await db.flush()
        result = await handle_grievance(db, g, reporter=user)
    await audit(db, user.id, "survey.answer", "survey", s.id, {"rating": rating}, ip)
    return result


async def report_grievance(db: AsyncSession, user: User, categories: list[str], description: str | None, source: str) -> dict[str, Any]:
    from app.models.farm import WORKER_ROLES

    # the farm where the person WORKS (a worker who also leads another farm reports about their job, not their team)
    member = (await db.execute(select(FarmMember).where(
        FarmMember.user_id == user.id, FarmMember.status.in_(("active", "notice")), FarmMember.role.in_(WORKER_ROLES))
        .order_by(FarmMember.joined_on.desc()))).scalars().first()
    if not member:
        raise AppError("errors.no_active_job")
    cats = [c for c in categories if c in GRIEVANCE_CATEGORIES] or ["other"]
    g = Grievance(reporter_id=user.id, member_id=member.id, farm_id=member.farm_id, branch_id=member.branch_id,
                  source=source, categories=cats, description=(description or "")[:2000] or None)
    db.add(g)
    await db.flush()
    return await handle_grievance(db, g, reporter=user)


async def handle_grievance(db: AsyncSession, g: Grievance, reporter: User) -> dict[str, Any]:
    farm = await db.get(Farm, g.farm_id)
    branch = await db.get(Branch, g.branch_id) if g.branch_id else None
    owner = await db.get(User, farm.owner_id)
    cats_txt = ", ".join(g.categories)
    # superadmin: full details
    await notify_admins(db, "grievance", title_key="notif.grievance_admin.title", body_key="notif.grievance_admin.body",
                        params={"worker": reporter.full_name, "phone": reporter.phone, "farm": farm.name,
                                "branch": branch.name if branch else "-", "owner": owner.full_name if owner else "-",
                                "owner_phone": owner.phone if owner else "-", "categories": cats_txt,
                                "comment": (g.description or "")[:300]},
                        link=f"/admin?tab=reports#g-{g.id}", severity="danger", channels=("email",))
    # owner: anonymised, delivered later in a digest
    db.add(OwnerWarning(owner_id=farm.owner_id, farm_id=farm.id, branch_id=g.branch_id, kind="anonymous_report",
                        message_key="warning.anonymous_report", params={"branch": branch.name if branch else farm.name,
                                                                         "categories": cats_txt}))
    cfg = await get_all(db)
    since = now_utc() - timedelta(days=int(cfg["bad_report_window_days"]))
    reporters = (await db.execute(select(func.count(func.distinct(Grievance.reporter_id))).where(
        Grievance.farm_id == g.farm_id, Grievance.branch_id == g.branch_id if g.branch_id else Grievance.branch_id.is_(None),
        Grievance.status.in_(("open", "valid")), Grievance.created_at >= since))).scalar_one()
    escalated = False
    if reporters >= int(cfg["bad_report_threshold"]):
        escalated = await escalate_branch(db, farm, branch, since)
    return {"reported": True, "escalated": escalated}


async def escalate_branch(db: AsyncSession, farm: Farm, branch: Branch | None, since) -> bool:
    halt, created = await ensure_halt(db, farm.id, branch.id if branch else None, "safety_reports",
                                      params={"branch": branch.name if branch else farm.name})
    if not created:
        return False
    gids = [str(x) for x in (await db.execute(select(Grievance.id).where(
        Grievance.farm_id == farm.id, Grievance.branch_id == (branch.id if branch else None),
        Grievance.created_at >= since, Grievance.status.in_(("open", "valid"))))).scalars()]
    case = SafetyCase(farm_id=farm.id, branch_id=branch.id if branch else None, halt_id=halt.id, grievance_ids=gids)
    db.add(case)
    await db.flush()
    bname = branch.name if branch else farm.name
    await notify(db, farm.owner_id, "safety_halt", title_key="notif.safety_halt_owner.title", body_key="notif.safety_halt_owner.body",
                 params={"branch": bname}, severity="danger", link=f"/farms/{farm.id}", channels=("email", "sms", "whatsapp"))
    await notify_admins(db, "safety_case", title_key="notif.safety_case_admin.title", body_key="notif.safety_case_admin.body",
                        params={"farm": farm.name, "branch": bname, "count": len(gids)}, link=f"/admin?tab=cases#c-{case.id}",
                        severity="danger", channels=("email",))
    return True


async def deliver_owner_warnings(db: AsyncSession) -> int:
    """Daily digest: one anonymised summary per owner/farm (no names, no times)."""
    rows = (await db.execute(select(OwnerWarning).where(
        OwnerWarning.delivered_at.is_(None), OwnerWarning.kind == "anonymous_report"))).scalars().all()
    groups: dict[tuple, list[OwnerWarning]] = defaultdict(list)
    for w in rows:
        groups[(w.owner_id, w.farm_id, w.branch_id)].append(w)
    for (owner_id, farm_id, _bid), ws in groups.items():
        cats = sorted({c.strip() for w in ws for c in (w.params or {}).get("categories", "").split(",") if c.strip()})
        await notify(db, owner_id, "anonymous_report", title_key="notif.anon_report.title", body_key="notif.anon_report.body",
                     params={"count": len(ws), "branch": (ws[0].params or {}).get("branch", ""), "categories": ", ".join(cats)},
                     severity="warning", link=f"/farms/{farm_id}", channels=("email",))
        for w in ws:
            w.delivered_at = now_utc()
    return len(rows)


# ------------------------------------------------------------------ admin decisions
async def mark_report(db: AsyncSession, admin: User, grievance_id: uuid.UUID, verdict: str, note: str | None) -> dict[str, Any]:
    g = await db.get(Grievance, grievance_id)
    if not g:
        raise NotFound()
    if verdict not in ("valid", "false", "resolved"):
        raise AppError("errors.invalid_input")
    g.status, g.admin_note, g.reviewed_by_id, g.reviewed_at = verdict, (note or "")[:2000] or None, admin.id, now_utc()
    out: dict[str, Any] = {"banned": False}
    if verdict == "false":
        reporter = await db.get(User, g.reporter_id)
        reporter.false_report_warnings += 1
        cfg = await get_all(db)
        await notify(db, reporter.id, "false_report_warning", title_key="notif.false_report.title", body_key="notif.false_report.body",
                     params={"count": reporter.false_report_warnings, "limit": cfg["false_report_ban_threshold"]},
                     severity="danger", channels=("sms", "whatsapp"))
        if reporter.false_report_warnings >= int(cfg["false_report_ban_threshold"]):
            await ban_user(db, reporter, "repeated_false_reports", by=admin)
            out["banned"] = True
    await audit(db, admin.id, f"grievance.{verdict}", "grievance", g.id, {"note": note})
    return out


async def ban_user(db: AsyncSession, user: User, reason: str, *, by: User | None) -> None:
    """Ban a user. Workers' memberships end and owners are told to pay salary till date (reason not disclosed)."""
    if user.is_admin:
        raise AppError("errors.cannot_ban_admin")
    user.status, user.banned_at, user.ban_reason = "banned", now_utc(), reason
    user.token_version += 1
    await destroy_user_sessions(user.id, reason="banned")
    today = today_ist()
    members = (await db.execute(select(FarmMember).where(
        FarmMember.user_id == user.id, FarmMember.status.in_(("active", "notice"))))).scalars().all()
    for m in members:
        m.status, m.ended_on, m.end_reason = "ended", today, "platform_ban"
    if members:
        from app.services.payroll import ensure_accruals_for_farm, farm_money

        farms = {f.id: f for f in (await db.execute(select(Farm).where(Farm.id.in_({m.farm_id for m in members})))).scalars()}
        for m in members:  # a worker has at most a couple of memberships
            farm = farms[m.farm_id]
            await ensure_accruals_for_farm(db, farm.id)
            money = (await farm_money(db, farm.id, members=[m]))[m.id]
            await notify(db, farm.owner_id, "worker_banned", title_key="notif.worker_banned_owner.title",
                         body_key="notif.worker_banned_owner.body",
                         params={"worker": user.full_name, "farm": farm.name, "amount": format_inr(money.due)},
                         severity="warning", link=f"/farms/{farm.id}?tab=payroll", channels=("email", "sms"))
    owned = (await db.execute(select(Farm).where(Farm.owner_id == user.id, Farm.status.in_(("active", "closing", "pending_approval"))))).scalars().all()
    for f in owned:
        await ensure_halt(db, f.id, None, "admin_ban", params={"farm": f.name}, by=by.id if by else None)
        workers = (await db.execute(select(FarmMember.user_id).where(FarmMember.farm_id == f.id, FarmMember.status != "ended"))).scalars()
        await notify_many(db, list(workers), "owner_banned", title_key="notif.owner_banned_worker.title",
                          body_key="notif.owner_banned_worker.body", params={"farm": f.name}, severity="warning")
    await notify(db, user.id, "banned", title_key="notif.banned.title", body_key="notif.banned.body", severity="danger",
                 link="/banned", channels=("sms", "email"))
    await audit(db, by.id if by else None, "user.ban", "user", user.id, {"reason": reason})


async def unban_user(db: AsyncSession, user: User, *, by: User, note: str | None) -> None:
    user.status, user.banned_at, user.ban_reason = "active", None, None
    user.false_report_warnings = 0
    owned = (await db.execute(select(Farm.id).where(Farm.owner_id == user.id))).scalars().all()
    for fid in owned:
        await lift_halts(db, fid, "admin_ban", by=by.id, note=note)
    await notify(db, user.id, "unbanned", title_key="notif.unbanned.title", body_key="notif.unbanned.body",
                 severity="success", channels=("sms", "email"))
    await audit(db, by.id, "user.unban", "user", user.id, {"note": note})


async def issue_legal_warning(db: AsyncSession, admin: User, case: SafetyCase, text: str) -> None:
    farm = await db.get(Farm, case.farm_id)
    case.legal_warning_text, case.status, case.decided_by_id, case.decided_at = text[:5000], "legal_warning", admin.id, now_utc()
    db.add(OwnerWarning(owner_id=farm.owner_id, farm_id=farm.id, branch_id=case.branch_id, kind="legal",
                        message_key="warning.legal", params={"farm": farm.name}, delivered_at=now_utc()))
    await notify(db, farm.owner_id, "legal_warning", title_key="notif.legal_warning.title", body_key=None, body=text[:2000],
                 params={"farm": farm.name}, severity="danger", link=f"/farms/{farm.id}", channels=("email", "sms", "whatsapp"))
    await audit(db, admin.id, "case.legal_warning", "safety_case", case.id, {})


async def order_compensation(db: AsyncSession, admin: User, case: SafetyCase, per_worker: Decimal) -> int:
    """Compensation goes to ALL workers of the branch → protects reporters' anonymity."""
    farm = await db.get(Farm, case.farm_id)
    stmt = select(FarmMember).where(FarmMember.farm_id == farm.id, FarmMember.status != "ended", FarmMember.role.in_(WORKER_ROLES))
    if case.branch_id:
        stmt = stmt.where(FarmMember.branch_id == case.branch_id)
    members = (await db.execute(stmt)).scalars().all()
    for m in members:
        db.add(DueAdjustment(member_id=m.id, farm_id=farm.id, amount=per_worker, reason="compensation",
                             note=f"Safety case {case.id}", case_id=case.id, created_by_id=admin.id))
    case.compensation_per_worker, case.status, case.decided_by_id, case.decided_at = per_worker, "compensation_ordered", admin.id, now_utc()
    await notify(db, farm.owner_id, "compensation_ordered", title_key="notif.compensation_owner.title",
                 body_key="notif.compensation_owner.body", params={"amount": format_inr(per_worker), "count": len(members), "farm": farm.name},
                 severity="danger", link=f"/farms/{farm.id}?tab=payroll", channels=("email", "sms"))
    await notify_many(db, [m.user_id for m in members], "compensation_worker", title_key="notif.compensation_worker.title",
                      body_key="notif.compensation_worker.body", params={"amount": format_inr(per_worker), "farm": farm.name},
                      severity="success")
    await audit(db, admin.id, "case.compensation", "safety_case", case.id, {"per_worker": str(per_worker), "workers": len(members)})
    return len(members)


async def resolve_case(db: AsyncSession, admin: User, case: SafetyCase, note: str | None) -> None:
    case.status, case.decided_by_id, case.decided_at = "resolved", admin.id, now_utc()
    await lift_halts(db, case.farm_id, "safety_reports", branch_id=case.branch_id, by=admin.id, note=note)
    farm = await db.get(Farm, case.farm_id)
    await notify(db, farm.owner_id, "safety_resolved", title_key="notif.safety_resolved.title", body_key="notif.safety_resolved.body",
                 params={"farm": farm.name}, severity="success", link=f"/farms/{farm.id}")
    await audit(db, admin.id, "case.resolve", "safety_case", case.id, {"note": note})


async def pending_survey_for(db: AsyncSession, user: User) -> WellbeingSurvey | None:
    return (await db.execute(select(WellbeingSurvey).where(
        WellbeingSurvey.user_id == user.id, WellbeingSurvey.status == "pending",
        WellbeingSurvey.survey_date >= today_ist() - timedelta(days=3)).order_by(WellbeingSurvey.survey_date.desc()))).scalars().first()


async def expire_old_surveys(db: AsyncSession) -> int:
    res = await db.execute(update(WellbeingSurvey).where(
        WellbeingSurvey.status == "pending", WellbeingSurvey.survey_date < today_ist() - timedelta(days=3)).values(status="expired"))
    return res.rowcount or 0
