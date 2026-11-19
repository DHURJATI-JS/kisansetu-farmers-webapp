"""Background jobs (APScheduler, Asia/Kolkata). Each job takes a Redis distributed lock so it runs once even with
many app workers, and records a JobRun row for the admin panel."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import timedelta
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, select, update

from app.config import settings
from app.database import SessionLocal
from app.i18n import t
from app.models import AIMessage, Branch, Farm, FarmMember, JobRun, OtpCode, PendingAction, SchemeVisit, User
from app.services.halts import ensure_halt, lift_halts
from app.services.notify import notify, notify_admins, notify_farm_leaders, notify_many
from app.services.payroll import ensure_accruals_for_farm, farm_money, farms_dues_summary, next_due_date, total_farm_due
from app.services.redis_client import distributed_lock
from app.services.settings_store import get_all
from app.services.shop import cancel_open_orders, open_order_count
from app.utils import IST, format_inr, now_utc, today_ist

log = logging.getLogger("kisansetu.scheduler")
scheduler = AsyncIOScheduler(timezone=IST)


def job(name: str, ttl: int = 3000, every: int | None = None) -> Callable[[Callable[[], Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """`every` (seconds) = the schedule period: each period's run happens once across ALL worker processes. The lock alone
    only stopped concurrent runs — with 4 workers every interval job ran 4 times in a row. Admin runs pass force=True."""
    def deco(fn: Callable[[], Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def runner(force: bool = False) -> Any:
            if every and not force:
                import time

                from app.services.redis_client import kv

                if not await kv().set(f"ks:jobslot:{name}:{int(time.time() // every)}", "1", ex=every * 2, nx=True):
                    return None   # another worker already ran this period
            async with distributed_lock(f"job:{name}", ttl) as ok:
                if not ok:
                    return None
                started = now_utc()
                details: Any = None
                success = False
                try:
                    details = await fn()
                    success = True
                    return details
                except Exception as exc:
                    log.exception("job %s failed", name)
                    details = {"error": str(exc)[:500]}
                finally:
                    try:
                        async with SessionLocal() as db:
                            db.add(JobRun(job=name, started_at=started, finished_at=now_utc(), ok=success,
                                          details=details if isinstance(details, dict) else {"result": str(details)[:500]}))
                            await db.commit()
                    except Exception:
                        pass
        runner.__name__ = runner.__qualname__ = name
        return runner
    return deco


async def _ai_due_warning(owner: User, farm: Farm, amount: str, due_date: str) -> str | None:
    from app.i18n import lang_meta
    from app.services.ai.providers import router
    from app.services.ai.queue import PRIORITY_BACKGROUND, ai_queue

    system = (f"Write a short, polite but firm reminder (max 45 words) in {lang_meta(owner.language)['name']} to a farm owner in India: "
              "worker salaries must be paid in full by the due date or farm operations will be halted and workers & the platform "
              "admin will be informed. Plain text only.")
    try:
        res = await ai_queue.run(lambda: router.chat([{"role": "user", "content": f"Farm {farm.name}; amount {amount}; due {due_date}."}],
                                                     system=system, max_tokens=150), priority=PRIORITY_BACKGROUND, timeout=200)
        from app.services.ai.guard import clean_text

        return clean_text(res.text, 600)
    except Exception:
        return None


@job("payroll", every=3600)
async def payroll_tick() -> dict[str, int]:
    stats = defaultdict(int)
    async with SessionLocal() as db:
        cfg = await get_all(db)
        today = today_ist()
        due_date = next_due_date(today, int(cfg["billing_day"]))
        farms = list((await db.execute(select(Farm).where(Farm.status.in_(("active", "closing", "banned"))))).scalars())
        for f in farms:  # accrual inserts are inherently per farm (bulk insert per farm)
            stats["accrued"] += await ensure_accruals_for_farm(db, f.id, today)
            await db.commit()   # release each farm's new rows at once — page views insert the same keys
        # (the notification phase below may wait for the AI; no accrual row is locked while it does)
        summary = await farms_dues_summary(db, [f.id for f in farms], today)
        owners = {u.id: u for u in (await db.execute(select(User).where(User.id.in_({f.owner_id for f in farms})))).scalars()}
        for f in farms:
            s = summary[f.id]
            if s["overdue"] > 0:
                _, created = await ensure_halt(db, f.id, None, "salary_overdue", params={"amount": format_inr(s["overdue"])})
                stats["halted"] += int(created)
                await notify(db, f.owner_id, "salary_overdue", title_key="notif.salary_overdue_owner.title",
                             body_key="notif.salary_overdue_owner.body", params={"farm": f.name, "amount": format_inr(s["overdue"])},
                             severity="danger", link=f"/farms/{f.id}?tab=payroll", channels=("sms", "email", "whatsapp"),
                             dedupe_key=f"overdue:{f.id}:{today}")
                money = await farm_money(db, f.id)
                worker_ids = list((await db.execute(select(FarmMember.user_id).where(
                    FarmMember.id.in_([m for m, mm in money.items() if mm.overdue > 0])))).scalars())
                await notify_many(db, worker_ids, "salary_overdue_worker", title_key="notif.salary_overdue_worker.title",
                                  body_key="notif.salary_overdue_worker.body", params={"farm": f.name}, severity="warning",
                                  channels=("sms",), dedupe_key=f"overduew:{f.id}:{today}")
                await notify_admins(db, "salary_overdue_admin", title_key="notif.salary_overdue_admin.title",
                                    body_key="notif.salary_overdue_admin.body", params={"farm": f.name, "amount": format_inr(s["overdue"])},
                                    link="/admin?tab=farms", severity="danger", dedupe_key=f"overduea:{f.id}:{today}")
            else:
                stats["lifted"] += await lift_halts(db, f.id, "salary_overdue", note="no overdue salary")
                days_left = (due_date - today).days
                if s["due_soon"] > 0 and 0 <= days_left <= int(cfg["salary_warning_days"]):
                    owner = owners.get(f.owner_id)
                    ai_text = await _ai_due_warning(owner, f, format_inr(s["due_soon"]), due_date.isoformat()) if owner else None
                    await notify_farm_leaders(db, f.id, "salary_due_soon", roles=("co_owner", "accountant"),
                                              title_key="notif.salary_due_soon.title", body_key=None if ai_text else "notif.salary_due_soon.body",
                                              body=ai_text, params={"farm": f.name, "amount": format_inr(s["due_soon"]), "date": due_date.isoformat(),
                                                                    "days": days_left},
                                              severity="warning", link=f"/farms/{f.id}?tab=payroll", channels=("sms", "email"),
                                              dedupe_key=f"duesoon:{f.id}:{today}")
                    stats["warned"] += 1
        await db.commit()
    return dict(stats)


@job("surveys", ttl=600, every=900)
async def survey_tick() -> dict[str, int]:
    from app.services.welfare import create_due_surveys

    async with SessionLocal() as db:
        n = await create_due_surveys(db)
        await db.commit()
    return {"created": n}


@job("shops", ttl=240, every=300)
async def shops_tick() -> dict[str, int]:
    """Safety net: a closing shop normally closes the moment its last order is done; one that raced closes here."""
    from app.services.shop import close_finished_shops

    async with SessionLocal() as db:
        n = await close_finished_shops(db)
        await db.commit()
    return {"closed": n}


@job("closures", every=86400)
async def closure_tick() -> dict[str, int]:
    stats = defaultdict(int)
    async with SessionLocal() as db:
        today = today_ist()
        # 1) notices that have run out → end membership only once fully paid
        ending = list((await db.execute(select(FarmMember).where(FarmMember.status == "notice", FarmMember.notice_end_date <= today))).scalars())
        by_farm: dict[Any, list[FarmMember]] = defaultdict(list)
        for m in ending:
            by_farm[m.farm_id].append(m)
        for fid, members in by_farm.items():
            await ensure_accruals_for_farm(db, fid, today)
            money = await farm_money(db, fid, members=members)
            farm = await db.get(Farm, fid)
            for m in members:
                if money[m.id].due <= 0:
                    m.status, m.ended_on, m.end_reason = "ended", m.notice_end_date, "notice_completed"
                    stats["ended"] += 1
                else:
                    await ensure_halt(db, fid, m.branch_id, "settlement_pending", params={"amount": format_inr(money[m.id].due)})
                    await notify(db, farm.owner_id, "settlement_pending", title_key="notif.settlement.title",
                                 body_key="notif.settlement.body", params={"farm": farm.name, "amount": format_inr(money[m.id].due)},
                                 severity="danger", link=f"/farms/{fid}?tab=payroll", dedupe_key=f"settle:{m.id}:{today}")
        # 2) branches / farms approaching or past closure date
        closing = list((await db.execute(select(Branch).where(Branch.status == "closing", Branch.closure_date.isnot(None)))).scalars())
        farms = {f.id: f for f in (await db.execute(select(Farm).where(Farm.id.in_({b.farm_id for b in closing})))).scalars()} if closing else {}
        for b in closing:
            farm = farms[b.farm_id]
            if today >= b.closure_date - timedelta(days=1):
                _, created = await ensure_halt(db, farm.id, b.id, "closure", params={"date": b.closure_date.isoformat()})
                if created:
                    stats["orders_cancelled"] += await cancel_open_orders(db, farm_id=farm.id, branch_id=b.id,
                                                                          reason_key="order.cancel_reason.closing", actor_id=None,
                                                                          farm_name=farm.name)
            if today >= b.closure_date:
                due = await total_farm_due(db, farm.id, b.id)
                if due <= 0 and await open_order_count(db, farm.id, b.id) == 0:
                    b.status, b.closed_at, b.hiring_open = "closed", now_utc(), False
                    await db.execute(update(FarmMember).where(FarmMember.farm_id == farm.id, FarmMember.branch_id == b.id,
                                                              FarmMember.status != "ended")
                                     .values(status="ended", ended_on=today, end_reason="branch_closed"))
                    stats["branches_closed"] += 1
                else:
                    await notify(db, farm.owner_id, "closure_blocked", title_key="notif.closure_blocked.title",
                                 body_key="notif.closure_blocked.body", params={"name": b.name, "amount": format_inr(due)},
                                 severity="danger", link=f"/farms/{farm.id}?tab=payroll", dedupe_key=f"closeblk:{b.id}:{today}")
        await db.flush()
        for f in (await db.execute(select(Farm).where(Farm.status == "closing", Farm.closure_date <= today))).scalars():
            open_branches = (await db.execute(select(Branch.id).where(Branch.farm_id == f.id, Branch.status != "closed"))).first()
            if open_branches:
                continue
            if await total_farm_due(db, f.id) <= 0:
                f.status, f.closed_at = "closed", now_utc()
                await db.execute(update(FarmMember).where(FarmMember.farm_id == f.id, FarmMember.status != "ended")
                                 .values(status="ended", ended_on=today, end_reason="farm_closed"))
                await notify(db, f.owner_id, "farm_closed", title_key="notif.farm_closed.title", body_key="notif.farm_closed.body",
                             params={"farm": f.name}, severity="success")
                stats["farms_closed"] += 1
        await db.commit()
    return dict(stats)


@job("farm_scan", ttl=6 * 3600, every=86400)
async def farm_scan() -> dict[str, int]:
    from app.services.weather import scan_farms

    async with SessionLocal() as db:
        stats = await scan_farms(db)
        await db.commit()
    return stats


@job("job_hunt", ttl=6 * 3600, every=86400)
async def job_hunt() -> dict[str, int]:
    from app.services.jobs import hunt_external_jobs

    async with SessionLocal() as db:
        stats = await hunt_external_jobs(db)
        await db.commit()
    return stats


@job("digest", ttl=1800, every=86400)
async def owner_digest() -> dict[str, int]:
    from app.services.welfare import deliver_owner_warnings

    async with SessionLocal() as db:
        n = await deliver_owner_warnings(db)
        await db.commit()
    return {"warnings": n}


@job("purge", ttl=3600, every=86400)
async def purge_tick() -> dict[str, int]:
    from app.services.deletion import purge_due_accounts
    from app.services.welfare import expire_old_surveys

    async with SessionLocal() as db:
        purged = await purge_due_accounts(db)
        expired = (await db.execute(update(PendingAction).where(PendingAction.status == "prepared", PendingAction.expires_at < now_utc())
                                    .values(status="expired"))).rowcount or 0
        from sqlalchemy import text as _sql

        from app.security.sanitize import SECRET_KEYS
        await db.execute(_sql("UPDATE pending_actions SET payload = payload - CAST(:k AS text[]) "
                              "WHERE status <> 'prepared' AND payload ?| CAST(:k AS text[])"), {"k": sorted(SECRET_KEYS)})
        surveys = await expire_old_surveys(db)
        await db.execute(delete(OtpCode).where(OtpCode.created_at < now_utc() - timedelta(days=2)))
        await db.execute(delete(AIMessage).where(AIMessage.created_at < now_utc() - timedelta(days=90)))
        await db.execute(delete(SchemeVisit).where(SchemeVisit.last_visit_at < now_utc() - timedelta(days=730)))
        await db.commit()
    return {"purged": purged, "actions_expired": expired, "surveys_expired": surveys}


@job("translations", ttl=1800, every=900)
async def translations_tick() -> dict[str, int]:
    """Every 15 minutes, for up to TRANSLATION_MINUTES: translate missing catalog lines — the whole catalog for every
    language people use (most-used first), and the first-visit screens for every other supported language — one batch
    per language per round. Steps aside whenever people use the AI. Whole catalogs at once:
    python scripts/build_translations.py --all"""
    import time

    from sqlalchemy import func

    from app.i18n import LANGUAGES, is_supported
    from app.services.ai.queue import ai_queue, people_quiet_for
    from app.services.i18n_ai import FIRST_VISIT_PREFIXES, fill_batch

    async with SessionLocal() as db:
        used = [code for code, _n in (await db.execute(
            select(User.language, func.count()).where(User.status == "active", User.language != "en")
            .group_by(User.language).order_by(func.count().desc()))).all() if is_supported(code)]
    deadline = time.monotonic() + 60 * float(settings.TRANSLATION_MINUTES)
    out: dict[str, int] = {}
    others = [l["code"] for l in LANGUAGES if l["code"] not in used and l["code"] != "en"]
    pending: list[tuple[str, tuple[str, ...] | None]] = [(c, None) for c in used] + [(c, FIRST_VISIT_PREFIXES) for c in others]
    while pending and time.monotonic() < deadline:
        for item in list(pending):
            q = ai_queue.stats()
            if time.monotonic() >= deadline or q.get("queued") or q.get("active") or await people_quiet_for() < 45:
                return out                      # time is up, or people use the AI right now: the next run continues
            code, prefixes = item
            n = await fill_batch(code, prefixes)
            if n is None:
                pending.remove(item)            # nothing left to try for this language (this run)
            else:
                out[code] = out.get(code, 0) + n
    return out


@job("schemes", ttl=3600, every=86400)
async def scheme_tick() -> dict[str, Any]:
    """Government schemes are not static: probe every official link (moved / broken / withdrawn pages) and remind the
    superadmin to re-verify rules that are due. Admins get one digest per day for links, one per week for reviews."""
    from app.services.schemes import bump_version, check_links, reviews_due

    async with SessionLocal() as db:
        res = await check_links(db)
        due = await reviews_due(db)
        today = today_ist()
        await db.commit()                       # results are safe before the AI is asked anything (no long transaction)
        if res["changed"]:
            from app.models import GovScheme
            from app.services.scheme_check import ai_reading
            from app.services.schemes import rules_text

            readings = {}
            for s in res["changed"][:5]:            # the AI reads what changed (validated, admin-only, never applied)
                rec = dict(s.official_check or {})
                ai = await ai_reading(s.name, rules_text(s.rules or {}, lambda k, **kw: t(k, "en", **kw)),
                                      rec.get("added") or [], rec.get("removed") or [])
                if ai:
                    readings[s.id] = ai
            for sid, ai in readings.items():      # short write on the fresh row: only if the page is still the same one
                fresh = await db.get(GovScheme, sid, with_for_update=True, populate_existing=True)
                rec = dict((fresh.official_check or {}) if fresh else {})
                if fresh is not None and rec.get("status") in ("changed", "may_have_ended"):
                    rec["ai"] = ai
                    fresh.official_check = rec
            await notify_admins(db, "scheme_changed", title_key="notif.scheme_changed.title", body_key="notif.scheme_changed.body",
                                params={"count": len(res["changed"]),
                                        "list": "; ".join(f"{s.name} ({(s.official_check or {}).get('status')})" for s in res["changed"][:12])},
                                link="/admin?tab=schemes", severity="warning", dedupe_key=f"scheme-changed:{today.isoformat()}")
        if res["attention"]:
            lines = "; ".join(f"{s.name} ({s.link_status}{' ' + str(s.link_http_status) if s.link_http_status else ''}"
                              f"{' · ' + s.link_note if s.link_note else ''})" for s in res["attention"][:12])
            await notify_admins(db, "scheme_links", title_key="notif.scheme_links.title", body_key="notif.scheme_links.body",
                                params={"count": len(res["attention"]), "list": lines}, link="/admin?tab=schemes",
                                severity="warning", dedupe_key=f"scheme-links:{today.isoformat()}")
        if due:
            year, week, _ = today.isocalendar()
            await notify_admins(db, "scheme_review", title_key="notif.scheme_review.title", body_key="notif.scheme_review.body",
                                params={"count": len(due), "list": ", ".join(s.name for s in due[:12])}, link="/admin?tab=schemes",
                                severity="info", dedupe_key=f"scheme-review:{year}-W{week}")
        await db.commit()
    await bump_version()
    return {"checked": res["checked"], "urls": res["urls"], "attention": len(res["attention"]), "reviews_due": len(due),
            "official_changed": len(res["changed"]),
            **res["by_status"]}


async def ipban_refresh() -> None:
    from app.services.ai.moderation import refresh_ip_bans

    try:
        await refresh_ip_bans()
    except Exception as exc:
        log.debug("ip ban refresh failed: %s", exc)


async def ai_health() -> None:
    from app.services.ai.providers import router

    await router.ping_gateway()


JOBS: dict[str, Callable[[], Awaitable[Any]]] = {
    "farm_scan": farm_scan, "payroll": payroll_tick, "surveys": survey_tick, "closures": closure_tick,
    "job_hunt": job_hunt, "purge": purge_tick, "digest": owner_digest, "translations": translations_tick,
    "schemes": scheme_tick,
}


def start_scheduler() -> None:
    if not settings.SCHEDULER_ENABLED:
        log.info("scheduler disabled")
        return
    kw = dict(replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
    scheduler.add_job(payroll_tick, IntervalTrigger(hours=1), id="payroll", **kw)
    scheduler.add_job(survey_tick, IntervalTrigger(minutes=15), id="surveys", **kw)
    scheduler.add_job(shops_tick, IntervalTrigger(minutes=5), id="shops", **kw)
    scheduler.add_job(closure_tick, CronTrigger(hour=0, minute=10), id="closures", **kw)
    scheduler.add_job(farm_scan, CronTrigger(hour=6, minute=0), id="farm_scan", **kw)
    scheduler.add_job(job_hunt, CronTrigger(hour=5, minute=0), id="job_hunt", **kw)
    scheduler.add_job(owner_digest, CronTrigger(hour=9, minute=0), id="digest", **kw)
    scheduler.add_job(purge_tick, CronTrigger(hour=2, minute=30), id="purge", **kw)
    scheduler.add_job(translations_tick, IntervalTrigger(minutes=15), id="translations", **kw)
    scheduler.add_job(scheme_tick, CronTrigger(hour=4, minute=30), id="schemes", **kw)
    scheduler.add_job(ipban_refresh, IntervalTrigger(seconds=30), id="ipbans", **kw)
    scheduler.add_job(ai_health, IntervalTrigger(minutes=2), id="ai_health", **kw)
    scheduler.start()
    if settings.RUN_STARTUP_SCAN:
        asyncio.get_running_loop().create_task(farm_scan())
    asyncio.get_running_loop().create_task(payroll_tick())
    log.info("scheduler started")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
