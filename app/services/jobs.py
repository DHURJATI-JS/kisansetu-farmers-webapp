"""Job discovery.

* Internal: open postings within the joining radius (≤30 km), excluding halted / closing branches.
* External: when nothing is available nearby, jobs hunted DAILY from authentic sites (Google Programmable Search,
  restricted to an admin-managed allow-list) are shown — clearly labelled as governed by the source site's policies.
"""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Branch, BranchCrop, ExternalJob, Farm, JobPosting, User
from app.services.geo import bbox_around, city_lookup, haversine_km
from app.services.halts import halted_scope
from app.services.http import http
from app.services.india_data import CITIES
from app.services.settings_store import get_setting
from app.utils import now_utc, parse_json_loose

log = logging.getLogger("kisansetu.jobs")

OFFICIAL_PORTALS = [
    {"name": "National Career Service", "url": "https://www.ncs.gov.in/", "key": "portal.ncs"},
    {"name": "e-Shram", "url": "https://eshram.gov.in/", "key": "portal.eshram"},
    {"name": "MGNREGA", "url": "https://nrega.nic.in/", "key": "portal.mgnrega"},
    {"name": "Skill India Digital Hub", "url": "https://www.skillindiadigital.gov.in/", "key": "portal.skillindia"},
]
DEFAULT_HUNT_CITIES = ["Nashik", "Ludhiana", "Karnal", "Indore", "Guntur", "Coimbatore", "Nagpur", "Patna", "Lucknow",
                       "Jaipur", "Rajkot", "Bhubaneswar", "Guwahati", "Thrissur", "Belagavi"]


def _daily_equiv():
    return case((JobPosting.salary_type == "daily", JobPosting.salary_amount), else_=JobPosting.salary_amount / 26)


async def search_internal(db: AsyncSession, *, lat: float | None, lon: float | None, radius_km: float, q: str | None = None,
                          city: str | None = None, min_daily: Decimal | None = None, sort: str = "distance",
                          limit: int = 60) -> list[dict[str, Any]]:
    halted_farms, halted_branches = await halted_scope(db)
    stmt = (select(JobPosting, Branch, Farm.name, Farm.farming_type, Farm.terms_mode)
            .join(Branch, Branch.id == JobPosting.branch_id).join(Farm, Farm.id == JobPosting.farm_id)
            .where(JobPosting.status == "open", Farm.status == "active", Branch.status == "active", Branch.hiring_open.is_(True)))
    if halted_farms:
        stmt = stmt.where(JobPosting.farm_id.notin_(halted_farms))
    if halted_branches:
        stmt = stmt.where(JobPosting.branch_id.notin_(halted_branches))
    if lat is not None and lon is not None:
        a, b, c, d = bbox_around(lat, lon, radius_km)
        stmt = stmt.where(Branch.lat.between(a, b), Branch.lon.between(c, d))
    if city:
        stmt = stmt.where(Branch.city.ilike(f"%{city.strip()[:60]}%"))
    if q:
        like = f"%{q.strip()[:80]}%"
        stmt = stmt.where(or_(JobPosting.title.ilike(like), JobPosting.description.ilike(like), Farm.name.ilike(like)))
    if min_daily:
        stmt = stmt.where(_daily_equiv() >= min_daily)
    rows = (await db.execute(stmt.order_by(JobPosting.created_at.desc()).limit(500))).all()
    crops = defaultdict(list)
    if rows:
        for bid, name in (await db.execute(select(BranchCrop.branch_id, BranchCrop.name).where(
                BranchCrop.branch_id.in_({r[1].id for r in rows}), BranchCrop.status == "growing"))).all():
            crops[bid].append(name)
    out = []
    for job, br, farm_name, ftype, tmode in rows:
        dist = haversine_km(lat, lon, br.lat, br.lon) if lat is not None and lon is not None else None
        if dist is not None and dist > radius_km:
            continue
        daily = Decimal(job.salary_amount) if job.salary_type == "daily" else Decimal(job.salary_amount) / 26
        out.append({"job": job, "branch": br, "farm_name": farm_name, "farming_type": ftype, "terms_mode": tmode,
                    "distance_km": round(dist, 1) if dist is not None else None, "daily": daily, "crops": crops[br.id][:6]})
    key = {"distance": lambda x: (x["distance_km"] if x["distance_km"] is not None else 1e9),
           "salary": lambda x: -x["daily"], "newest": lambda x: -x["job"].created_at.timestamp()}.get(sort)
    if key:
        out.sort(key=key)
    return out[:limit]


async def search_external(db: AsyncSession, *, lat: float | None, lon: float | None, q: str | None = None, city: str | None = None,
                          min_daily: Decimal | None = None, sort: str = "distance", limit: int = 60) -> list[dict[str, Any]]:
    stmt = select(ExternalJob).where(or_(ExternalJob.expires_at.is_(None), ExternalJob.expires_at > now_utc()))
    if city:
        stmt = stmt.where(ExternalJob.city.ilike(f"%{city.strip()[:60]}%"))
    if q:
        like = f"%{q.strip()[:80]}%"
        stmt = stmt.where(or_(ExternalJob.title.ilike(like), ExternalJob.snippet.ilike(like), ExternalJob.employer.ilike(like)))
    if min_daily:
        stmt = stmt.where(or_(ExternalJob.salary_min.is_(None), ExternalJob.salary_min >= min_daily))
    jobs = (await db.execute(stmt.order_by(ExternalJob.fetched_at.desc()).limit(400))).scalars().all()
    out = []
    for j in jobs:
        dist = haversine_km(lat, lon, j.lat, j.lon) if (lat is not None and lon is not None and j.lat is not None) else None
        out.append({"job": j, "distance_km": round(dist, 1) if dist is not None else None})
    if sort == "distance":
        out.sort(key=lambda x: x["distance_km"] if x["distance_km"] is not None else 1e9)
    elif sort == "salary":
        out.sort(key=lambda x: -(x["job"].salary_max or x["job"].salary_min or 0))
    return out[:limit]


def _allowed(link: str, sites: list[str]) -> str | None:
    host = (urlparse(link).hostname or "").lower()
    for s in sites:
        if host == s or host.endswith("." + s):
            return s
    return None


async def _extract(items: list[dict[str, Any]], city: str) -> list[dict[str, Any]]:
    from app.services.ai.providers import router
    from app.services.ai.queue import PRIORITY_BACKGROUND, ai_queue

    listing = "\n".join(f"[{i}] {it.get('title', '')} | {it.get('snippet', '')[:300]}" for i, it in enumerate(items))
    system = ("Extract farm/agriculture job postings from Indian search results. Ignore non-job pages. Return ONLY a JSON list: "
              '[{"idx": int, "is_job": bool, "title": str, "employer": str|null, "city": str|null, "salary_text": str|null, '
              '"salary_min_daily": number|null, "salary_max_daily": number|null}]. Convert monthly pay to daily (÷26).')
    try:
        res = await ai_queue.run(lambda: router.chat([{"role": "user", "content": f"Searched city: {city}\n{listing}"}],
                                                     system=system, max_tokens=900, temperature=0.0),
                                 priority=PRIORITY_BACKGROUND, timeout=300)
        data = parse_json_loose(res.text)
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.info("job extraction failed: %s", exc)
        return [{"idx": i, "is_job": True, "title": it.get("title", "")[:200]} for i, it in enumerate(items)]


async def hunt_external_jobs(db: AsyncSession) -> dict[str, int]:
    """Daily hunt for open farm jobs on authentic sites (runs for everyone — logged in or not)."""
    stats = {"queries": 0, "saved": 0}
    await db.execute(delete(ExternalJob).where(ExternalJob.expires_at < now_utc()))
    if not (settings.GOOGLE_CSE_API_KEY and settings.GOOGLE_CSE_CX):
        log.info("job hunt skipped: GOOGLE_CSE_API_KEY / GOOGLE_CSE_CX not configured")
        return stats
    sites: list[str] = list(await get_setting(db, "authentic_job_sites"))
    user_cities = [c for (c,) in (await db.execute(select(User.city).where(User.city.isnot(None), User.role == "farmer")
                                                   .group_by(User.city).order_by(func.count().desc()).limit(40))).all()]
    cities = list(dict.fromkeys([*user_cities, *DEFAULT_HUNT_CITIES]))[: settings.JOB_HUNT_MAX_QUERIES_PER_DAY]
    site_q = " OR ".join(f"site:{s}" for s in sites[:10])
    for city in cities:
        q = f'(farm OR agriculture OR "farm worker" OR "farm labour" OR harvesting) jobs {city} ({site_q})'
        try:
            r = await http().get("https://www.googleapis.com/customsearch/v1", params={
                "key": settings.GOOGLE_CSE_API_KEY, "cx": settings.GOOGLE_CSE_CX, "q": q, "num": 10, "gl": "in", "dateRestrict": "m1"},
                timeout=20)
            stats["queries"] += 1
            if r.status_code != 200:
                log.warning("CSE %s: %s", r.status_code, r.text[:200])
                if r.status_code in (403, 429):
                    break
                continue
            items = [it for it in (r.json().get("items") or []) if _allowed(it.get("link", ""), sites)]
        except Exception as exc:
            log.warning("CSE failed: %s", exc)
            continue
        if not items:
            continue
        for ex in await _extract(items, city):
            try:
                it = items[int(ex.get("idx"))]
            except (TypeError, ValueError, IndexError):
                continue
            if not ex.get("is_job", True):
                continue
            geo = city_lookup(ex.get("city")) or city_lookup(city)
            site = _allowed(it["link"], sites) or "?"
            values = dict(
                id=uuid.uuid4(), title=str(ex.get("title") or it.get("title"))[:200], employer=(ex.get("employer") or None),
                city=(geo or {}).get("city") or ex.get("city"), state=(geo or {}).get("state"),
                lat=(geo or {}).get("lat"), lon=(geo or {}).get("lon"), salary_text=(ex.get("salary_text") or None),
                salary_min=_num(ex.get("salary_min_daily")), salary_max=_num(ex.get("salary_max_daily")),
                source_site=site, source_url=it["link"][:700], snippet=(it.get("snippet") or "")[:600],
                fetched_at=now_utc(), expires_at=now_utc() + timedelta(days=14))
            await db.execute(pg_insert(ExternalJob).values(**values).on_conflict_do_update(
                index_elements=["source_url"], set_={k: values[k] for k in ("title", "salary_text", "fetched_at", "expires_at")}))
            stats["saved"] += 1
    return stats


def _num(v: Any) -> Decimal | None:
    try:
        d = Decimal(str(v))
        return d if 0 < d < 100000 else None
    except Exception:
        return None


def city_options() -> list[str]:
    return sorted({c[0] for c in CITIES})
