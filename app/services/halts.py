"""Operational halts. A halt (farm-wide when branch_id is NULL) stops hiring, the shop and new operations.
Salary keeps accruing during halts (workers are protected); payments are always allowed so dues can be cleared."""
from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any, Iterable

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Halt
from app.utils import now_utc, to_jsonable

REASONS = {
    "salary_overdue": "halt.salary_overdue",
    "settlement_pending": "halt.settlement_pending",
    "safety_reports": "halt.safety_reports",
    "calamity_no_insurance": "halt.calamity_no_insurance",
    "insurance_expired": "halt.insurance_expired",
    "emergency": "halt.emergency",
    "closure": "halt.closure",
    "admin_ban": "halt.admin_ban",
}
ADMIN_ONLY_LIFT = {"safety_reports", "admin_ban"}
OWNER_LIFT = {"emergency"}


async def active_halts(db: AsyncSession, farm_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[Halt]]:
    ids = list(set(farm_ids))
    out: dict[uuid.UUID, list[Halt]] = defaultdict(list)
    if not ids:
        return out
    rows = (await db.execute(select(Halt).where(Halt.farm_id.in_(ids), Halt.lifted_at.is_(None))
                             .order_by(Halt.created_at))).scalars().all()
    for h in rows:
        out[h.farm_id].append(h)
    return out


def halts_for_branch(farm_halts: list[Halt], branch_id: uuid.UUID | None) -> list[Halt]:
    return [h for h in farm_halts if h.branch_id is None or h.branch_id == branch_id]


async def branch_halts(db: AsyncSession, farm_id: uuid.UUID, branch_id: uuid.UUID | None) -> list[Halt]:
    stmt = select(Halt).where(Halt.farm_id == farm_id, Halt.lifted_at.is_(None))
    if branch_id is not None:
        stmt = stmt.where(or_(Halt.branch_id.is_(None), Halt.branch_id == branch_id))
    return list((await db.execute(stmt)).scalars())


async def is_halted(db: AsyncSession, farm_id: uuid.UUID, branch_id: uuid.UUID | None, ignore: Iterable[str] = ()) -> bool:
    ign = set(ignore)
    return any(h.reason not in ign for h in await branch_halts(db, farm_id, branch_id))


async def halted_scope(db: AsyncSession) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """(farm ids halted farm-wide, branch ids halted) — one query, for marketplace/job filtering."""
    from app.models import Branch

    farms, branches = set(), set()
    rows = await db.execute(select(Halt.farm_id, Halt.branch_id).outerjoin(Branch, Branch.id == Halt.branch_id).where(
        Halt.lifted_at.is_(None), or_(Halt.branch_id.is_(None), Branch.farm_id == Halt.farm_id)))   # never another farm's branch
    for fid, bid in rows.all():
        (branches.add(bid) if bid else farms.add(fid))
    return farms, branches


async def ensure_halt(db: AsyncSession, farm_id: uuid.UUID, branch_id: uuid.UUID | None, reason: str, *,
                      params: dict[str, Any] | None = None, details: dict[str, Any] | None = None,
                      by: uuid.UUID | None = None) -> tuple[Halt, bool]:
    stmt = select(Halt).where(Halt.farm_id == farm_id, Halt.reason == reason, Halt.lifted_at.is_(None))
    stmt = stmt.where(Halt.branch_id.is_(None) if branch_id is None else Halt.branch_id == branch_id)
    existing = (await db.execute(stmt)).scalars().first()
    if existing:
        return existing, False
    h = Halt(farm_id=farm_id, branch_id=branch_id, reason=reason, message_key=REASONS.get(reason, "halt.generic"),
             params=to_jsonable(params or {}), details=to_jsonable(details or {}), created_by_id=by)
    db.add(h)
    await db.flush()
    return h, True


async def lift_halts(db: AsyncSession, farm_id: uuid.UUID, reason: str, *, branch_id: uuid.UUID | None | str = "any",
                     by: uuid.UUID | None = None, note: str | None = None) -> int:
    stmt = update(Halt).where(Halt.farm_id == farm_id, Halt.reason == reason, Halt.lifted_at.is_(None))
    if branch_id != "any":
        stmt = stmt.where(Halt.branch_id.is_(None) if branch_id is None else Halt.branch_id == branch_id)
    res = await db.execute(stmt.values(lifted_at=now_utc(), lifted_by_id=by, lift_note=note))
    return res.rowcount or 0
