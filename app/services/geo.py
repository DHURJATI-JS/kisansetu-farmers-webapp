"""Geo helpers: distance, India-only eligibility check, reverse geocoding and typed-address lookup (Nominatim, cached
& throttled)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from functools import lru_cache
from typing import Any

from app.config import settings
from app.services.http import http
from app.services.india_data import CITIES, CITY_ALIASES, STATES

log = logging.getLogger("kisansetu.geo")

INDIA_BBOX = (6.5, 37.6, 68.0, 97.5)  # min_lat, max_lat, min_lon, max_lon
_geo_cache: dict[tuple[float, float], dict[str, Any] | None] = {}
_geo_lock = asyncio.Lock()
_last_call = 0.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bbox_around(lat: float, lon: float, km: float) -> tuple[float, float, float, float]:
    dlat = km / 111.0
    dlon = km / (111.0 * max(0.2, math.cos(math.radians(lat))))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


def in_india_bbox(lat: float, lon: float) -> bool:
    a, b, c, d = INDIA_BBOX
    return a <= lat <= b and c <= lon <= d


def nearest_city(lat: float, lon: float) -> tuple[dict[str, Any], float]:
    best, best_d = None, 1e9
    for name, state, clat, clon in CITIES:
        d = haversine_km(lat, lon, clat, clon)
        if d < best_d:
            best, best_d = {"city": name, "state": state, "lat": clat, "lon": clon}, d
    return best or {}, best_d


@lru_cache(maxsize=2048)
def city_lookup(name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    key = name.strip().casefold()
    key = CITY_ALIASES.get(key, key).casefold()
    for city, state, lat, lon in CITIES:
        if city.casefold() == key:
            return {"city": city, "state": state, "lat": lat, "lon": lon}
    for city, state, lat, lon in CITIES:
        if key in city.casefold() or city.casefold() in key:
            return {"city": city, "state": state, "lat": lat, "lon": lon}
    return None


def normalize_state(name: str | None) -> str | None:
    if not name:
        return None
    k = name.strip().casefold()
    for s in STATES:
        if s.casefold() == k:
            return s
    if k in ("nct of delhi", "national capital territory of delhi"):
        return "Delhi"
    return name.strip()


async def reverse_geocode(lat: float, lon: float) -> dict[str, Any] | None:
    """Nominatim reverse geocoding (≤1 req/s, cached by ~1 km cell). Returns None if unreachable."""
    global _last_call
    cell = (round(lat, 2), round(lon, 2))
    if cell in _geo_cache:
        return _geo_cache[cell]
    async with _geo_lock:
        wait = 1.05 - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.monotonic()
        try:
            r = await http().get(
                f"{settings.NOMINATIM_URL}/reverse",
                params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 10, "addressdetails": 1},
                headers={"User-Agent": settings.GEOCODER_USER_AGENT, "Accept-Language": "en"},
                timeout=12,
            )
            if r.status_code != 200:
                return None
            addr = (r.json() or {}).get("address", {})
            out = {
                "country_code": (addr.get("country_code") or "").lower(),
                "state": normalize_state(addr.get("state") or addr.get("union_territory") or addr.get("territory")),
                "district": addr.get("state_district") or addr.get("county") or addr.get("district"),
                "city": addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality")
                or addr.get("suburb") or addr.get("county"),
            }
        except Exception as exc:
            log.info("reverse geocode unavailable: %s", exc)
            return None
    if len(_geo_cache) > 20000:
        _geo_cache.clear()
    _geo_cache[cell] = out
    return out


async def check_india(lat: float | None, lon: float | None) -> dict[str, Any]:
    """Eligibility: the app is restricted to India. Uses Nominatim, falls back to an offline city check."""
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return {"ok": False, "reason": "no_location"}
    if not in_india_bbox(lat, lon):
        return {"ok": False, "reason": "outside_india"}
    geo = await reverse_geocode(lat, lon)
    if geo and geo.get("country_code"):
        ok = geo["country_code"] == "in"
        return {"ok": ok, "approx": False, "reason": None if ok else "outside_india", **geo}
    city, dist = nearest_city(lat, lon)
    ok = dist <= 250
    return {"ok": ok, "approx": True, "reason": None if ok else "outside_india",
            "state": city.get("state"), "city": city.get("city"), "district": None, "country_code": "in" if ok else ""}


# ------------------------------------------------------------------ typed address (no GPS, or a wrong GPS fix)
PINCODE_RE = re.compile(r"^[1-9][0-9]{5}$")
_STATE_KEYS = {s.casefold(): s for s in STATES}
_STATE_ALIASES = {"orissa": "Odisha", "pondicherry": "Puducherry", "uttaranchal": "Uttarakhand",
                  "nct of delhi": "Delhi", "national capital territory of delhi": "Delhi"}
_FOUND_TTL, _MISS_TTL = 30 * 86400, 86400


def tidy_place(value: str | None, max_len: int = 96) -> str:
    """A typed village / district name on one line with single spaces and no stray punctuation at the ends."""
    return re.sub(r"\s+", " ", value or "").strip(" ,.;:-")[:max_len]


def state_of(name: str | None) -> str | None:
    """The canonical STATES entry for a typed or Nominatim state name ("Jammu & Kashmir", "Orissa"…), else None."""
    k = re.sub(r"\s+", " ", (name or "").replace("&", " and ")).strip().casefold()
    return _STATE_KEYS.get(k) or _STATE_ALIASES.get(k)


def _city_exact(name: str) -> dict[str, Any] | None:
    """Offline fallback: an EXACT city match only (a substring match would turn "Pur" into Jaipur)."""
    key = name.strip().casefold()
    key = CITY_ALIASES.get(key, key).casefold()
    for city, state, lat, lon in CITIES:
        if city.casefold() == key:
            return {"city": city, "state": state, "lat": lat, "lon": lon}
    return None


async def _nominatim_search(params: dict[str, Any]) -> list[dict[str, Any]] | None:
    """One Nominatim /search call inside India, sharing the ≤1 request/s budget with reverse geocoding.
    None = the service could not be reached (the caller stops asking and uses the offline city list)."""
    global _last_call
    async with _geo_lock:
        wait = 1.05 - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.monotonic()
        try:
            r = await http().get(
                f"{settings.NOMINATIM_URL}/search",
                params={"format": "jsonv2", "addressdetails": 1, "limit": 5, "countrycodes": "in", **params},
                headers={"User-Agent": settings.GEOCODER_USER_AGENT, "Accept-Language": "en"},
                timeout=12,
            )
            data = r.json() if r.status_code == 200 else None
        except Exception as exc:  # noqa: BLE001 - network, TLS or a non-JSON answer: treat as unreachable
            log.info("typed-address lookup unavailable: %s", exc)
            return None
    return data if isinstance(data, list) else None


def _hit(item: dict[str, Any], state: str) -> dict[str, Any] | None:
    """A Nominatim answer that is in India AND in the state the person chose, as {lat, lon, found}."""
    addr = item.get("address") or {}
    if (addr.get("country_code") or "").lower() != "in":
        return None
    if state_of(addr.get("state") or addr.get("union_territory") or addr.get("territory")) != state:
        return None
    try:
        lat, lon = float(item["lat"]), float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not in_india_bbox(lat, lon):
        return None
    name = (addr.get("village") or addr.get("town") or addr.get("city") or addr.get("hamlet") or addr.get("suburb")
            or addr.get("municipality") or addr.get("postcode") or item.get("name"))
    area = addr.get("state_district") or addr.get("county") or addr.get("district")
    found = ", ".join(dict.fromkeys(str(x) for x in (name, area, state) if x))
    return {"lat": round(lat, 6), "lon": round(lon, 6), "found": found[:200]}


async def _cache_get(key: str) -> Any:
    from app.services.redis_client import cache_get_json

    try:
        return await cache_get_json(key)
    except Exception:  # noqa: BLE001 - a cache problem must never block someone from setting their location
        return None


async def _cache_set(key: str, value: Any, ttl: int) -> None:
    from app.services.redis_client import cache_set_json

    try:
        await cache_set_json(key, value, ttl)
    except Exception:  # noqa: BLE001
        pass


async def locate_address(state: str, district: str, place: str, pincode: str | None = None) -> dict[str, Any] | None:
    """A typed address (no GPS) → a point in India, or None when the place cannot be found.

    Asks for the village / town, then the PIN code, then the district; every answer must lie in India AND in the chosen
    state, so a "Rampur" in another state never wins. If the map service is unreachable, the built-in city list is
    used (exact names only). The typed names are what gets saved — the map only supplies coordinates, and the PIN code
    is used for the search but never stored. Answers are cached in Redis for every worker (misses for a day)."""
    st = state_of(state)
    district, place = tidy_place(district), tidy_place(place)
    pin = (pincode or "").strip() or None
    if not st or len(district) < 2 or len(place) < 2 or (pin and not PINCODE_RE.match(pin)):
        return None
    key = "ks:geo:typed:" + hashlib.sha256(
        json.dumps([st, district.casefold(), place.casefold(), pin], ensure_ascii=False).encode()).hexdigest()[:40]
    cached = await _cache_get(key)
    if isinstance(cached, dict):
        return None if cached.get("none") else cached

    hit, precision, reachable = None, None, True
    for precision, params in (("place", {"q": f"{place}, {district}, {st}"}),
                              ("pincode", {"postalcode": pin} if pin else None),
                              ("district", {"q": f"{district}, {st}"})):
        if not params:
            continue
        items = await _nominatim_search(params)
        if items is None:
            reachable = False
            break
        hit = next((h for h in (_hit(i, st) for i in items if isinstance(i, dict)) if h), None)
        if hit:
            break
    if not hit:
        for precision, name in (("place", place), ("district", district)):
            c = _city_exact(name)
            if c and c["state"] == st:
                hit = {"lat": c["lat"], "lon": c["lon"], "found": f"{c['city']}, {st}"}
                break
    if not hit:
        if reachable:
            await _cache_set(key, {"none": True}, _MISS_TTL)
        return None
    out = {"lat": hit["lat"], "lon": hit["lon"], "state": st, "district": district, "city": place,
           "precision": precision, "approx": precision != "place", "found": hit["found"]}
    if reachable:          # an offline answer is not cached: the map service may know the exact village next time
        await _cache_set(key, out, _FOUND_TTL)
    return out
