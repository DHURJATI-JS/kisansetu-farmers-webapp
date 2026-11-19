"""KisanSetu ASGI application.

Run:  uvicorn app.main:asgi --host 0.0.0.0 --port 8088 --proxy-headers --no-server-header
(see run.py — it passes the right flags, including trusted proxy IPs).
"""
from __future__ import annotations

import asyncio
import time
import os
import sys
import logging
from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import APP_DIR, settings
from app.database import SessionLocal, engine, init_db
from app.deps import LoginRequired
from app.errors import AppError, SessionRevoked
from app.i18n import load_catalogs, t
from app.middleware import CachedStatic, LangMiddleware
from app.realtime import sio
from app.security.csrf import CSRFMiddleware
from app.security.guard import GuardMiddleware
from app.security.ratelimit import RateLimitExceeded, RateLimitMiddleware, limiter, rate_limit_exceeded_handler
from app.security.sanitize import SanitizeMiddleware
from app.security.sessions import SessionMiddleware
from app.security.talisman import Talisman

logging.basicConfig(level=logging.DEBUG if settings.DEBUG else logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("kisansetu")


async def bootstrap() -> None:
    """Create the superadmin from env (first run) and embed the crop knowledge base for RAG."""
    from app.models import User
    from app.security.passwords import hash_password, password_problems
    from app.services.crops import seed_crop_knowledge
    from app.utils import normalize_phone, now_utc

    async with SessionLocal() as db:
        phone = normalize_phone(settings.SUPERADMIN_PHONE) if settings.SUPERADMIN_PHONE else None
        if phone and settings.SUPERADMIN_PASSWORD:
            exists = (await db.execute(select(User.id).where(User.phone == phone))).first()
            if not exists:
                problems = password_problems(settings.SUPERADMIN_PASSWORD, "superadmin")
                if problems:
                    log.error("SUPERADMIN_PASSWORD too weak (%s) — superadmin not created", problems)
                else:
                    db.add(User(full_name=settings.SUPERADMIN_NAME, phone=phone, email=(settings.SUPERADMIN_EMAIL or None),
                                password_hash=await hash_password(settings.SUPERADMIN_PASSWORD), role="superadmin",
                                phone_verified=True, photo_verified=True, terms_version="platform-v1",
                                terms_accepted_at=now_utc(), privacy_accepted_at=now_utc()))
                    log.info("superadmin created for %s", phone[:5] + "…")
        try:
            from app.services.schemes import seed_schemes

            n = await seed_schemes(db)
            if n:
                log.info("seeded %d government schemes", n)
        except Exception as exc:
            log.warning("scheme seeding skipped: %s", exc)
        try:
            n = await seed_crop_knowledge(db)
            if n:
                log.info("embedded %d crop knowledge chunks", n)
        except Exception as exc:
            log.warning("crop knowledge seeding skipped: %s", exc)
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.ai.queue import ai_queue
    from app.services.http import close_http
    from app.services.notify import outbound_worker
    from app.services.redis_client import close_redis, init_redis, is_redis
    from app.services.scheduler import start_scheduler, stop_scheduler

    import app.services.actions  # noqa: F401  (register action handlers)
    import app.sockets  # noqa: F401  (register socket events)

    if sys.platform == "win32":
        loop = asyncio.get_running_loop()

        def _quiet_resets(lp, context):
            if isinstance(context.get("exception"), ConnectionResetError) and "_call_connection_lost" in repr(context.get("handle")):
                return
            lp.default_exception_handler(context)

        loop.set_exception_handler(_quiet_resets)
    await init_redis()
    if is_redis() and settings.SOCKETIO_REDIS:
        mgr = socketio.AsyncRedisManager(settings.REDIS_URL, channel="ks-socketio")
        mgr.set_server(sio)
        sio.manager = mgr
    load_catalogs()
    from app.database import advisory_lock
    from app.templating import warm_templates

    async with advisory_lock(7_243_001):   # several workers start at once: schema + seeding one at a time
        if settings.AUTO_CREATE_TABLES and not settings.is_production:
            await init_db()
        await bootstrap()
    log.info("compiled %d templates", warm_templates())
    await ai_queue.start()
    from app.services.ai.providers import router as ai_router

    ai_router.warm()
    stop = asyncio.Event()
    outbound = asyncio.create_task(outbound_worker(stop), name="outbound")
    from app.services.loopmon import monitor as loop_monitor
    from app.services.loopmon import publisher as metrics_publisher

    loopmon = asyncio.create_task(loop_monitor(stop), name="loop-lag")
    metrics_pub = asyncio.create_task(metrics_publisher(stop), name="metrics")
    start_scheduler()
    log.info("%s ready (test_mode=%s)", settings.APP_NAME, settings.TEST_MODE)
    try:
        yield
    finally:
        stop.set()
        stop_scheduler()
        await ai_queue.stop()
        outbound.cancel()
        loopmon.cancel()
        metrics_pub.cancel()
        await close_http()
        await close_redis()
        await engine.dispose()


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api") or "application/json" in request.headers.get("accept", "")


def create_app() -> FastAPI:
    prod = settings.is_production
    app = FastAPI(title=settings.APP_NAME, lifespan=lifespan, docs_url=None if prod else "/api/docs", redoc_url=None,
                  openapi_url=None if prod else "/api/openapi.json")
    app.state.limiter = limiter

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        lang = getattr(request.state, "lang", "en")
        msg = t(exc.key, lang, **exc.params)
        if _wants_json(request):
            return JSONResponse({"error": exc.key, "message": msg}, status_code=exc.status)
        from app.templating import render_error

        return render_error(request, exc.status, msg)

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        target = "/admin/2fa" if exc.next_url == "/admin/2fa" else f"/login?next={exc.next_url}"
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        lang = getattr(request.state, "lang", "en")
        err = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(x) for x in err.get("loc", ()) if x != "body")
        return JSONResponse({"error": "errors.invalid_field", "message": t("errors.invalid_field", lang, field=field or "?",
                                                                            detail=str(err.get("msg", ""))[:120])}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        lang = getattr(request.state, "lang", "en")
        key = {404: "errors.not_found", 405: "errors.method", 403: "errors.forbidden"}.get(exc.status_code, "errors.server")
        if _wants_json(request):
            return JSONResponse({"error": key, "message": t(key, lang)}, status_code=exc.status_code)
        from app.templating import render_error

        return render_error(request, exc.status_code, t(key, lang))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        lang = getattr(request.state, "lang", "en")
        if _wants_json(request):
            return JSONResponse({"error": "errors.server", "message": t("errors.server", lang)}, status_code=500)
        from app.templating import render_error

        return render_error(request, 500, t("errors.server", lang))

    @app.exception_handler(SessionRevoked)
    async def session_revoked(request: Request, exc: SessionRevoked):
        """This device was signed out by the server: clear the cookie, change nothing, say why."""
        from app.security.sessions import clear_session_cookie, logout_url

        lang = getattr(request.state, "lang", "en")
        url = logout_url(exc.end)
        reason = exc.end.get("reason", "signed_out_all")
        if _wants_json(request):
            resp = JSONResponse({"error": "session_revoked", "reason": reason, "redirect": url,
                                 "message": t(f"logout.reason.{reason}.short", lang)}, status_code=401)
        else:
            resp = RedirectResponse(url, status_code=303)
        clear_session_cookie(resp)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

    from app.routers import account, api, auth, guide, pages

    app.include_router(auth.router)
    app.include_router(account.router)
    app.include_router(api.router)
    app.include_router(guide.router)
    app.include_router(pages.router)
    app.mount("/static", CachedStatic(directory=str(APP_DIR / "static")), name="static")

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        body = (APP_DIR / "static" / "js" / "sw.js").read_text("utf-8")
        return Response(body, media_type="application/javascript", headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return JSONResponse({"name": settings.APP_NAME, "short_name": settings.APP_NAME, "start_url": "/dashboard", "display": "standalone",
                             "background_color": "#06281d", "theme_color": "#0b3d2c", "lang": "en",
                             "icons": [{"src": "/static/img/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]},
                            media_type="application/manifest+json")

    @app.get("/robots.txt", include_in_schema=False)
    async def robots():
        return PlainTextResponse("User-agent: *\nDisallow: /api/\nDisallow: /admin\nDisallow: /account\nDisallow: /media/\n")

    @app.get("/.well-known/security.txt", include_in_schema=False)
    async def security_txt():
        contact = settings.SUPERADMIN_EMAIL or "security@example.com"
        return PlainTextResponse(f"Contact: mailto:{contact}\nPreferred-Languages: en, hi\nPolicy: {settings.BASE_URL}/legal/terms\n"
                                 f"Expires: 2027-12-31T23:59:59Z\n")

    # middleware — added inner → outer
    app.add_middleware(SanitizeMiddleware)   # clean every JSON / form / query string before routing
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(LangMiddleware)
    app.add_middleware(SessionMiddleware)
    app.add_middleware(CSRFMiddleware, secure=settings.https, exempt_paths=("/api/csp-report",),
                       trusted_origins=(settings.base_origin,))
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    from app.templating import tailwind_built

    app.add_middleware(Talisman, force_https=settings.FORCE_HTTPS, inline_styles=not tailwind_built())
    return app


fastapi_app = create_app()
# Outermost: IP bans + body limits for HTTP *and* websockets, then Socket.IO, then FastAPI.
asgi = GuardMiddleware(socketio.ASGIApp(sio, other_asgi_app=fastapi_app, socketio_path="socket.io"),
                       max_body_bytes=settings.MAX_BODY_MB * 1024 * 1024)

if os.environ.get("KS_TRACE_REQUESTS") == "1":   # diagnostics only: log start/end of every request with the worker pid
    _inner = asgi
    _trace = logging.getLogger("kisansetu.trace")

    async def asgi(scope, receive, send):  # type: ignore[no-redef]
        if scope["type"] != "http":
            return await _inner(scope, receive, send)
        rid = os.urandom(3).hex()
        t0 = time.perf_counter()
        _trace.info("START %s pid=%s %s %s", rid, os.getpid(), scope.get("method"), scope.get("path"))
        try:
            await _inner(scope, receive, send)
        finally:
            _trace.info("END   %s pid=%s %.0fms", rid, os.getpid(), (time.perf_counter() - t0) * 1000)
