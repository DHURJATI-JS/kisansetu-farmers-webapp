"""Superadmin-editable platform settings (billing day, thresholds, tax slabs …) with a small cache."""
from __future__ import annotations

import copy
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PlatformSetting
from app.utils import now_utc

DEFAULTS: dict[str, Any] = {
    "billing_day": 7,                   # salaries for month M are due by this day of month M+1
    "salary_warning_days": 3,           # AI warns the owner this many days before the due date
    "salary_grace_days": 0,             # after due date + grace → operations halted
    "notice_days": 30,                  # 1 month notice for kick-outs / closures
    "max_work_hours": 8,
    "holiday_premium_pct": 10,
    "max_join_radius_km": 30,
    "calamity_radius_km": 200,
    "bad_report_threshold": 2,          # distinct workers reporting within window → branch halted
    "bad_report_window_days": 7,
    "false_report_ban_threshold": 3,
    "national_floor_wage": 178,         # National Floor Level Minimum Wage (₹/day). Admin must keep updated.
    "min_worker_age": 18,
    "approval_sla_hours": "48-72",
    "data_retention_days": 30,
    "compensation_default_days": 7,
    "insurance_expiry_warning_days": 15,
    "tax": {
        "label": "New tax regime defaults (Finance Act 2025). Verify every financial year.",
        "standard_deduction": 75000,
        "rebate_limit": 1200000,
        "rebate_max": 60000,
        "cess_pct": 4,
        "slabs": [[400000, 0], [800000, 5], [1200000, 10], [1600000, 15], [2000000, 20], [2400000, 25], [None, 30]],
    },
    "authentic_job_sites": [
        "ncs.gov.in", "nrega.nic.in", "eshram.gov.in", "apna.co", "naukri.com", "in.indeed.com",
        "workindia.in", "shine.com", "timesjobs.com", "foundit.in",
    ],
    # official domains a government-scheme link may point to (a host matches the domain or any sub-domain)
    "scheme_domains": ["gov.in", "nic.in", "maandhan.in"],
    "scheme_review_days": 180,
}

_cache: dict[str, Any] = {}
_loaded_at = 0.0
TTL = 30.0
_VER_KEY = "ks:settings:ver"   # bumped after a change is committed → every worker reloads within ~2 s
_ver_seen: Any = None
_ver_checked = 0.0


async def _load(db: AsyncSession) -> dict[str, Any]:
    global _cache, _loaded_at, _ver_seen, _ver_checked
    now = time.monotonic()
    if now - _loaded_at < TTL and _cache:
        if now - _ver_checked < 2.0:
            return _cache
        _ver_checked = now
        try:
            from app.services.redis_client import kv

            ver = await kv().get(_VER_KEY)
        except Exception:
            ver = _ver_seen
        if ver == _ver_seen:
            return _cache
        _ver_seen = ver
    rows = (await db.execute(select(PlatformSetting))).scalars().all()
    merged = copy.deepcopy(DEFAULTS)
    for r in rows:
        merged[r.key] = r.value
    _cache, _loaded_at = merged, time.monotonic()
    return _cache


async def get_all(db: AsyncSession) -> dict[str, Any]:
    return dict(await _load(db))


async def get_setting(db: AsyncSession, key: str) -> Any:
    return (await _load(db)).get(key, DEFAULTS.get(key))


async def set_setting(db: AsyncSession, key: str, value: Any, by: uuid.UUID | None) -> None:
    global _loaded_at
    row = await db.get(PlatformSetting, key)
    if row is None:
        db.add(PlatformSetting(key=key, value=value, updated_at=now_utc(), updated_by_id=by))
    else:
        row.value = value
        row.updated_at = now_utc()
        row.updated_by_id = by
    _loaded_at = 0.0


def invalidate() -> None:
    global _loaded_at
    _loaded_at = 0.0


async def bump_version() -> None:
    """Tell every worker to reload settings. Call after the change is committed (realtime.after_commit)."""
    invalidate()
    try:
        from app.services.redis_client import kv

        await kv().incr(_VER_KEY)
    except Exception:
        pass
