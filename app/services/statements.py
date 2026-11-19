"""Credit / debit statements — a person's own and, for the owner and co-owners, each farm's — shown in the account page
and in the farm's transaction history.

Constant queries for any number of entries: ONE aggregate (opening balance + the period's credits, debits and tax notes)
and ONE page of rows whose running balance comes from a window function. "info" lines (tax withheld) are shown for the
record and never move the balance. Business dates are Indian dates."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LedgerEntry, PersonalLedgerEntry
from app.utils import today_ist

PER_PAGE = 50
ZERO = Decimal("0")
EPOCH = date(2000, 1, 1)


@dataclass
class Statement:
    month: str                     # "2026-11" or "all"
    start: date
    end: date
    opening: Decimal
    credits: Decimal
    debits: Decimal
    info: Decimal                  # tax withheld etc. — for the record, not in the balance
    count: int
    page: int
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def closing(self) -> Decimal:
        return self.opening + self.credits - self.debits

    @property
    def pages(self) -> int:
        return max(1, -(-self.count // PER_PAGE))

    @property
    def prev_month(self) -> str | None:
        return None if self.month == "all" else f"{self.start - timedelta(days=1):%Y-%m}"

    @property
    def next_month(self) -> str | None:
        if self.month == "all":
            return None
        nxt = self.end + timedelta(days=1)
        return f"{nxt:%Y-%m}" if nxt <= today_ist() else None


def period(month: str | None) -> tuple[str, date, date]:
    """"2026-11" → that month; "all" → everything up to today; anything else → the current Indian month."""
    today = today_ist()
    if month == "all":
        return "all", EPOCH, today
    try:
        y, m = (int(x) for x in (month or "").split("-"))
        first = date(y, m, 1)
        if not 2000 <= y <= today.year:
            raise ValueError
    except (ValueError, TypeError):
        first = today.replace(day=1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return f"{first:%Y-%m}", first, last


async def _statement(db: AsyncSession, model: Any, owner_filter: Any, day: Any, month: str | None, page: int,
                     extra: dict[str, Any]) -> Statement:
    key, start, end = period(month)
    signed = case((model.direction == "in", model.amount), (model.direction == "out", -model.amount), else_=ZERO)
    in_period = and_(day >= start, day <= end)
    opening, credits, debits, info, count = (await db.execute(select(
        func.coalesce(func.sum(signed).filter(day < start), ZERO),
        func.coalesce(func.sum(model.amount).filter(in_period, model.direction == "in"), ZERO),
        func.coalesce(func.sum(model.amount).filter(in_period, model.direction == "out"), ZERO),
        func.coalesce(func.sum(model.amount).filter(in_period, model.direction == "info"), ZERO),
        func.count().filter(in_period),
    ).where(owner_filter))).one()
    page = max(1, min(int(page or 1), max(1, -(-int(count) // PER_PAGE))))
    running = func.sum(signed).over(order_by=(day, model.created_at, model.id), rows=(None, 0))
    sub = (select(model.id.label("id"), day.label("day"), model.created_at.label("at"), model.entry_type.label("type"),
                  model.direction.label("direction"), model.amount.label("amount"), model.description.label("description"),
                  *[col.label(name) for name, col in extra.items()], running.label("run"))
           .where(owner_filter, in_period).subquery())
    rows = (await db.execute(select(sub).order_by(sub.c.day, sub.c.at, sub.c.id)
                             .offset((page - 1) * PER_PAGE).limit(PER_PAGE))).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["balance"] = opening + (d.pop("run") or ZERO)
        d["credit"] = d["amount"] if d["direction"] == "in" else None
        d["debit"] = d["amount"] if d["direction"] == "out" else None
        out.append(d)
    return Statement(key, start, end, opening, credits, debits, info, int(count), page, out)


async def personal_statement(db: AsyncSession, user_id: uuid.UUID, month: str | None, page: int = 1) -> Statement:
    M = PersonalLedgerEntry
    day = func.date(func.timezone("Asia/Kolkata", M.created_at))      # the Indian date of the entry
    return await _statement(db, M, M.user_id == user_id, day, month, page,
                            {"who": M.farm_name, "farm_id": M.farm_id, "reference": M.reference_id})


async def farm_statement(db: AsyncSession, farm_id: uuid.UUID, month: str | None, page: int = 1) -> Statement:
    M = LedgerEntry
    return await _statement(db, M, M.farm_id == farm_id, M.occurred_on, month, page,
                            {"who": M.counterparty_name, "category": M.category, "by": M.created_by_name, "upload_id": M.upload_id})
