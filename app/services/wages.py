"""Minimum-wage compliance: max(National Floor Level Minimum Wage, state rate set by superadmin)."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MinimumWage
from app.services.settings_store import get_setting

WORKING_DAYS_PER_MONTH = 26


async def min_daily_wage(db: AsyncSession, state: str | None) -> Decimal:
    floor = Decimal(str(await get_setting(db, "national_floor_wage")))
    if state:
        row = (await db.execute(select(MinimumWage.daily_wage).where(MinimumWage.state == state))).scalar_one_or_none()
        if row is not None:
            floor = max(floor, Decimal(row))
    return floor


def daily_equivalent(salary_type: str, amount: Decimal) -> Decimal:
    amount = Decimal(amount)
    return amount if salary_type == "daily" else (amount / WORKING_DAYS_PER_MONTH)


async def check_min_wage(db: AsyncSession, state: str | None, salary_type: str, amount: Decimal) -> tuple[bool, Decimal]:
    """Returns (ok, minimum in the same unit as `salary_type`)."""
    floor = await min_daily_wage(db, state)
    minimum = floor if salary_type == "daily" else floor * WORKING_DAYS_PER_MONTH
    return Decimal(amount) >= minimum, minimum.quantize(Decimal("0.01"))
