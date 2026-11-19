"""Talisman-style security headers for ASGI with a fresh CSP nonce per request.

Strict CSP (nonce + 'strict-dynamic'), HSTS, frame denial, COOP/CORP, Permissions-Policy, no-store caching
for dynamic responses, Trusted-Types in report-only mode, and CSP violation reporting.
"""
from __future__ import annotations

import secrets

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CDN_SCRIPT_HOSTS = "https://cdn.jsdelivr.net https://unpkg.com"
CDN_STYLE_HOSTS = "https://fonts.googleapis.com https://unpkg.com https://cdn.jsdelivr.net"


class Talisman:
    def __init__(
        self,
        app: ASGIApp,
        *,
        force_https: bool = False,
        hsts_seconds: int = 31536000,
        report_uri: str = "/api/csp-report",
        inline_styles: bool = False,
        static_prefixes: tuple[str, ...] = ("/static/",),
    ) -> None:
        self.app = app
        self.force_https = force_https
        self.hsts_seconds = hsts_seconds
        self.report_uri = report_uri
        self.inline_styles = inline_styles
        self.static_prefixes = static_prefixes

    def _csp(self, nonce: str, host: str, https: bool) -> str:
        style_src = (
            f"'self' 'unsafe-inline' {CDN_STYLE_HOSTS}" if self.inline_styles
            else f"'self' 'nonce-{nonce}' {CDN_STYLE_HOSTS}"
        )
        ws = f"wss://{host}" if https else f"ws://{host} wss://{host}"
        directives = [
            "default-src 'self'",
            # Strict CSP: only nonce'd scripts (and what they load) run. https:/unsafe-inline are ignored by
            # CSP3 browsers when a nonce + strict-dynamic is present (kept only as legacy fallbacks).
            f"script-src 'nonce-{nonce}' 'strict-dynamic' 'self' {CDN_SCRIPT_HOSTS}",
            f"style-src {style_src}",
            "style-src-attr 'unsafe-inline'" if self.inline_styles else "style-src-attr 'none'",
            "font-src 'self' https://fonts.gstatic.com data:",
            "img-src 'self' data: blob: https://*.tile.openstreetmap.org https://unpkg.com",
            f"connect-src 'self' {ws} https://cdn.jsdelivr.net https://unpkg.com",
            "media-src 'self' blob:",
            "worker-src 'self'",
            "manifest-src 'self'",
            "frame-src 'none'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            f"report-uri {self.report_uri}",
            "report-to csp",
        ]
        if https:
            directives.append("upgrade-insecure-requests")
        return "; ".join(directives)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        req_headers = Headers(scope=scope)
        forwarded_proto = req_headers.get("x-forwarded-proto", "")
        https = scope.get("scheme") == "https" or forwarded_proto == "https"
        path = scope.get("path", "")
        if self.force_https and not https and path not in ("/healthz", "/readyz"):
            host = req_headers.get("host", "")
            qs = scope.get("query_string", b"").decode()
            url = f"https://{host}{path}" + (f"?{qs}" if qs else "")
            await RedirectResponse(url, status_code=301)(scope, receive, send)
            return

        nonce = secrets.token_urlsafe(18)
        scope.setdefault("state", {})["csp_nonce"] = nonce
        host = req_headers.get("host", "localhost")
        is_static = path.startswith(self.static_prefixes)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                ctype = h.get("content-type", "")
                if "text/html" in ctype:
                    h["Content-Security-Policy"] = self._csp(nonce, host, https)
                    h["Content-Security-Policy-Report-Only"] = (
                        f"require-trusted-types-for 'script'; report-uri {self.report_uri}"
                    )
                    h["Reporting-Endpoints"] = f'csp="{self.report_uri}"'
                elif path == "/sw.js":
                    # the service worker's own policy governs its fetch() calls — same-origin only
                    h["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
                else:
                    h["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'; sandbox"
                if https:
                    h["Strict-Transport-Security"] = f"max-age={self.hsts_seconds}; includeSubDomains; preload"
                h["X-Content-Type-Options"] = "nosniff"
                h["X-Frame-Options"] = "DENY"
                h["Referrer-Policy"] = "strict-origin-when-cross-origin"
                h["Permissions-Policy"] = (
                    "geolocation=(self), camera=(self), microphone=(self), payment=(), usb=(), serial=(), "
                    "bluetooth=(), magnetometer=(), gyroscope=(), accelerometer=(), display-capture=(), fullscreen=(self)"
                )
                h["Cross-Origin-Opener-Policy"] = "same-origin"
                h["Cross-Origin-Resource-Policy"] = "same-origin"
                h["X-Permitted-Cross-Domain-Policies"] = "none"
                h["Origin-Agent-Cluster"] = "?1"
                h["X-DNS-Prefetch-Control"] = "off"
                if "server" in h:
                    del h["server"]
                if not is_static and "cache-control" not in h:
                    h["Cache-Control"] = "no-store, max-age=0"
                    h["Pragma"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_wrapper)
