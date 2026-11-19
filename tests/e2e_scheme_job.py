"""The daily scheme job under concurrent change — directly against the services and the real database (no HTTP).

    python tests/e2e_scheme_job.py

The 1-hour confirmation soak found the job dying with StaleDataError when a scheme row changed while the portals were
being fetched (one long transaction; every scheme's result lost). Here a scheme is deleted and another withdrawn WHILE
the job fetches: the job must finish, skip exactly those two, record the rest — and hold no transaction while it waits.
"""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from e2e_flow import finish, ok  # noqa: E402


async def run() -> None:
    from sqlalchemy import delete, select, text, update

    from app.database import SessionLocal, engine
    from app.models import GovScheme
    from app.services import schemes as svc
    from app.services.redis_client import init_redis
    from app.utils import now_utc

    await init_redis()
    tag = uuid.uuid4().hex[:6]
    slugs = [f"zz_job_{tag}_{i}" for i in range(3)]
    async with SessionLocal() as db:
        for i, slug in enumerate(slugs):
            db.add(GovScheme(slug=slug, name=f"Job test scheme {i}", description="test", url="https://agriinfra.dac.gov.in/",
                             audience=["farmer"], rules={}, level="central", states=[], status="active", builtin=False,
                             i18n_managed=False, version=1, link_status="unknown", link_failures=0))
        await db.commit()
        ids = {s.slug: s.id for s in (await db.execute(select(GovScheme).where(GovScheme.slug.in_(slugs)))).scalars()}
    seen_idle_in_tx = []
    real_probe = svc.probe

    async def slow_probe(url, domains):
        # while the job waits for the portal: one test scheme is deleted, another withdrawn by "an admin"
        async with SessionLocal() as other:
            await other.execute(delete(GovScheme).where(GovScheme.id == ids[slugs[0]]))
            await other.execute(update(GovScheme).where(GovScheme.id == ids[slugs[1]]).values(status="archived"))
            await other.commit()
            n = (await other.execute(text("SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction' "
                                          "AND datname = current_database() AND pid <> pg_backend_pid()"))).scalar_one()
            seen_idle_in_tx.append(n)
        await asyncio.sleep(0.2)
        return {"status": "ok", "code": 200, "final": url, "note": None, "html": "<p>" + "Agriculture Infrastructure Fund " * 30 + "</p>"}

    svc.probe = slow_probe
    t0 = now_utc()
    try:
        async with SessionLocal() as db:
            res = await svc.check_links(db, only=list(ids.values()))
            await db.commit()
        ok(True, "the job finished although schemes changed under it")
    except Exception as exc:  # noqa: BLE001
        ok(False, "the job finished although schemes changed under it", repr(exc))
        res = {"checked": -1}
    finally:
        svc.probe = real_probe
    async with SessionLocal() as db:
        rows = {s.slug: s for s in (await db.execute(select(GovScheme).where(GovScheme.slug.in_(slugs)))).scalars()}
        ok(slugs[0] not in rows, "the deleted scheme is gone (skipped, no crash)")
        ok(rows[slugs[1]].status == "archived" and (rows[slugs[1]].link_checked_at is None or rows[slugs[1]].link_checked_at < t0),
           "the withdrawn one was not touched")
        s2 = rows[slugs[2]]
        ok(s2.link_status == "ok" and s2.link_checked_at and s2.link_checked_at >= t0 and (s2.official_check or {}).get("status"),
           "the unchanged one was checked and recorded", (s2.link_status, s2.official_check and s2.official_check.get("status")))
        ok(res.get("checked") == 1, "exactly one scheme recorded", res.get("checked"))
        ok(max(seen_idle_in_tx or [0]) == 0, "no transaction was held open while the portal was fetched", seen_idle_in_tx)
        await db.execute(delete(GovScheme).where(GovScheme.slug.in_(slugs)))
        await db.commit()
    await engine.dispose()


def main() -> int:
    t0 = time.time()
    asyncio.run(run())
    return finish(t0)


if __name__ == "__main__":
    sys.exit(main())
