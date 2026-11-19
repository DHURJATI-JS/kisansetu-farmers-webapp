"""Government schemes — stored in the database so the superadmin can add, update, withdraw and restore them
(real schemes launch, change rules, move portals and get merged). Cached per process with a Redis version key.

* Eligibility rules are simplified summaries of public guidelines; the official portal makes the final decision.
* Links only ever point at allow-listed official domains (https, no IP literals / credentials / odd ports) —
  checked when saved, again before every redirect page, and daily by a link-health job.
* People who opened a scheme's portal through the app are told when it changes or is withdrawn.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.i18n import t
from app.models import GovScheme, SchemeVisit
from app.services.redis_client import kv
from app.utils import now_utc, today_ist

log = logging.getLogger("kisansetu.schemes")

DEFAULT_DOMAINS = ["gov.in", "nic.in", "maandhan.in"]
_VER_KEY = "ks:schemes:ver"
_LOCAL_TTL = 300.0
SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")


class Rules(BaseModel):
    """Eligibility rules a scheme may use. Unknown keys are rejected (no free-form logic)."""

    model_config = ConfigDict(extra="forbid")
    min_age: int | None = Field(default=None, ge=0, le=120)
    max_age: int | None = Field(default=None, ge=0, le=120)
    land: bool = False                 # must own / cultivate land
    max_land: Decimal | None = Field(default=None, ge=0, le=100000)          # hectares
    max_month_income: Decimal | None = Field(default=None, ge=0, le=100000000)
    not_itp: bool = False              # not an income-tax payer
    not_govt: bool = False             # not a government employee
    not_epf: bool = False              # not in EPF / ESIC / NPS
    bank: bool = False                 # needs a bank account
    rural: bool = False                # rural households only
    portal_check: bool = False         # eligibility is decided on the portal (e.g. SECC / Aadhaar lookup)

    @model_validator(mode="after")
    def _ages(self) -> "Rules":
        if self.min_age is not None and self.max_age is not None and self.min_age > self.max_age:
            raise ValueError("min_age is above max_age")
        return self

    def compact(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in self.model_dump().items():
            if v is None or v is False:
                continue
            out[k] = float(v) if isinstance(v, Decimal) else v
        return out


# Seed catalog (first run only — afterwards the database is the source of truth and admin edits are never overwritten).
BUILTIN: list[dict[str, Any]] = [
    {"id": "pm_kisan", "url": "https://pmkisan.gov.in/", "for": ("farmer", "owner"),
     "rules": {"land": True, "not_itp": True, "not_govt": True}},
    {"id": "pmfby", "url": "https://pmfby.gov.in/", "for": ("farmer", "owner"), "rules": {"land": True}},
    {"id": "kcc", "url": "https://www.myscheme.gov.in/schemes/kcc", "for": ("farmer", "owner"),
     "rules": {"min_age": 18, "max_age": 75, "land": True}},
    {"id": "pm_kmy", "url": "https://maandhan.in/", "for": ("farmer", "owner"),
     "rules": {"min_age": 18, "max_age": 40, "land": True, "max_land": 2, "not_itp": True, "not_epf": True}},
    {"id": "pm_sym", "url": "https://maandhan.in/", "for": ("farmer",),
     "rules": {"min_age": 18, "max_age": 40, "max_month_income": 15000, "not_itp": True, "not_epf": True}},
    {"id": "eshram", "url": "https://eshram.gov.in/", "for": ("farmer",),
     "rules": {"min_age": 16, "max_age": 59, "not_itp": True, "not_epf": True}},
    {"id": "mgnrega", "url": "https://nrega.nic.in/", "for": ("farmer",), "rules": {"min_age": 18, "rural": True}},
    {"id": "pmjjby", "url": "https://jansuraksha.gov.in/", "for": ("farmer", "owner"),
     "rules": {"min_age": 18, "max_age": 50, "bank": True}},
    {"id": "pmsby", "url": "https://jansuraksha.gov.in/", "for": ("farmer", "owner"),
     "rules": {"min_age": 18, "max_age": 70, "bank": True}},
    {"id": "apy", "url": "https://jansuraksha.gov.in/", "for": ("farmer", "owner"),
     "rules": {"min_age": 18, "max_age": 40, "bank": True, "not_itp": True}},
    {"id": "pmjay", "url": "https://beneficiary.nha.gov.in/", "for": ("farmer", "owner"), "rules": {"portal_check": True}},
    {"id": "soil_health", "url": "https://soilhealth.dac.gov.in/", "for": ("owner", "farmer"), "rules": {"land": True}},
    {"id": "pm_kusum", "url": "https://pmkusum.mnre.gov.in/", "for": ("owner", "farmer"), "rules": {"land": True}},
    {"id": "pmksy", "url": "https://pmksy.gov.in/", "for": ("owner", "farmer"), "rules": {"land": True}},
    {"id": "enam", "url": "https://www.enam.gov.in/", "for": ("owner", "farmer"), "rules": {}},
    {"id": "aif", "url": "https://agriinfra.dac.gov.in/", "for": ("owner",), "rules": {"min_age": 18}},
    {"id": "pkvy", "url": "https://pgsindia-ncof.gov.in/", "for": ("owner", "farmer"), "rules": {"land": True}},
    {"id": "smam", "url": "https://agrimachinery.nic.in/", "for": ("owner", "farmer"), "rules": {"land": True}},
    {"id": "pmfme", "url": "https://pmfme.mofpi.gov.in/", "for": ("owner",), "rules": {"min_age": 18}},
    {"id": "nbhm", "url": "https://nbb.gov.in/", "for": ("owner", "farmer"), "rules": {}},
    {"id": "pmmsy", "url": "https://pmmsy.dof.gov.in/", "for": ("owner", "farmer"), "rules": {}},
]


# ------------------------------------------------------------------ official links
def official_url_problem(url: str, domains: list[str]) -> str | None:
    """None when `url` is an https link on an allow-listed official domain, else an i18n error key."""
    raw = (url or "").strip()
    if not raw or len(raw) > 500 or any(c.isspace() or c in "<>\"'`\\" for c in raw):
        return "errors.scheme_url_invalid"
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return "errors.scheme_url_invalid"
    if parts.scheme != "https":
        return "errors.scheme_url_https"
    if parts.username or parts.password or port not in (None, 443):
        return "errors.scheme_url_invalid"
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or "." not in host or not host.isascii():
        return "errors.scheme_url_invalid"
    try:
        ipaddress.ip_address(host)
        return "errors.scheme_url_invalid"
    except ValueError:
        pass
    if not any(host == d or host.endswith("." + d) for d in domains):
        return "errors.scheme_url_not_official"
    return None


async def scheme_domains(db: AsyncSession) -> list[str]:
    from app.services.settings_store import get_setting

    try:
        doms = await get_setting(db, "scheme_domains")
    except Exception:
        doms = None
    return [str(d).lower() for d in (doms or DEFAULT_DOMAINS)]


# ------------------------------------------------------------------ cache
_local: dict[str, Any] = {"ver": None, "at": 0.0, "items": None}


def _row(s: GovScheme) -> dict[str, Any]:
    return {"id": s.slug, "uuid": str(s.id), "name": s.name, "desc": s.description, "url": s.url,
            "audience": list(s.audience or []), "rules": dict(s.rules or {}), "level": s.level, "states": list(s.states or []),
            "status": s.status, "i18n": bool(s.i18n_managed), "builtin": bool(s.builtin), "version": s.version,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            "link_status": s.link_status, "link_checked_at": s.link_checked_at.isoformat() if s.link_checked_at else None,
            "replaced_by": s.replaced_by, "archived_reason": s.archived_reason,
            "archived_at": s.archived_at.isoformat() if s.archived_at else None,
            "official": (s.official_check or {}).get("status"), "official_since": ((s.official_check or {}).get("changed_at") or "")[:10]}


async def all_schemes(db: AsyncSession) -> list[dict[str, Any]]:
    """Every scheme (active + archived) as plain dicts, refreshed whenever any worker bumps the version."""
    try:
        ver = await kv().get(_VER_KEY)
    except Exception:
        ver = None
    ver = ver.decode() if isinstance(ver, bytes) else ver
    items = _local["items"]
    if items is not None and _local["ver"] == ver and time.monotonic() - _local["at"] < _LOCAL_TTL:
        return items
    rows = (await db.execute(select(GovScheme).order_by(GovScheme.builtin.desc(), GovScheme.created_at, GovScheme.slug))).scalars().all()
    items = [_row(s) for s in rows]
    _local.update(ver=ver, at=time.monotonic(), items=items)
    return items


async def active_schemes(db: AsyncSession) -> list[dict[str, Any]]:
    return [s for s in await all_schemes(db) if s["status"] == "active"]


async def get_scheme(db: AsyncSession, slug: str) -> dict[str, Any] | None:
    return next((s for s in await all_schemes(db) if s["id"] == slug), None)


async def bump_version() -> None:
    """Invalidate every worker's cache. Call only AFTER the change is committed (see `after_commit`)."""
    _local.update(items=None)
    try:
        await kv().incr(_VER_KEY)
    except Exception as exc:
        log.debug("scheme cache bump failed: %s", exc)


def text_of(s: dict[str, Any], lang: str) -> tuple[str, str, bool]:
    """(name, description, needs_dynamic_translation) for display in `lang`."""
    if s["i18n"]:
        return t(f"scheme.{s['id']}.name", lang), t(f"scheme.{s['id']}.desc", lang), False
    return s["name"], s["desc"], lang != "en"


def badges(s: dict[str, Any]) -> list[str]:
    """'new' for schemes added in the last 30 days, 'updated' for schemes changed in the last 30 days."""
    from datetime import datetime

    out = []
    cutoff = now_utc() - timedelta(days=30)
    created = datetime.fromisoformat(s["created_at"]) if s.get("created_at") else None
    updated = datetime.fromisoformat(s["updated_at"]) if s.get("updated_at") else None
    if not s["builtin"] and created and created >= cutoff:
        out.append("new")
    elif s["version"] > 1 and updated and updated >= cutoff:
        out.append("updated")
    return out


def rules_text(rules: dict[str, Any], t: Any) -> str:
    """Human summary of eligibility rules (also shown to users on the schemes page)."""
    parts = []
    lo, hi = rules.get("min_age"), rules.get("max_age")
    if lo is not None and hi is not None:
        parts.append(t("scheme.rule.age_range", min=lo, max=hi))
    elif lo is not None:
        parts.append(t("scheme.rule.min_age", n=lo))
    elif hi is not None:
        parts.append(t("scheme.rule.max_age", n=hi))
    if rules.get("land"):
        parts.append(t("scheme.rule.land"))
    if rules.get("max_land") is not None:
        parts.append(t("scheme.rule.max_land", n=f"{float(rules['max_land']):g}"))
    if rules.get("max_month_income") is not None:
        parts.append(t("scheme.rule.max_month_income", n=f"{float(rules['max_month_income']):,.0f}"))
    for k in ("not_itp", "not_govt", "not_epf", "bank", "rural", "portal_check"):
        if rules.get(k):
            parts.append(t(f"scheme.rule.{k}"))
    return " · ".join(parts) or t("scheme.rule.none")


def scheme_card(s: dict[str, Any], lang: str) -> dict[str, Any]:
    """Display data for one scheme in `lang` (catalog translation for unedited built-ins, else dynamic translation)."""
    name, desc, translate = text_of(s, lang)
    return {"id": s["id"], "name": name, "desc": desc, "translate": translate, "badges": badges(s), "status": s["status"],
            "rules": rules_text(s["rules"], lambda k, **kw: t(k, lang, **kw)), "audience": s["audience"], "states": s["states"],
            "level": s["level"], "link_status": s["link_status"], "link_checked_at": s["link_checked_at"],
            "updated_at": s["updated_at"], "version": s["version"], "archived_at": s["archived_at"],
            "archived_reason": s["archived_reason"], "replaced_by": s["replaced_by"],
            "official": s.get("official"), "official_since": s.get("official_since")}


# ------------------------------------------------------------------ seeding
async def seed_schemes(db: AsyncSession, review_days: int = 180) -> int:
    """Insert built-in schemes that were never stored. Never touches existing rows (admin edits / withdrawals win)."""
    rows = [dict(slug=s["id"], name=t(f"scheme.{s['id']}.name", "en"), description=t(f"scheme.{s['id']}.desc", "en"),
                 url=s["url"], audience=list(s["for"]), rules=Rules.model_validate(s["rules"]).compact(), level="central",
                 states=[], status="active", builtin=True, i18n_managed=True, version=1, link_status="unknown",
                 link_failures=0, review_due_on=today_ist() + timedelta(days=review_days), last_reviewed_at=now_utc())
            for s in BUILTIN]
    res = await db.execute(pg_insert(GovScheme).values(rows).on_conflict_do_nothing(index_elements=["slug"])
                           .returning(GovScheme.id))
    n = len(res.scalars().all())
    if n:
        await bump_version()
    return n


# ------------------------------------------------------------------ eligibility
def evaluate(profile: dict[str, Any], role: str | None, schemes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each active scheme → status eligible | not_eligible | need_info | check_portal, with reason keys."""
    age = profile.get("age")
    land = profile.get("landholding_hectares")
    monthly = profile.get("monthly_income")
    state = profile.get("state")
    out = []
    for s in schemes:
        if s["status"] != "active":
            continue
        r = s["rules"]
        fails: list[str] = []
        missing: list[str] = []

        def need(field: str, value: Any) -> bool:
            if value is None:
                missing.append(field)
                return False
            return True

        if role and role not in s["audience"] and role != "buyer":
            fails.append("scheme.reason.audience")
        if s["states"] and need("state", state) and state not in s["states"]:
            fails.append("scheme.reason.other_state")
        if r.get("min_age") is not None and need("age", age) and age < r["min_age"]:
            fails.append("scheme.reason.too_young")
        if r.get("max_age") is not None and need("age", age) and age > r["max_age"]:
            fails.append("scheme.reason.too_old")
        if r.get("land") and need("landholding_hectares", land) and Decimal(str(land)) <= 0:
            fails.append("scheme.reason.no_land")
        if r.get("max_land") is not None and need("landholding_hectares", land) and Decimal(str(land)) > Decimal(str(r["max_land"])):
            fails.append("scheme.reason.land_too_large")
        if r.get("max_month_income") is not None and need("monthly_income", monthly) \
                and Decimal(str(monthly)) > Decimal(str(r["max_month_income"])):
            fails.append("scheme.reason.income_too_high")
        if r.get("not_itp") and need("is_income_tax_payer", profile.get("is_income_tax_payer")) and profile["is_income_tax_payer"]:
            fails.append("scheme.reason.tax_payer")
        if r.get("not_govt") and need("is_govt_employee", profile.get("is_govt_employee")) and profile["is_govt_employee"]:
            fails.append("scheme.reason.govt_employee")
        if r.get("not_epf") and need("has_epf_esic_nps", profile.get("has_epf_esic_nps")) and profile["has_epf_esic_nps"]:
            fails.append("scheme.reason.epf_member")
        if r.get("bank") and need("has_bank_account", profile.get("has_bank_account")) and not profile["has_bank_account"]:
            fails.append("scheme.reason.no_bank")
        if r.get("rural") and need("is_rural", profile.get("is_rural")) and not profile["is_rural"]:
            fails.append("scheme.reason.not_rural")
        if fails:
            status = "not_eligible"
        elif r.get("portal_check"):
            status = "eligible" if (age or 0) >= 70 else "check_portal"
        elif missing:
            status = "need_info"
        else:
            status = "eligible"
        out.append({"id": s["id"], "status": status, "reasons": fails, "missing": sorted(set(missing))})
    order = {"eligible": 0, "check_portal": 1, "need_info": 2, "not_eligible": 3}
    return sorted(out, key=lambda x: order[x["status"]])


# ------------------------------------------------------------------ visitors
async def record_visit(db: AsyncSession, user_id: Any, scheme_uuid: str) -> None:
    await db.execute(pg_insert(SchemeVisit).values(user_id=user_id, scheme_id=scheme_uuid, visits=1, last_visit_at=now_utc())
                     .on_conflict_do_update(constraint="uq_scheme_visit",
                                            set_={"visits": SchemeVisit.visits + 1, "last_visit_at": now_utc()}))


async def visitor_ids(db: AsyncSession, scheme_id: Any) -> list[Any]:
    return list((await db.execute(select(SchemeVisit.user_id).where(SchemeVisit.scheme_id == scheme_id))).scalars())


def name_param(s: GovScheme) -> dict[str, str]:
    """Notification params: translated per reader for catalog-managed schemes, else the stored name."""
    return {"scheme__key": f"scheme.{s.slug}.name"} if s.i18n_managed else {"scheme": s.name}


# ------------------------------------------------------------------ link health
_HINTS = ("scheme has been discontinued", "scheme is discontinued", "has been discontinued", "scheme has been closed",
          "scheme is closed", "no longer accepting", "has been merged", "merged with", "scheme has ended", "website has moved",
          "this site has moved", "domain has expired", "domain is for sale")


async def _public_host(host: str) -> bool:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


async def _get(client: httpx.AsyncClient, url: str) -> tuple:
    """One hop: ("redirect", location) or ("page", status, body ≤ 300 KB, content-type, charset)."""
    async with client.stream("GET", url, follow_redirects=False, timeout=httpx.Timeout(20.0, connect=10.0),
                             headers={"Accept": "text/html,*/*;q=0.5"}) as r:
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            return ("redirect", r.headers["location"])
        body = b""
        async for chunk in r.aiter_bytes():
            body += chunk
            if len(body) > 300_000:
                break
        return ("page", r.status_code, body, (r.headers.get("content-type") or "").lower(), r.charset_encoding or "utf-8")


async def probe(url: str, domains: list[str]) -> dict[str, Any]:
    """GET the official page (manual redirects, every hop must resolve to a public address). Never raises."""
    from app.services.http import http

    if official_url_problem(url, domains):
        return {"status": "broken", "code": None, "final": url, "note": "not an allow-listed official link"}
    current = url
    for _hop in range(6):
        host = urlsplit(current).hostname or ""
        if urlsplit(current).scheme not in ("https", "http") or not await _public_host(host):
            return {"status": "unreachable", "code": None, "final": current, "note": "DNS failed or non-public address"}
        try:
            try:
                got = await _get(http(), current)
            except httpx.ConnectError as exc:
                if "unable to get local issuer" not in str(exc).lower():
                    raise
                # the portal does not send its intermediate certificate: fetch it like a browser would (AIA), retry
                from app.services.tls_aia import repaired_context

                ctx = await repaired_context(host, _public_host)
                if ctx is None:
                    raise
                async with httpx.AsyncClient(verify=ctx) as fixed:
                    got = await _get(fixed, current)
        except httpx.TimeoutException:
            return {"status": "unreachable", "code": None, "final": current, "note": "timeout"}
        except httpx.ConnectError as exc:
            low = str(exc).lower()
            if "certificate has expired" in low:
                from app.services.tls_aia import expiry

                exp = await expiry(host)
                return {"status": "insecure", "code": None, "final": current,
                        "note": f"security certificate expired{' on ' + exp.date().isoformat() if exp else ''}"}
            if "unable to get issuer" in low or "unable to get local issuer" in low:
                # the chain ends at a root that is not in our (Mozilla) trust list — browsers with newer root stores
                # may accept it: tell admins, do not frighten users with a possibly wrong warning
                return {"status": "unverified", "code": None, "final": current,
                        "note": "certificate chain could not be verified (issuer not in the trusted root list)"}
            if "certificate" in low or "ssl" in low:
                return {"status": "insecure", "code": None, "final": current, "note": "security certificate not valid"}
            return {"status": "unreachable", "code": None, "final": current, "note": "connection failed"}
        except httpx.HTTPError as exc:
            return {"status": "unreachable", "code": None, "final": current, "note": type(exc).__name__}
        if got[0] == "redirect":
            current = urljoin(current, got[1])
            continue
        _, code, body, ctype, charset = got
        text_low = body.decode("utf-8", "ignore").lower()
        hint = next((h for h in _HINTS if h in text_low), None)
        moved_off = official_url_problem(current, domains) is not None
        if code in (404, 410):
            status = "broken"
        elif code >= 500 or code in (403, 429):
            status = "unreachable"          # many portals block bots / are down for maintenance — retry tomorrow
        elif moved_off:
            status = "moved"                # now leaves the official allow-list → needs a human
        else:
            status = "ok"
        html = body.decode(charset, "ignore") if status == "ok" and ("html" in ctype or not ctype) else None
        return {"status": status, "code": code, "final": current, "note": f"page mentions “{hint}”" if hint else None, "html": html}
    return {"status": "broken", "code": None, "final": current, "note": "too many redirects"}


async def check_links(db: AsyncSession, only: list[Any] | None = None) -> dict[str, Any]:
    """Probe every active scheme's link (shared URLs probed once); returns the schemes that need attention."""
    stmt = select(GovScheme).where(GovScheme.status == "active")
    if only:
        stmt = stmt.where(GovScheme.id.in_(only))
    targets = [(s.id, s.url) for s in (await db.execute(stmt)).scalars().all()]
    domains = await scheme_domains(db)
    await db.commit()            # no transaction (nor pooled connection) is held while the portals answer — minutes
    sem = asyncio.Semaphore(4)
    results: dict[str, dict[str, Any]] = {}

    async def one(url: str) -> None:
        async with sem:
            results[url] = await probe(url, domains)

    await asyncio.gather(*(one(u) for u in {u for _, u in targets}))   # a portal shared by schemes is fetched once
    from app.services.scheme_check import cross_check

    attention, changed, schemes = [], [], []
    for sid, url in targets:
        try:
            async with db.begin_nested():       # one scheme at a time, on a FRESH locked row: an admin edit / withdrawal
                s = await db.get(GovScheme, sid, with_for_update=True, populate_existing=True)   # meanwhile is respected
                if s is None or s.status != "active" or s.url != url:
                    continue                     # deleted, withdrawn or re-pointed while we were fetching: next run
                await _apply_probe(db, s, results[url], attention, changed, cross_check)
                await db.flush()
            schemes.append(s)
        except Exception as exc:                 # a row changed / vanished meanwhile, or one odd page: never all of them
            log.warning("scheme %s skipped this run (%s: %s)", sid, type(exc).__name__, exc)
    return {"checked": len(schemes), "urls": len(results), "attention": attention, "changed": changed,
            "by_status": {k: sum(1 for s in schemes if s.link_status == k) for k in ("ok", "moved", "broken", "unreachable", "insecure", "unverified")}}


async def _apply_probe(db: AsyncSession, s: GovScheme, r: dict[str, Any], attention: list, changed: list, cross_check) -> None:
    """Record one scheme's probe (link health + what the official page says) on its fresh, locked row."""
    before = (s.official_check or {}).get("status")
    if r.get("html"):                      # read what the official page SAYS (exists? terms changed?)
        names = [s.name, s.slug.replace("_", " ")] + ([t(f"scheme.{s.slug}.name", "en")] if s.i18n_managed else [])
        try:
            rec = await cross_check(db, s, html=r["html"], names=names, final_url=r["final"])
            if rec.get("status") in ("changed", "may_have_ended") and rec["status"] != before:
                changed.append(s)
        except Exception as exc:          # one odd page never stops the others
            log.warning("official cross-check failed for %s: %s", s.slug, exc)
    s.link_status, s.link_http_status, s.link_checked_at = r["status"], r["code"], now_utc()
    s.link_final_url = (r["final"] or "")[:500] if r["final"] != s.url else None
    s.link_note = (r["note"] or "")[:300] or None
    s.link_failures = 0 if r["status"] == "ok" else s.link_failures + 1
    # broken/moved → tell admins at once; flaky (5xx/timeouts) only after 3 days in a row
    if r["status"] in ("broken", "moved", "insecure") or (r["status"] == "unreachable" and s.link_failures >= 3) \
            or (r["note"] and "mentions" in r["note"]):
        attention.append(s)


async def reviews_due(db: AsyncSession) -> list[GovScheme]:
    return list((await db.execute(select(GovScheme).where(GovScheme.status == "active", GovScheme.review_due_on <= today_ist())
                                  .order_by(GovScheme.review_due_on))).scalars())
