"""Jinja2 rendering: autoescaping, CSP nonce, masked CSRF token, i18n, minified asset manifest (+SRI)."""
from __future__ import annotations

import json
from datetime import date, datetime
from functools import lru_cache
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from app.config import APP_DIR, settings
from app.i18n import LANGUAGES, fonts_href, js_catalog, lang_meta, t
from app.security.csrf import mask_token
from app.services.india_data import HELPLINES, STATES
from app.utils import IST, format_inr, short_id

STATIC = APP_DIR / "static"
DIST = STATIC / "dist"

env = Environment(
    loader=FileSystemLoader(str(APP_DIR / "templates")),
    autoescape=select_autoescape(["html", "xml", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
templates = Jinja2Templates(env=env)


@lru_cache(maxsize=1)
def _manifest() -> dict[str, Any]:
    p = DIST / "manifest.json"
    if p.exists() and not settings.DEBUG:
        return json.loads(p.read_text("utf-8"))
    return {}


def asset(name: str) -> str:
    m = _manifest().get(name)
    return f"/static/{m['file']}" if m else f"/static/{name}"


def sri(name: str) -> str:
    m = _manifest().get(name)
    return m.get("integrity", "") if m else ""


def tailwind_built() -> bool:
    return "tailwind.css" in _manifest()


def vendor(name: str, cdn: str) -> str:
    """Self-hosted vendor file if fetched (scripts/fetch_vendor.py), else pinned CDN URL."""
    return f"/static/vendor/{name}" if (STATIC / "vendor" / name).exists() else cdn


def _fmt_dt(v: Any, fmt: str = "%d %b %Y, %H:%M") -> str:
    if not v:
        return ""
    if isinstance(v, datetime):
        return v.astimezone(IST).strftime(fmt)
    if isinstance(v, date):
        return v.strftime("%d %b %Y")
    return str(v)


env.filters.update(inr=format_inr, dt=_fmt_dt, short=short_id, d=lambda v: _fmt_dt(v, "%d %b %Y"))
env.globals.update(
    LANGUAGES=LANGUAGES, asset=asset, sri=sri, vendor=vendor, HELPLINES=HELPLINES, INDIA_STATES=STATES, APP_NAME=settings.APP_NAME,
    TEST_MODE=settings.TEST_MODE, tailwind_built=tailwind_built,
)


def base_context(request: Request) -> dict[str, Any]:
    lang = getattr(request.state, "lang", "en")
    meta = lang_meta(lang)
    raw = getattr(request.state, "csrf_raw", None)

    def _t(key: str, **params: Any) -> str:
        return t(key, lang, **params)

    return {
        "request": request,
        "lang": lang,
        "lang_meta": meta,
        "dir": meta["dir"],
        "t": _t,
        "user": getattr(request.state, "user", None),
        "sio_transports": ",".join(settings.socketio_transports),
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
        "csrf_token": mask_token(raw) if raw else "",
        "js_i18n": js_catalog(lang),
        "fonts_href": fonts_href(lang),
        "theme": request.cookies.get("ks_theme", "system"),
        "unread": getattr(request.state, "unread", 0),
        "path": request.url.path,
    }


def render(request: Request, name: str, ctx: dict[str, Any] | None = None, status_code: int = 200) -> HTMLResponse:
    from app.services.i18n_ai import schedule_fill

    data = base_context(request)
    if ctx:
        data.update(ctx)
    resp = templates.TemplateResponse(request, name, data, status_code=status_code)
    schedule_fill(data["lang"])  # back-fill any missing translations for this language in the background
    return resp


def render_error(request: Request, status: int, message: str) -> HTMLResponse:
    return render(request, "errors/error.html", {"status": status, "message": message}, status_code=status)


def warm_templates() -> int:
    """Compile every template at startup. Lazy compilation stalled the event loop for 100–230 ms per template during the
    first requests after a deploy (seen as loop-lag spikes in the load test's ramp-up)."""
    n = 0
    for name in env.list_templates(extensions=["html"]):
        env.get_template(name)
        n += 1
    return n
