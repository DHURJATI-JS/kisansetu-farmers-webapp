"""Language negotiation middleware + cache-aware static files."""
from __future__ import annotations

from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from starlette.datastructures import Headers, MutableHeaders
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.i18n import DEFAULT_LANG, is_supported, negotiate


class LangMiddleware:
    """?lang=xx → cookie ks_lang → Accept-Language → English. (User preference is synced to the cookie at login.)"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        lang = None
        qs = parse_qs(scope.get("query_string", b"").decode())
        if "lang" in qs and is_supported(qs["lang"][0]):
            lang = qs["lang"][0]
        set_cookie = lang is not None
        if not lang:
            c = SimpleCookie()
            try:
                c.load(headers.get("cookie", ""))
            except Exception:
                pass
            if "ks_lang" in c and is_supported(c["ks_lang"].value):
                lang = c["ks_lang"].value
        lang = lang or negotiate(headers.get("accept-language")) or DEFAULT_LANG
        scope.setdefault("state", {})["lang"] = lang

        async def send_wrapper(message: Message) -> None:
            if set_cookie and message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("set-cookie", f"ks_lang={lang}; Path=/; Max-Age=31536000; SameSite=Lax")
            await send(message)

        await self.app(scope, receive, send_wrapper)


class CachedStatic(StaticFiles):
    async def get_response(self, path: str, scope: Scope):
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = ("public, max-age=31536000, immutable" if path.startswith("dist/") or path.startswith("vendor/")
                                             else "public, max-age=3600")
            resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp
