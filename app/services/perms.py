"""Farm-level roles & permissions (owner > co-owner > manager > accountant > supervisor > employee).

A leader under an INDIVIDUAL notice (being let go) keeps working and is paid until the last day, but holds no leadership
power meanwhile: a manager on the way out cannot pay, sack, hire or change anything, and stops receiving the farm's
private alerts. A notice caused by a farm/branch CLOSURE suspends nobody — those leaders run the shutdown.
"""
from __future__ import annotations

import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.errors import Forbidden, NotFound
from app.models import Farm, FarmMember, User

ALL_ROLES = {"owner", "co_owner", "manager", "accountant", "supervisor", "employee"}
PERMS: dict[str, set[str]] = {
    "view": ALL_ROLES,
    "settings": {"owner", "co_owner"},
    "members": {"owner", "co_owner", "manager"},
    "salary": {"owner"},                       # salaries are determined by the farm owner
    "payroll": {"owner", "co_owner", "accountant", "manager"},
    "ledger": {"owner", "co_owner", "manager", "accountant"},
    "shop": {"owner", "co_owner", "manager"},
    "orders": {"owner", "co_owner", "manager"},
    "crops": {"owner", "co_owner", "manager", "supervisor"},
    "emergency": {"owner", "co_owner", "manager"},
    "answer": {"owner", "co_owner", "manager"},
    "insurance": {"owner", "co_owner", "manager"},
    "hiring": {"owner", "co_owner", "manager"},
    "vote": {"owner", "co_owner"},
}
LEADER_ROLES = ("co_owner", "manager", "accountant")
RANK = {"employee": 0, "supervisor": 1, "accountant": 2, "manager": 3, "co_owner": 4, "owner": 5}


async def has_worker_job(db, user_id) -> bool:
    """Does the person work on a farm right now (or is serving notice)? Worker protections follow the job, not the
    account type — a worker who also became a farm owner keeps them."""
    from sqlalchemy import select

    from app.models import FarmMember
    from app.models.farm import WORKER_ROLES

    return (await db.execute(select(FarmMember.id).where(FarmMember.user_id == user_id, FarmMember.role.in_(WORKER_ROLES),
                                                         FarmMember.status.in_(("active", "notice"))).limit(1))).first() is not None


def outranks(actor: str | None, target: str | None) -> bool:
    """Strictly higher in the hierarchy: nobody acts on a peer or on someone above them."""
    return actor in RANK and target in RANK and RANK[actor] > RANK[target]


def effective_role(role: str, status: str, notice_kind: str | None) -> str | None:
    if status == "ended":
        return None
    if status == "notice" and notice_kind == "individual" and role != "co_owner":
        return "employee"           # still works here and is paid; no powers
    return role


def holds_role() -> ColumnElement[bool]:
    """SQL twin of effective_role(): the member currently holds the powers of their role (NULL-safe)."""
    return and_(FarmMember.status != "ended",
                or_(FarmMember.status != "notice", FarmMember.notice_kind.is_distinct_from("individual")))


async def farm_role(db: AsyncSession, farm: Farm, user: User) -> str | None:
    if user.status != "active":         # a banned / deleted account holds no farm powers anywhere (its own pay stays visible)
        return None
    if farm.owner_id == user.id:
        return "owner"
    row = (await db.execute(select(FarmMember.role, FarmMember.status, FarmMember.notice_kind).where(
        FarmMember.farm_id == farm.id, FarmMember.user_id == user.id, FarmMember.status != "ended"))).first()
    return effective_role(*row) if row else None


async def roles_by_farm(db: AsyncSession, user: User) -> dict[uuid.UUID, str]:
    """All farms the user belongs to → effective role (2 queries total). Banned / deleted accounts: none."""
    out: dict[uuid.UUID, str] = {}
    if user.status != "active":
        return out
    for fid, role, status, kind in (await db.execute(select(FarmMember.farm_id, FarmMember.role, FarmMember.status,
                                                              FarmMember.notice_kind).where(
            FarmMember.user_id == user.id, FarmMember.status != "ended"))).all():
        out[fid] = effective_role(role, status, kind) or role
    for (fid,) in (await db.execute(select(Farm.id).where(Farm.owner_id == user.id))).all():
        out[fid] = "owner"
    return out


async def require_perm(db: AsyncSession, farm_id: uuid.UUID | str, user: User, perm: str, *, lock: bool = False) -> tuple[Farm, str]:
    try:
        fid = uuid.UUID(str(farm_id))
    except ValueError as exc:
        raise NotFound("errors.farm_not_found") from exc
    stmt = select(Farm).where(Farm.id == fid)
    if lock:
        stmt = stmt.with_for_update()
    farm = (await db.execute(stmt)).scalar_one_or_none()
    if not farm:
        raise NotFound("errors.farm_not_found")
    role = await farm_role(db, farm, user)
    if role is None or role not in PERMS[perm]:
        raise Forbidden("errors.no_farm_permission")
    return farm, role


def can(role: str | None, perm: str) -> bool:
    return bool(role) and role in PERMS.get(perm, set())


async def coowner_ids(db: AsyncSession, farm_id: uuid.UUID) -> list[uuid.UUID]:
    return list((await db.execute(select(FarmMember.user_id).where(
        FarmMember.farm_id == farm_id, FarmMember.role == "co_owner", FarmMember.status != "ended"))).scalars())
