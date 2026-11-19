"""Daily AI farm scan: abnormal weather, calamities within 200 km, crop suitability, insurance enforcement.

External sources (all free, no key): Open-Meteo forecasts (batched multi-location), USGS earthquakes (one India-wide
call), GDACS (floods/cyclones/droughts/volcanoes/wildfires) and NASA EONET (storms/wildfires/floods).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Branch, BranchCrop, CalamityEvent, Farm, FarmMember, InsurancePolicy, User, WeatherReport
from app.services.crops import find_crop, crop_weather_warnings, farming_type_advice, recommend
from app.services.geo import haversine_km
from app.services.halts import ensure_halt, lift_halts
from app.services.http import http
from app.services.notify import notify, notify_many
from app.services.redis_client import cache_get_json, cache_set_json
from app.services.settings_store import get_all
from app.utils import now_utc, parse_json_loose, today_ist

log = logging.getLogger("kisansetu.weather")


def _web_link(url: object) -> str | None:
    """Links from outside feeds (USGS / GDACS / EONET) become a "Source" link on farm pages: plain http(s) only —
    never javascript:, data: or anything else a tampered feed could carry."""
    if isinstance(url, str) and url.strip().lower().startswith(("https://", "http://")) and len(url) <= 500:
        return url.strip()
    return None


INDIA_QUERY_BOX = {"minlatitude": 4.5, "maxlatitude": 39.5, "minlongitude": 66.0, "maxlongitude": 99.5}


# ------------------------------------------------------------------ fetchers
async def fetch_forecasts(coords: list[tuple[float, float]]) -> dict[tuple[float, float], dict[str, Any]]:
    out: dict[tuple[float, float], dict[str, Any]] = {}
    pending = []
    for c in dict.fromkeys(coords):
        cached = await cache_get_json(f"ks:wx:{c[0]:.2f}:{c[1]:.2f}")
        if cached:
            out[c] = cached
        else:
            pending.append(c)
    for i in range(0, len(pending), 40):
        chunk = pending[i : i + 40]
        try:
            r = await http().get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": ",".join(f"{c[0]:.4f}" for c in chunk), "longitude": ",".join(f"{c[1]:.4f}" for c in chunk),
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,weather_code",
                "timezone": "Asia/Kolkata", "forecast_days": 7}, timeout=25)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            log.warning("open-meteo failed: %s", exc)
            continue
        items = data if isinstance(data, list) else [data]
        for c, item in zip(chunk, items):
            d = item.get("daily") or {}
            days = [{"date": d["time"][k], "tmax": d["temperature_2m_max"][k], "tmin": d["temperature_2m_min"][k],
                     "rain": d["precipitation_sum"][k], "wind": d["wind_speed_10m_max"][k], "code": d["weather_code"][k]}
                    for k in range(len(d.get("time", [])))]
            fc = {"daily": days, "fetched": now_utc().isoformat()}
            out[c] = fc
            await cache_set_json(f"ks:wx:{c[0]:.2f}:{c[1]:.2f}", fc, 3 * 3600)
    return out


async def fetch_earthquakes() -> list[dict[str, Any]]:
    cached = await cache_get_json("ks:usgs:india")
    if cached is not None:
        return cached
    start = (now_utc() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        r = await http().get("https://earthquake.usgs.gov/fdsnws/event/1/query", params={
            "format": "geojson", "starttime": start, "minmagnitude": 4.0, **INDIA_QUERY_BOX}, timeout=25)
        r.raise_for_status()
        feats = r.json().get("features", [])
    except Exception as exc:
        log.warning("USGS failed: %s", exc)
        return []
    out = []
    for f in feats:
        lon, lat = f["geometry"]["coordinates"][:2]
        p = f.get("properties", {})
        mag = p.get("mag") or 0
        out.append({"source": "usgs", "id": f.get("id"), "kind": "earthquake", "title": p.get("title") or f"M{mag} earthquake",
                    "severity": "severe" if mag >= 6 else "high" if mag >= 5 else "moderate", "lat": lat, "lon": lon,
                    "time": p.get("time"), "url": _web_link(p.get("url")), "magnitude": mag})
    await cache_set_json("ks:usgs:india", out, 1800)
    return out


async def fetch_gdacs() -> list[dict[str, Any]]:
    cached = await cache_get_json("ks:gdacs")
    if cached is not None:
        return cached
    out = []
    try:
        r = await http().get("https://www.gdacs.org/gdacsapi/api/events/geteventlist/EVENTS4APP", timeout=25)
        r.raise_for_status()
        for f in (r.json() or {}).get("features", []):
            geom = f.get("geometry") or {}
            if geom.get("type") != "Point":
                continue
            lon, lat = geom["coordinates"][:2]
            p = f.get("properties", {})
            kind = {"EQ": "earthquake", "TC": "cyclone", "FL": "flood", "VO": "volcano", "DR": "drought", "WF": "wildfire"}.get(p.get("eventtype"), "hazard")
            level = (p.get("alertlevel") or "Green").lower()
            out.append({"source": "gdacs", "id": f"{p.get('eventtype')}{p.get('eventid')}", "kind": kind,
                        "title": p.get("name") or p.get("description") or kind,
                        "severity": {"red": "severe", "orange": "high"}.get(level, "moderate"), "lat": lat, "lon": lon,
                        "time": p.get("fromdate"), "url": _web_link((p.get("url") or {}).get("report") if isinstance(p.get("url"), dict) else None)})
    except Exception as exc:
        log.info("GDACS unavailable: %s", exc)
    await cache_set_json("ks:gdacs", out, 3600)
    return out


async def fetch_eonet() -> list[dict[str, Any]]:
    cached = await cache_get_json("ks:eonet")
    if cached is not None:
        return cached
    out = []
    try:
        r = await http().get("https://eonet.gsfc.nasa.gov/api/v3/events", params={
            "status": "open", "days": 10, "bbox": "66,39.5,99.5,4.5"}, timeout=25)
        r.raise_for_status()
        for e in (r.json() or {}).get("events", []):
            geos = [g for g in e.get("geometry", []) if g.get("type") == "Point"]
            if not geos:
                continue
            lon, lat = geos[-1]["coordinates"][:2]
            cat = ((e.get("categories") or [{}])[0].get("id") or "hazard").lower()
            kind = {"severestorms": "storm", "wildfires": "wildfire", "floods": "flood", "volcanoes": "volcano",
                    "landslides": "landslide", "drought": "drought"}.get(cat, cat)
            out.append({"source": "eonet", "id": e.get("id"), "kind": kind, "title": e.get("title") or kind,
                        "severity": "high", "lat": lat, "lon": lon, "time": geos[-1].get("date"),
                        "url": _web_link((e.get("sources") or [{}])[0].get("url"))})
    except Exception as exc:
        log.info("EONET unavailable: %s", exc)
    await cache_set_json("ks:eonet", out, 3600)
    return out


# ------------------------------------------------------------------ analysis
def abnormal_alerts(daily: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for d in daily:
        tmax, tmin, rain, wind, code = d.get("tmax"), d.get("tmin"), d.get("rain") or 0, d.get("wind") or 0, d.get("code") or 0
        if tmax is not None and tmax >= 42:
            out.append({"date": d["date"], "kind": "heatwave", "severity": "severe" if tmax >= 45 else "warning", "value": tmax})
        if tmin is not None and tmin <= 4:
            out.append({"date": d["date"], "kind": "coldwave", "severity": "severe" if tmin <= 2 else "warning", "value": tmin})
        if rain >= 64.5:
            sev = "extreme" if rain >= 204.5 else "severe" if rain >= 115.6 else "warning"
            out.append({"date": d["date"], "kind": "heavy_rain", "severity": sev, "value": rain})
        if wind >= 50:
            sev = "extreme" if wind >= 89 else "severe" if wind >= 62 else "warning"
            out.append({"date": d["date"], "kind": "strong_wind", "severity": sev, "value": wind})
        if code in (95, 96, 99):
            out.append({"date": d["date"], "kind": "thunderstorm_hail" if code in (96, 99) else "thunderstorm",
                        "severity": "severe" if code in (96, 99) else "warning", "value": code})
    return out


def is_dangerous(alerts: list[dict], calamities: list[dict]) -> bool:
    return bool(calamities) or any(a["severity"] in ("severe", "extreme") for a in alerts)


async def insurance_state(db: AsyncSession, farm_ids: list[uuid.UUID], on: date | None = None) -> dict[uuid.UUID, dict[str, Any]]:
    on = on or today_ist()
    out = {fid: {"salary": False, "health": False, "crop_or_field": False, "comprehensive": False, "expiring": [], "expired": []}
           for fid in farm_ids}
    if not farm_ids:
        return out
    rows = (await db.execute(select(InsurancePolicy).where(
        InsurancePolicy.farm_id.in_(farm_ids), InsurancePolicy.status.in_(("verified", "expired", "pending"))))).scalars()
    cfg_days = 15
    for p in rows:
        st = out[p.farm_id]
        valid = p.status in ("verified", "pending") and p.valid_from <= on <= p.valid_to
        if valid:
            if p.policy_type == "comprehensive":
                st["comprehensive"] = True
            if p.policy_type == "salary" or p.covers_salaries:
                st["salary"] = True
            if p.policy_type in ("health", "comprehensive"):
                st["health"] = True
            if p.policy_type in ("crop", "field"):
                st["crop_or_field"] = True
            if (p.valid_to - on).days <= cfg_days:
                st["expiring"].append({"type": p.policy_type, "valid_to": p.valid_to.isoformat()})
        elif p.valid_to < on:
            st["expired"].append({"type": p.policy_type, "valid_to": p.valid_to.isoformat()})
    return out


async def _ai_advisory(owner_lang: str, farm: Farm, branch: Branch, alerts, crop_warns, calamities, recs) -> str | None:
    if not (alerts or crop_warns or calamities):
        return None
    from app.i18n import lang_meta
    from app.services.ai.providers import router
    from app.services.ai.queue import PRIORITY_BACKGROUND, ai_queue

    lang = lang_meta(owner_lang)
    prompt = (f"Farm '{farm.name}', branch '{branch.name}' ({branch.city or ''}, {branch.state or ''}). "
              f"Weather alerts: {alerts[:6]}. Crop risks: {crop_warns[:6]}. Disasters within 200 km: "
              f"{[{'kind': c['kind'], 'title': c['title'], 'km': c['distance_km']} for c in calamities[:4]]}. "
              f"Suitable crops now: {[r['name'] for r in recs[:4]]}.")
    system = (f"You are an agronomy and farm-safety advisor in India. Write a short, practical advisory (max 90 words) in "
              f"{lang['name']} for the farm owner: protective actions for crops and worker safety. Plain text only.")
    try:
        res = await ai_queue.run(lambda: router.chat([{"role": "user", "content": prompt}], system=system, max_tokens=260),
                                 priority=PRIORITY_BACKGROUND, timeout=300)
        return res.text[:1500]
    except Exception:
        return None


async def scan_farms(db: AsyncSession, farm_ids: list[uuid.UUID] | None = None, *, use_ai: bool = True) -> dict[str, int]:
    """Scan all active branches (or the given farms). Batched: ~6 queries + 1–N HTTP calls in total."""
    today = today_ist()
    cfg = await get_all(db)
    radius = float(cfg["calamity_radius_km"])
    stmt = select(Farm).where(Farm.status.in_(("active", "closing")))
    if farm_ids:
        stmt = stmt.where(Farm.id.in_(farm_ids))
    farms = {f.id: f for f in (await db.execute(stmt)).scalars()}
    if not farms:
        return {"branches": 0}
    branches = list((await db.execute(select(Branch).where(Branch.farm_id.in_(list(farms)), Branch.status != "closed"))).scalars())
    crops_by_branch: dict[uuid.UUID, list[BranchCrop]] = defaultdict(list)
    for c in (await db.execute(select(BranchCrop).where(BranchCrop.branch_id.in_([b.id for b in branches]),
                                                        BranchCrop.status == "growing"))).scalars():
        crops_by_branch[c.branch_id].append(c)
    owners = {u.id: u for u in (await db.execute(select(User).where(User.id.in_({f.owner_id for f in farms.values()})))).scalars()}
    workers_by_branch: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for bid, uid in (await db.execute(select(FarmMember.branch_id, FarmMember.user_id).where(
            FarmMember.farm_id.in_(list(farms)), FarmMember.status != "ended"))).all():
        if bid:
            workers_by_branch[bid].append(uid)
    ins = await insurance_state(db, list(farms), today)

    forecasts, quakes, gdacs, eonet = await asyncio.gather(
        fetch_forecasts([(round(b.lat, 2), round(b.lon, 2)) for b in branches]),
        fetch_earthquakes(), fetch_gdacs(), fetch_eonet())
    events = quakes + gdacs + eonet
    stored_events = set()
    stats = {"branches": 0, "alerts": 0, "calamities": 0, "halts": 0}
    ai_budget = 60 if use_ai else 0

    for b in branches:
        farm = farms[b.farm_id]
        owner = owners.get(farm.owner_id)
        fc = forecasts.get((round(b.lat, 2), round(b.lon, 2))) or {"daily": []}
        daily = fc.get("daily", [])
        alerts = abnormal_alerts(daily)
        near = []
        for e in events:
            try:
                dist = haversine_km(b.lat, b.lon, float(e["lat"]), float(e["lon"]))
            except Exception:
                continue
            if dist <= radius:
                near.append({**e, "distance_km": round(dist, 1)})
                stored_events.add((e["source"], str(e["id"])))
        crop_warns = []
        for c in crops_by_branch.get(b.id, []):
            kb = find_crop(c.crop_key or c.name)
            if kb:
                for w in crop_weather_warnings(kb, daily):
                    crop_warns.append({"crop": c.name, **w})
        temps = [((d.get("tmax") or 0) + (d.get("tmin") or 0)) / 2 for d in daily if d.get("tmax") is not None]
        recs = recommend(b.state, sum(temps) / len(temps) if temps else None, today)
        st = ins[farm.id]
        danger = is_dangerous(alerts, near)
        insurance_status = "ok"
        if danger and not (st["salary"] or st["comprehensive"]):
            insurance_status = "missing_salary_cover"
            _, created = await ensure_halt(db, farm.id, b.id, "calamity_no_insurance",
                                           params={"branch": b.name}, details={"alerts": alerts[:5], "calamities": near[:5]})
            if created:
                stats["halts"] += 1
            await notify(db, farm.owner_id, "insurance_required", title_key="notif.insurance_required.title",
                         body_key="notif.insurance_required.body", params={"branch": b.name}, severity="danger",
                         link=f"/farms/{farm.id}?tab=insurance", channels=("sms", "email", "whatsapp"),
                         dedupe_key=f"insreq:{b.id}:{today}")
        elif danger and not (st["crop_or_field"] or st["comprehensive"]):
            insurance_status = "suggest_field_cover"
            await notify(db, farm.owner_id, "insurance_suggest", title_key="notif.insurance_suggest.title",
                         body_key="notif.insurance_suggest.body", params={"branch": b.name}, severity="warning",
                         link=f"/farms/{farm.id}?tab=insurance", dedupe_key=f"inssug:{b.id}:{today}")
        if near:
            stats["calamities"] += len(near)
            top = sorted(near, key=lambda x: x["distance_km"])[0]
            params = {"kind": top["kind"], "title": top["title"][:120], "km": top["distance_km"], "branch": b.name}
            await notify(db, farm.owner_id, "calamity", title_key="notif.calamity.title", body_key="notif.calamity.body",
                         params=params, severity="danger", link=f"/farms/{farm.id}?tab=weather",
                         channels=("sms", "whatsapp"), dedupe_key=f"calam:{b.id}:{top['source']}:{top['id']}")
            await notify_many(db, workers_by_branch.get(b.id, []), "calamity_worker", title_key="notif.calamity_worker.title",
                              body_key="notif.calamity_worker.body", params=params, severity="danger",
                              dedupe_key=f"calamw:{b.id}:{top['source']}:{top['id']}")
        if alerts:
            stats["alerts"] += len(alerts)
            worst = max(alerts, key=lambda a: ("warning", "severe", "extreme").index(a["severity"]))
            await notify(db, farm.owner_id, "weather_alert", title_key="notif.weather_alert.title",
                         body_key=f"weather.kind.{worst['kind']}", params={"value": worst["value"], "date": worst["date"], "branch": b.name},
                         severity="warning", link=f"/farms/{farm.id}?tab=weather", dedupe_key=f"wx:{b.id}:{today}")
        if crop_warns:
            crops_txt = ", ".join(sorted({w["crop"] for w in crop_warns}))
            await notify(db, farm.owner_id, "crop_weather", title_key="notif.crop_weather.title", body_key="notif.crop_weather.body",
                         params={"crops": crops_txt, "branch": b.name}, severity="warning",
                         link=f"/farms/{farm.id}?tab=weather", dedupe_key=f"cropwx:{b.id}:{today}")
        advisory = None
        if ai_budget > 0 and (alerts or crop_warns or near):
            ai_budget -= 1
            advisory = await _ai_advisory(owner.language if owner else "en", farm, b, alerts, crop_warns, near, recs)
        values = {"id": uuid.uuid4(), "branch_id": b.id, "farm_id": farm.id, "report_date": today,
                  "forecast": fc, "alerts": alerts, "crop_warnings": crop_warns,
                  "calamities": [{k: v for k, v in c.items() if k != "raw"} for c in near],
                  "insurance_status": insurance_status, "recommendations": recs[:6], "advisory": advisory}
        upd = {k: v for k, v in values.items() if k not in ("id", "branch_id", "report_date")}
        await db.execute(pg_insert(WeatherReport).values(**values).on_conflict_do_update(
            index_elements=["branch_id", "report_date"], set_=upd))
        stats["branches"] += 1

    # insurance expiry: mandatory covers (salary + health) must stay valid
    for fid, st in ins.items():
        farm = farms[fid]
        if not (st["salary"] and st["health"]) and farm.status == "active":
            await ensure_halt(db, fid, None, "insurance_expired", params={"farm": farm.name})
            await notify(db, farm.owner_id, "insurance_expired", title_key="notif.insurance_expired.title",
                         body_key="notif.insurance_expired.body", params={"farm": farm.name}, severity="danger",
                         link=f"/farms/{fid}?tab=insurance", channels=("email", "sms"), dedupe_key=f"insexp:{fid}:{today}")
        elif st["salary"] and st["health"]:
            await lift_halts(db, fid, "insurance_expired", note="valid insurance on file")
        for e in st["expiring"]:
            await notify(db, farm.owner_id, "insurance_expiring", title_key="notif.insurance_expiring.title",
                         body_key="notif.insurance_expiring.body", params={"type": e["type"], "date": e["valid_to"], "farm": farm.name},
                         severity="warning", link=f"/farms/{fid}?tab=insurance", dedupe_key=f"insexpiring:{fid}:{e['type']}:{e['valid_to']}")

    # persist calamity events seen (history / admin view)
    for e in events:
        if (e["source"], str(e["id"])) in stored_events:
            await db.execute(pg_insert(CalamityEvent).values(
                id=uuid.uuid4(), source=e["source"], external_id=str(e["id"])[:160], kind=e["kind"][:24],
                title=str(e["title"])[:300], severity=e["severity"], lat=float(e["lat"]), lon=float(e["lon"]),
                url=(e.get("url") or None)).on_conflict_do_nothing(index_elements=["source", "external_id"]))
    return stats


async def location_advice(lat: float, lon: float, state: str | None, lang: str = "en") -> dict[str, Any]:
    """Crop + farming-type recommendation for a location (used in farm creation and by the AI assistant)."""
    fc = (await fetch_forecasts([(round(lat, 2), round(lon, 2))])).get((round(lat, 2), round(lon, 2))) or {"daily": []}
    temps = [((d["tmax"] or 0) + (d["tmin"] or 0)) / 2 for d in fc["daily"] if d.get("tmax") is not None]
    avg = round(sum(temps) / len(temps), 1) if temps else None
    recs = recommend(state, avg, today_ist())
    return {"avg_temp": avg, "recommendations": recs, "farming_type": farming_type_advice(recs),
            "alerts": abnormal_alerts(fc["daily"]), "forecast": fc["daily"]}


def parse_ai_list(text: str) -> list[Any]:
    data = parse_json_loose(text)
    return data if isinstance(data, list) else []
