"""Payroll engine.

* Salary accrues PER DAY from the date of joining (monthly salaries pro-rated per calendar day; daily wages for
  every non-holiday day — farmers are paid even if the owner shuts the branch on a working day).
* If a branch is open on a holiday / weekly-off, every employee earns +holiday_premium_pct for that day.
* TDS is withheld per day at the effective annualised rate (usually 0 for farm workers).
* Salary for month M is due on `billing_day` of month M+1 (set by the superadmin).

All functions work on whole farms with a constant number of queries (no N+1).
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Branch, BranchDay, DueAdjustment, Farm, FarmMember, Holiday, SalaryAccrual, SalaryPayment
from app.services.india_data import national_holidays_for
from app.services.settings_store import get_all
from app.services.tax import tds_rate
from app.utils import add_months, days_in_month, today_ist

D0 = Decimal("0.00")
CENT = Decimal("0.01")


def q2(x: Decimal) -> Decimal:
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------ calendar
def due_date_for_month(year: int, month: int, billing_day: int) -> date:
    return add_months(date(year, month, 1), 1, day=min(int(billing_day), 28))


def overdue_cutoff(as_of: date, billing_day: int, grace_days: int) -> date:
    """Last work date whose salary is already past due (due date + grace < as_of)."""
    prev_end = as_of.replace(day=1) - timedelta(days=1)
    due_prev = due_date_for_month(prev_end.year, prev_end.month, billing_day)
    if due_prev + timedelta(days=grace_days) < as_of:
        return prev_end
    return prev_end.replace(day=1) - timedelta(days=1)


def next_due_date(as_of: date, billing_day: int) -> date:
    prev_end = as_of.replace(day=1) - timedelta(days=1)
    d = due_date_for_month(prev_end.year, prev_end.month, billing_day)
    return d if d >= as_of else due_date_for_month(as_of.year, as_of.month, billing_day)


class Calendar:
    """Holidays + branch open/closed overrides for a date range (3 queries)."""

    def __init__(self) -> None:
        self.holidays: set[tuple[date, str | None]] = set()
        self.overrides: dict[tuple[uuid.UUID, date], bool] = {}
        self.branches: dict[uuid.UUID, Branch] = {}

    @classmethod
    async def load(cls, db: AsyncSession, branches: Iterable[Branch], start: date, end: date) -> "Calendar":
        cal = cls()
        cal.branches = {b.id: b for b in branches}
        for y in range(start.year, end.year + 1):
            for d, _ in national_holidays_for(y):
                cal.holidays.add((d, None))
        for d, st in (await db.execute(select(Holiday.day, Holiday.state).where(Holiday.day.between(start, end)))).all():
            cal.holidays.add((d, st))
        if cal.branches:
            rows = await db.execute(select(BranchDay.branch_id, BranchDay.day, BranchDay.is_open).where(
                BranchDay.branch_id.in_(list(cal.branches)), BranchDay.day.between(start, end)))
            for bid, d, is_open in rows.all():
                cal.overrides[(bid, d)] = is_open
        return cal

    def is_holiday(self, branch: Branch | None, d: date) -> bool:
        if (d, None) in self.holidays:
            return True
        if branch is not None:
            if branch.state and (d, branch.state) in self.holidays:
                return True
            if d.weekday() in (branch.weekly_offs or []):
                return True
        return False

    def is_open(self, branch: Branch | None, d: date) -> bool:
        hol = self.is_holiday(branch, d)
        if branch is not None and (branch.id, d) in self.overrides:
            return self.overrides[(branch.id, d)]
        return not hol


def accrue_day(member: FarmMember, d: date, *, holiday: bool, open_: bool, premium_pct: Decimal, rate: Decimal) -> dict[str, Any]:
    amount = Decimal(member.salary_amount or 0)
    if member.salary_type == "monthly":
        unit = amount / days_in_month(d)
        base = unit
    else:
        unit = amount
        base = amount if (not holiday or open_) else D0
    premium = unit * premium_pct / 100 if (holiday and open_) else D0
    gross = q2(base + premium)
    tds = q2(gross * rate)
    return {"base": q2(base), "holiday_premium": q2(premium), "gross": gross, "tds": tds, "net": gross - tds, "is_holiday": holiday}


async def ensure_accruals_for_farm(db: AsyncSession, farm_id: uuid.UUID, upto: date | None = None) -> int:
    upto = upto or today_ist()
    members = (await db.execute(select(FarmMember).where(
        FarmMember.farm_id == farm_id, FarmMember.salary_amount > 0, FarmMember.joined_on <= upto,
        or_(FarmMember.ended_on.is_(None), FarmMember.ended_on >= FarmMember.joined_on)))).scalars().all()
    if not members:
        return 0
    last = dict((await db.execute(select(SalaryAccrual.member_id, func.max(SalaryAccrual.work_date)).where(
        SalaryAccrual.member_id.in_([m.id for m in members])).group_by(SalaryAccrual.member_id))).all())
    plans: list[tuple[FarmMember, date, date]] = []
    for m in members:
        start = (last[m.id] + timedelta(days=1)) if m.id in last else m.joined_on
        end = min(upto, m.ended_on) if m.ended_on else upto
        if start <= end:
            plans.append((m, start, end))
    if not plans:
        return 0
    branches = (await db.execute(select(Branch).where(Branch.farm_id == farm_id))).scalars().all()
    main = next((b for b in branches if b.is_main), branches[0] if branches else None)
    cal = await Calendar.load(db, branches, min(p[1] for p in plans), upto)
    cfg = await get_all(db)
    premium_pct = Decimal(str(cfg["holiday_premium_pct"]))
    rows: list[dict[str, Any]] = []
    for m, start, end in plans:
        branch = cal.branches.get(m.branch_id) if m.branch_id else main
        rate = tds_rate(m.salary_type, Decimal(m.salary_amount), cfg["tax"])
        d = start
        while d <= end:
            hol = cal.is_holiday(branch, d)
            vals = accrue_day(m, d, holiday=hol, open_=cal.is_open(branch, d), premium_pct=premium_pct, rate=rate)
            rows.append({"id": uuid.uuid4(), "member_id": m.id, "farm_id": farm_id,
                         "branch_id": branch.id if branch else None, "work_date": d, **vals})
            d += timedelta(days=1)
    for i in range(0, len(rows), 1000):
        await db.execute(pg_insert(SalaryAccrual).values(rows[i : i + 1000])
                         .on_conflict_do_nothing(index_elements=["member_id", "work_date"]))
    return len(rows)


async def recompute_day(db: AsyncSession, branch: Branch, d: date) -> None:
    """Re-price an already accrued day (e.g. branch marked open on a holiday after the fact)."""
    scope = [FarmMember.branch_id == branch.id]
    if branch.is_main:
        scope.append(FarmMember.branch_id.is_(None))
    members = (await db.execute(select(FarmMember).where(
        FarmMember.farm_id == branch.farm_id, FarmMember.joined_on <= d, or_(*scope),
        or_(FarmMember.ended_on.is_(None), FarmMember.ended_on >= d)))).scalars().all()
    if not members:
        return
    cal = await Calendar.load(db, [branch], d, d)
    cfg = await get_all(db)
    premium = Decimal(str(cfg["holiday_premium_pct"]))
    existing = {a.member_id: a for a in (await db.execute(select(SalaryAccrual).where(
        SalaryAccrual.member_id.in_([m.id for m in members]), SalaryAccrual.work_date == d))).scalars()}
    for m in members:
        a = existing.get(m.id)
        if not a:
            continue
        vals = accrue_day(m, d, holiday=cal.is_holiday(branch, d), open_=cal.is_open(branch, d), premium_pct=premium,
                          rate=tds_rate(m.salary_type, Decimal(m.salary_amount), cfg["tax"]))
        for k, v in vals.items():
            setattr(a, k, v)


@dataclass
class MemberMoney:
    member_id: uuid.UUID
    gross: Decimal = D0
    tds: Decimal = D0
    net: Decimal = D0
    paid: Decimal = D0
    adjustments: Decimal = D0
    tds_recorded: Decimal = D0
    net_due_by_cutoff: Decimal = D0
    net_due_by_prev_month: Decimal = D0
    due: Decimal = D0
    overdue: Decimal = D0
    due_soon: Decimal = D0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items() if k != "extra"} | {"member_id": str(self.member_id)}


async def farm_money(db: AsyncSession, farm_id: uuid.UUID, *, as_of: date | None = None,
                     members: list[FarmMember] | None = None) -> dict[uuid.UUID, MemberMoney]:
    """Dues for every member of a farm in 5 aggregate queries."""
    as_of = as_of or today_ist()
    cfg = await get_all(db)
    billing_day, grace = int(cfg["billing_day"]), int(cfg["salary_grace_days"])
    cutoff = overdue_cutoff(as_of, billing_day, grace)
    prev_end = as_of.replace(day=1) - timedelta(days=1)
    if members is None:
        members = list((await db.execute(select(FarmMember).where(FarmMember.farm_id == farm_id))).scalars())
    out = {m.id: MemberMoney(m.id) for m in members}
    if not out:
        return out
    ids = list(out)
    acc = await db.execute(select(
        SalaryAccrual.member_id, func.coalesce(func.sum(SalaryAccrual.gross), 0), func.coalesce(func.sum(SalaryAccrual.tds), 0),
        func.coalesce(func.sum(SalaryAccrual.net), 0),
        func.coalesce(func.sum(SalaryAccrual.net).filter(SalaryAccrual.work_date <= cutoff), 0),
        func.coalesce(func.sum(SalaryAccrual.net).filter(SalaryAccrual.work_date <= prev_end), 0),
    ).where(SalaryAccrual.member_id.in_(ids)).group_by(SalaryAccrual.member_id))
    for mid, gross, tds, net, net_cut, net_prev in acc.all():
        mm = out[mid]
        mm.gross, mm.tds, mm.net, mm.net_due_by_cutoff, mm.net_due_by_prev_month = map(q2, (gross, tds, net, net_cut, net_prev))
    pays = await db.execute(select(SalaryPayment.member_id, func.coalesce(func.sum(SalaryPayment.amount), 0),
                                   func.coalesce(func.sum(SalaryPayment.tds_portion), 0))
                            .where(SalaryPayment.member_id.in_(ids)).group_by(SalaryPayment.member_id))
    for mid, paid, tds_rec in pays.all():
        out[mid].paid, out[mid].tds_recorded = q2(paid), q2(tds_rec)
    adj_due_before = as_of - timedelta(days=7)
    adjs = await db.execute(select(DueAdjustment.member_id, func.coalesce(func.sum(DueAdjustment.amount), 0),
                                   func.coalesce(func.sum(DueAdjustment.amount).filter(func.date(DueAdjustment.created_at) <= adj_due_before), 0))
                            .where(DueAdjustment.member_id.in_(ids)).group_by(DueAdjustment.member_id))
    adj_overdue: dict[uuid.UUID, Decimal] = {}
    for mid, total, overdue_part in adjs.all():
        out[mid].adjustments = q2(total)
        adj_overdue[mid] = q2(overdue_part)
    by_id = {m.id: m for m in members}
    for mid, mm in out.items():
        m = by_id[mid]
        mm.due = max(D0, mm.net + mm.adjustments - mm.paid)
        final_date = m.ended_on or (m.notice_end_date if m.status == "notice" else None)
        if final_date and final_date + timedelta(days=grace) < as_of:
            mm.overdue = mm.due  # final settlement is past due
        else:
            mm.overdue = max(D0, mm.net_due_by_cutoff + adj_overdue.get(mid, D0) - mm.paid)
        mm.due_soon = max(D0, mm.net_due_by_prev_month + mm.adjustments - mm.paid)
    return out


async def farms_dues_summary(db: AsyncSession, farm_ids: list[uuid.UUID], as_of: date | None = None) -> dict[uuid.UUID, dict[str, Decimal]]:
    """Per-farm totals (due / overdue) for dashboards — 4 queries for any number of farms."""
    as_of = as_of or today_ist()
    out = {fid: {"due": D0, "overdue": D0, "due_soon": D0, "employees": 0} for fid in farm_ids}
    if not farm_ids:
        return out
    cfg = await get_all(db)
    cutoff = overdue_cutoff(as_of, int(cfg["billing_day"]), int(cfg["salary_grace_days"]))
    prev_end = as_of.replace(day=1) - timedelta(days=1)
    acc = (await db.execute(select(
        SalaryAccrual.farm_id, func.coalesce(func.sum(SalaryAccrual.net), 0),
        func.coalesce(func.sum(SalaryAccrual.net).filter(SalaryAccrual.work_date <= cutoff), 0),
        func.coalesce(func.sum(SalaryAccrual.net).filter(SalaryAccrual.work_date <= prev_end), 0),
    ).where(SalaryAccrual.farm_id.in_(farm_ids)).group_by(SalaryAccrual.farm_id))).all()
    net = {r[0]: r[1] for r in acc}
    net_cut = {r[0]: r[2] for r in acc}
    net_prev = {r[0]: r[3] for r in acc}
    paid = dict((await db.execute(select(SalaryPayment.farm_id, func.coalesce(func.sum(SalaryPayment.amount), 0))
                                  .where(SalaryPayment.farm_id.in_(farm_ids)).group_by(SalaryPayment.farm_id))).all())
    adj = dict((await db.execute(select(DueAdjustment.farm_id, func.coalesce(func.sum(DueAdjustment.amount), 0))
                                 .where(DueAdjustment.farm_id.in_(farm_ids)).group_by(DueAdjustment.farm_id))).all())
    emp = dict((await db.execute(select(FarmMember.farm_id, func.count()).where(
        FarmMember.farm_id.in_(farm_ids), FarmMember.status != "ended").group_by(FarmMember.farm_id))).all())
    for fid in farm_ids:
        n, nc, npv, p, a = (Decimal(x.get(fid, 0)) for x in (net, net_cut, net_prev, paid, adj))
        out[fid] = {"due": q2(max(D0, n + a - p)), "overdue": q2(max(D0, nc - p)), "due_soon": q2(max(D0, npv + a - p)),
                    "employees": int(emp.get(fid, 0))}
    return out


async def total_farm_due(db: AsyncSession, farm_id: uuid.UUID, branch_id: uuid.UUID | None = None) -> Decimal:
    """Exact outstanding salary (incl. adjustments) for a farm or one branch — used to block closures/deletions."""
    await ensure_accruals_for_farm(db, farm_id)
    stmt = select(FarmMember).where(FarmMember.farm_id == farm_id)
    if branch_id is not None:
        stmt = stmt.where(FarmMember.branch_id == branch_id)
    members = list((await db.execute(stmt)).scalars())
    money = await farm_money(db, farm_id, members=members)
    return q2(sum((m.due for m in money.values()), D0))


async def member_rows(db: AsyncSession, farm_id: uuid.UUID) -> list[dict[str, Any]]:
    """Payroll table: members + user names + money in constant queries."""
    from app.models import User

    await ensure_accruals_for_farm(db, farm_id)
    rows = (await db.execute(select(FarmMember, User.full_name, User.phone, User.photo_path, User.status)
                             .join(User, User.id == FarmMember.user_id)
                             .where(FarmMember.farm_id == farm_id)
                             .order_by(FarmMember.status, FarmMember.joined_on))).all()
    money = await farm_money(db, farm_id, members=[r[0] for r in rows])
    out = []
    for m, name, phone, photo, ustatus in rows:
        out.append({"member": m, "name": name, "phone": phone, "photo": photo, "user_status": ustatus, "money": money[m.id]})
    return out


def group_by(items: Iterable[Any], key) -> dict[Any, list[Any]]:
    d: dict[Any, list[Any]] = defaultdict(list)
    for it in items:
        d[key(it)].append(it)
    return d
