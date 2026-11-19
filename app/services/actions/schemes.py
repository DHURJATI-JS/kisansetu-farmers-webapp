"""Superadmin actions for government schemes: add / update / withdraw / restore / mark re-verified / re-check link.
Every change goes through the GO step, is audited, refreshes every worker's cache after commit, and tells the people
who opened the scheme's portal through the app."""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints
from sqlalchemy import func, select

from app.errors import AppError, Conflict, NotFound
from app.models import GovScheme, SchemeVisit
from app.realtime import after_commit
from app.services.actions.base import Action, Ctx, Payload, Prepared, register
from app.services.actions.fields import ShortText
from app.services.audit import audit
from app.services.india_data import STATES
from app.services.notify import notify_many
from app.services.schemes import (
    SLUG_RE, Rules, bump_version, check_links, name_param, official_url_problem, rules_text, scheme_domains, visitor_ids,
)
from app.services.settings_store import get_setting
from app.utils import now_utc, today_ist

ADMIN = {"superadmin"}
SchemeName = Annotated[str, StringConstraints(min_length=2, max_length=160, strip_whitespace=True)]
SchemeDesc = Annotated[str, StringConstraints(min_length=10, max_length=2000, strip_whitespace=True)]


async def _review_days(ctx: Ctx) -> int:
    try:
        return int(await get_setting(ctx.db, "scheme_review_days") or 180)
    except Exception:
        return 180


async def _check_soon(scheme_id: uuid.UUID) -> None:
    """Probe a (new / changed / restored) scheme link right after the change is committed."""
    from app.database import SessionLocal

    async with SessionLocal() as db:
        await check_links(db, only=[scheme_id])
        await db.commit()
    await bump_version()


async def _scheme(ctx: Ctx, scheme_id: uuid.UUID, *, lock: bool = False) -> GovScheme:
    stmt = select(GovScheme).where(GovScheme.id == scheme_id)
    if lock:
        stmt = stmt.with_for_update()
    s = (await ctx.db.execute(stmt)).scalar_one_or_none()
    if not s:
        raise NotFound()
    return s


async def _visitors(ctx: Ctx, s: GovScheme) -> int:
    return (await ctx.db.execute(select(func.count()).select_from(SchemeVisit).where(SchemeVisit.scheme_id == s.id))).scalar_one()


def _audience(ctx: Ctx, aud: list[str]) -> str:
    return ", ".join(ctx.t(f"role.{a}") for a in aud)


def _states(ctx: Ctx, level: str, states: list[str]) -> str:
    return ctx.t("common.all_india") if level == "central" or not states else ", ".join(states)


@register
class AdminSchemeSave(Action):
    type = "admin.scheme.save"
    roles = ADMIN

    class Payload(Payload):
        scheme_id: uuid.UUID | None = None
        slug: str | None = Field(default=None, max_length=48)
        name: SchemeName
        description: SchemeDesc
        url: str = Field(max_length=500)
        audience: list[Literal["farmer", "owner"]] = Field(min_length=1, max_length=2)
        level: Literal["central", "state"] = "central"
        states: list[str] = Field(default_factory=list, max_length=40)
        rules: Rules = Field(default_factory=Rules)
        source_ref: str | None = Field(default=None, max_length=500)
        change_note: ShortText | None = None
        notify_users: bool = True

    async def _validate(self, ctx: Ctx, p: Payload) -> dict[str, Any]:
        domains = await scheme_domains(ctx.db)
        problem = official_url_problem(p.url, domains)
        if problem:
            raise AppError(problem, domains=", ".join(domains))
        if p.source_ref:
            problem = official_url_problem(p.source_ref, domains)
            if problem:
                raise AppError(problem, domains=", ".join(domains))
        states = sorted(set(p.states))
        if any(s not in STATES for s in states):
            raise AppError("errors.invalid_input")
        if p.level == "state" and not states:
            raise AppError("errors.scheme_states_required")
        if p.level == "central":
            states = []
        return {"name": p.name, "description": p.description, "url": p.url.strip(), "audience": sorted(set(p.audience)),
                "level": p.level, "states": states, "rules": p.rules.compact(), "source_ref": (p.source_ref or "").strip() or None}

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        new = await self._validate(ctx, p)
        if p.scheme_id is None:
            if not p.slug or not SLUG_RE.match(p.slug):
                raise AppError("errors.scheme_slug_invalid")
            if (await ctx.db.execute(select(GovScheme.id).where(GovScheme.slug == p.slug))).first():
                raise Conflict("errors.scheme_slug_taken")
            summary = [ctx.t("sum.scheme_create", name=new["name"], url=new["url"]),
                       ctx.t("sum.scheme_for", audience=_audience(ctx, new["audience"]), where=_states(ctx, new["level"], new["states"])),
                       ctx.t("sum.scheme_rules", rules=rules_text(new["rules"], ctx.t))]
            return Prepared(summary, [ctx.t("warn.scheme_auto_translate")], data={"new": new})
        s = await _scheme(ctx, p.scheme_id)
        if s.status != "active":
            raise Conflict("errors.scheme_archived")
        old = {"name": s.name, "description": s.description, "url": s.url, "audience": sorted(s.audience or []), "level": s.level,
               "states": sorted(s.states or []), "rules": dict(s.rules or {}), "source_ref": s.source_ref}
        changed = [k for k in new if new[k] != old[k]]
        if not changed:
            raise AppError("errors.no_changes")
        summary = [ctx.t("sum.scheme_update", name=s.name, old=s.version, new=s.version + 1)]
        for k in changed:
            if k == "rules":
                o, n = rules_text(old["rules"], ctx.t), rules_text(new["rules"], ctx.t)
            elif k == "audience":
                o, n = _audience(ctx, old["audience"]), _audience(ctx, new["audience"])
            elif k in ("states", "level"):
                o, n = _states(ctx, old["level"], old["states"]), _states(ctx, new["level"], new["states"])
            elif k == "description":
                o, n = (old[k] or "")[:60] + "…", (new[k] or "")[:60] + "…"
            else:
                o, n = old[k] or "—", new[k] or "—"
            line = ctx.t("sum.scheme_change", field=ctx.t(f"scheme.field.{k}"), old=o, new=n)
            if line not in summary:
                summary.append(line)
        warnings = []
        if "url" in changed:
            warnings.append(ctx.t("warn.scheme_url_changed", url=new["url"]))
        if "rules" in changed or "audience" in changed or "states" in changed:
            warnings.append(ctx.t("warn.scheme_rules_changed"))
        if s.i18n_managed and ("name" in changed or "description" in changed):
            warnings.append(ctx.t("warn.scheme_auto_translate"))
        if p.notify_users:
            n_vis = await _visitors(ctx, s)
            if n_vis:
                summary.append(ctx.t("sum.scheme_notify", count=n_vis))
        return Prepared(summary, warnings, data={"new": new, "changed": changed})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        new, days = prep.data["new"], await _review_days(ctx)
        now = now_utc()
        if p.scheme_id is None:
            s = GovScheme(slug=p.slug, **new, status="active", builtin=False, i18n_managed=False, version=1,
                          change_note=p.change_note, review_due_on=today_ist() + timedelta(days=days), last_reviewed_at=now,
                          link_status="unknown", link_failures=0, created_at=now, updated_at=now, updated_by_id=ctx.user.id)
            ctx.db.add(s)
            await ctx.db.flush()
            await audit(ctx.db, ctx.user.id, "scheme.create", "scheme", s.id, {"slug": s.slug, **new}, ctx.ip)
        else:
            s = await _scheme(ctx, p.scheme_id, lock=True)
            if s.status != "active":
                raise Conflict("errors.scheme_archived")
            before = {k: getattr(s, k) for k in prep.data["changed"]}
            for k in prep.data["changed"]:
                setattr(s, k, new[k])
            if "name" in prep.data["changed"] or "description" in prep.data["changed"]:
                s.i18n_managed = False
            if "url" in prep.data["changed"]:
                s.link_status, s.link_failures, s.link_checked_at, s.link_http_status, s.link_note = "unknown", 0, None, None, None
            s.version += 1
            s.updated_at, s.updated_by_id, s.change_note = now, ctx.user.id, p.change_note
            s.last_reviewed_at, s.review_due_on = now, today_ist() + timedelta(days=days)
            await audit(ctx.db, ctx.user.id, "scheme.update", "scheme", s.id,
                        {"before": before, "after": {k: new[k] for k in prep.data["changed"]}, "version": s.version, "note": p.change_note}, ctx.ip)
            if p.notify_users:
                await notify_many(ctx.db, await visitor_ids(ctx.db, s.id), "scheme_update", title_key="notif.scheme_updated.title",
                                  body_key="notif.scheme_updated.body", params={**name_param(s), "note": p.change_note or ""},
                                  link=f"/schemes/go/{s.slug}", severity="info", dedupe_key=f"scheme:{s.id}:v{s.version}")
        after_commit(ctx.db, bump_version)
        if p.scheme_id is None or "url" in prep.data.get("changed", []):
            sid = s.id
            after_commit(ctx.db, lambda: _check_soon(sid))
        return {"message": ctx.t("msg.scheme_saved"), "scheme_id": str(s.id)}


class _Target(Payload):
    scheme_id: uuid.UUID


@register
class AdminSchemeArchive(Action):
    type = "admin.scheme.archive"
    roles = ADMIN

    class Payload(_Target):
        reason: Annotated[str, StringConstraints(min_length=3, max_length=300, strip_whitespace=True)]
        replaced_by: str | None = Field(default=None, max_length=48)
        notify_users: bool = True

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        s = await _scheme(ctx, p.scheme_id)
        if s.status != "active":
            raise Conflict("errors.no_changes")
        summary = [ctx.t("sum.scheme_archive", name=s.name), ctx.t("sum.scheme_archive_reason", reason=p.reason)]
        if p.replaced_by:
            rep = (await ctx.db.execute(select(GovScheme).where(GovScheme.slug == p.replaced_by, GovScheme.status == "active"))).scalar_one_or_none()
            if not rep or rep.id == s.id:
                raise AppError("errors.scheme_replacement_invalid")
            summary.append(ctx.t("sum.scheme_replaced_by", name=rep.name))
        if p.notify_users:
            n_vis = await _visitors(ctx, s)
            if n_vis:
                summary.append(ctx.t("sum.scheme_notify", count=n_vis))
        return Prepared(summary, [ctx.t("warn.scheme_archive")])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        s = await _scheme(ctx, p.scheme_id, lock=True)
        if s.status != "active":
            raise Conflict("errors.no_changes")
        now = now_utc()
        s.status, s.archived_at, s.archived_reason, s.replaced_by = "archived", now, p.reason, p.replaced_by or None
        s.version += 1
        s.updated_at, s.updated_by_id = now, ctx.user.id
        await audit(ctx.db, ctx.user.id, "scheme.archive", "scheme", s.id, {"reason": p.reason, "replaced_by": p.replaced_by}, ctx.ip)
        if p.notify_users:
            await notify_many(ctx.db, await visitor_ids(ctx.db, s.id), "scheme_update", title_key="notif.scheme_withdrawn.title",
                              body_key="notif.scheme_withdrawn.body", params={**name_param(s), "reason": p.reason},
                              link=f"/schemes/go/{s.slug}", severity="warning", dedupe_key=f"scheme:{s.id}:v{s.version}")
        after_commit(ctx.db, bump_version)
        return {"message": ctx.t("msg.scheme_archived")}


@register
class AdminSchemeRestore(Action):
    type = "admin.scheme.restore"
    roles = ADMIN

    class Payload(_Target):
        pass

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        s = await _scheme(ctx, p.scheme_id)
        if s.status != "archived":
            raise Conflict("errors.no_changes")
        domains = await scheme_domains(ctx.db)
        if official_url_problem(s.url, domains):
            raise AppError("errors.scheme_url_not_official", domains=", ".join(domains))
        return Prepared([ctx.t("sum.scheme_restore", name=s.name)], [ctx.t("warn.scheme_restore")])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        s = await _scheme(ctx, p.scheme_id, lock=True)
        if s.status != "archived":
            raise Conflict("errors.no_changes")
        now = now_utc()
        s.status, s.archived_at, s.archived_reason, s.replaced_by = "active", None, None, None
        s.version += 1
        s.updated_at, s.updated_by_id = now, ctx.user.id
        s.link_status, s.link_failures = "unknown", 0
        s.last_reviewed_at, s.review_due_on = now, today_ist() + timedelta(days=await _review_days(ctx))
        await audit(ctx.db, ctx.user.id, "scheme.restore", "scheme", s.id, {}, ctx.ip)
        sid = s.id
        after_commit(ctx.db, bump_version)
        after_commit(ctx.db, lambda: _check_soon(sid))
        return {"message": ctx.t("msg.scheme_restored")}


@register
class AdminSchemeReviewed(Action):
    type = "admin.scheme.reviewed"
    roles = ADMIN

    class Payload(_Target):
        note: ShortText | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        s = await _scheme(ctx, p.scheme_id)
        due = today_ist() + timedelta(days=await _review_days(ctx))
        lines = [ctx.t("sum.scheme_reviewed", name=s.name, date=due.isoformat())]
        if (s.official_check or {}).get("status") in ("changed", "may_have_ended"):
            lines.append(ctx.t("sum.scheme_accept_page"))
        return Prepared(lines)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        s = await _scheme(ctx, p.scheme_id, lock=True)
        s.last_reviewed_at, s.review_due_on = now_utc(), today_ist() + timedelta(days=await _review_days(ctx))
        from app.services.scheme_check import accept_current

        accept_current(s)          # today's official page becomes the reference for the next "did it change?" check
        after_commit(ctx.db, bump_version)
        await audit(ctx.db, ctx.user.id, "scheme.reviewed", "scheme", s.id, {"note": p.note}, ctx.ip)
        return {"message": ctx.t("msg.saved")}


@register
class AdminSchemeCheck(Action):
    type = "admin.scheme.check"
    roles = ADMIN

    class Payload(_Target):
        pass

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        s = await _scheme(ctx, p.scheme_id)
        if s.status != "active":
            raise Conflict("errors.scheme_archived")
        return Prepared([ctx.t("sum.scheme_check", name=s.name, url=s.url)])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        sid = p.scheme_id
        after_commit(ctx.db, lambda: _check_soon(sid))
        return {"message": ctx.t("msg.job_started")}
