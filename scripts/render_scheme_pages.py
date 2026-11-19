"""Read JavaScript-only official scheme portals with a headless browser — OUTSIDE the web app.

Many government portals show their text only after JavaScript runs, so the app's daily check marks them "unreadable".
This optional ops task (cron / Task Scheduler, once a day after the app's 04:30 scheme job) renders those pages in a
locked-down headless Chromium and feeds the visible text to the SAME cross-check the app uses (did the scheme end? did
its terms change?). The web app itself never starts a browser or any other process.

Locked down: https only; the page may contact only the allow-listed official domains (plus a few static CDNs that
portals load scripts from); images / media / fonts are not loaded; downloads refused; 45 s budget per page.

    pip install playwright && python -m playwright install chromium
    python scripts/render_scheme_pages.py            # every active scheme the app could not read
    python scripts/render_scheme_pages.py --all      # every active scheme with a working link
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CDNS = ("cdn.jsdelivr.net", "cdnjs.cloudflare.com", "unpkg.com", "ajax.googleapis.com", "code.jquery.com",
        "fonts.googleapis.com", "fonts.gstatic.com", "stackpath.bootstrapcdn.com", "maxcdn.bootstrapcdn.com")


def _allowed(host: str, domains: list[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains) or host in CDNS


async def render(browser, url: str, domains: list[str]) -> tuple[str, str]:
    ctx = await browser.new_context(accept_downloads=False, service_workers="block", java_script_enabled=True,
                                    user_agent="Mozilla/5.0 (KisanSetu official-scheme-page check)")

    async def route(r):
        req = r.request
        if req.resource_type in ("image", "media", "font") or not _allowed(urlsplit(req.url).hostname or "", domains):
            return await r.abort()
        return await r.continue_()

    try:
        await ctx.route("**/*", route)
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:        # some portals poll forever: read what is rendered
            pass
        text = await page.inner_text("body", timeout=5_000)
        return text[:80_000], page.url
    finally:
        await ctx.close()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="render every active scheme with a working link")
    args = ap.parse_args()
    from playwright.async_api import async_playwright
    from sqlalchemy import select

    from app.database import SessionLocal, engine
    from app.i18n import t
    from app.models import GovScheme
    from app.services.redis_client import init_redis
    from app.services.scheme_check import cross_check
    from app.services.schemes import bump_version, official_url_problem, scheme_domains

    await init_redis()
    done = 0
    async with SessionLocal() as db, async_playwright() as pw:
        domains = await scheme_domains(db)
        schemes = (await db.execute(select(GovScheme).where(GovScheme.status == "active", GovScheme.link_status == "ok")
                                    .order_by(GovScheme.slug))).scalars().all()
        # unreadable without JavaScript, or a page shell that never mentions the scheme (e.g. a client-rendered portal)
        todo = [s for s in schemes if args.all or (s.official_check or {}).get("status") in (None, "unreadable")
                or not (s.official_check or {}).get("name_found")]
        browser = await pw.chromium.launch(headless=True)
        try:
            rendered: dict[str, tuple[str, str]] = {}
            for s in todo:
                if official_url_problem(s.url, domains) or urlsplit(s.url).scheme != "https":
                    print(f"skip {s.slug}: not an allow-listed https link")
                    continue
                try:
                    if s.url not in rendered:
                        rendered[s.url] = await asyncio.wait_for(render(browser, s.url, domains), timeout=45)
                except Exception as exc:
                    print(f"{s.slug}: could not render ({type(exc).__name__})")
                    continue
                text, final = rendered[s.url]
                if official_url_problem(final, domains):
                    print(f"{s.slug}: the page left the official domains ({final}) — not read")
                    continue
                names = [s.name, s.slug.replace("_", " ")] + ([t(f"scheme.{s.slug}.name", "en")] if s.i18n_managed else [])
                rec = await cross_check(db, s, names=names, final_url=final, text=text, via="browser")
                print(f"{s.slug:12} {rec.get('status'):15} chars={rec.get('chars')} name={rec.get('name_found')} "
                      f"similarity={rec.get('similarity')} hint={rec.get('hint')}")
                done += 1
        finally:
            await browser.close()
        await db.commit()
    await bump_version()
    await engine.dispose()
    print(f"rendered {done} page(s)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
