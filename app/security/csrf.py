"""CSRF protection (pure ASGI).

* HttpOnly, SameSite=Strict cookie holds the raw token.
* Pages embed a *masked* token (fresh random pad per render → BREACH-safe with compression).
* Unsafe methods must send `X-CSRF-Token`; Origin / Sec-Fetch-Site are also validated.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
from http.cookies import SimpleCookie
from urllib.parse import urlparse

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

COOKIE = "ks_csrf"
HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
TOKEN_BYTES = 32


def _new_raw() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def _raw_bytes(token: str) -> bytes:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))


def mask_token(raw: str) -> str:
    raw_b = _raw_bytes(raw)
    pad = os.urandom(len(raw_b))
    return base64.urlsafe_b64encode(pad + bytes(a ^ b for a, b in zip(pad, raw_b))).decode().rstrip("=")


def unmask_token(masked: str) -> bytes | None:
    try:
        blob = base64.urlsafe_b64decode(masked + "=" * (-len(masked) % 4))
    except Exception:
        return None
    if len(blob) != 2 * TOKEN_BYTES:
        return None
    pad, x = blob[:TOKEN_BYTES], blob[TOKEN_BYTES:]
    return bytes(a ^ b for a, b in zip(pad, x))


class CSRFMiddleware:
    def __init__(self, app: ASGIApp, *, secure: bool, exempt_paths: tuple[str, ...] = (), trusted_origins: tuple[str, ...] = ()):
        self.app = app
        self.secure = secure
        self.exempt_paths = exempt_paths
        self.trusted_origins = {o.rstrip("/") for o in trusted_origins if o}

    async def _reject(self, send: Send, reason: str) -> None:
        body = json.dumps({"error": "csrf", "detail": reason, "message_key": "errors.csrf"}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        cookies = SimpleCookie()
        try:
            cookies.load(headers.get("cookie", ""))
        except Exception:
            pass
        raw = cookies[COOKIE].value if COOKIE in cookies else None
        valid_raw = False
        if raw:
            try:
                valid_raw = len(_raw_bytes(raw)) == TOKEN_BYTES
            except Exception:
                valid_raw = False
        issue_new = not valid_raw
        if issue_new:
            raw = _new_raw()
        scope.setdefault("state", {})["csrf_raw"] = raw

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        if method not in SAFE_METHODS and not path.startswith(self.exempt_paths):
            host = headers.get("host", "")
            site = headers.get("sec-fetch-site")
            if site and site not in ("same-origin", "none"):
                await self._reject(send, "cross-site request")
                return
            origin = headers.get("origin")
            if origin and origin != "null":
                o = urlparse(origin)
                if o.netloc != host and origin.rstrip("/") not in self.trusted_origins:
                    await self._reject(send, "origin mismatch")
                    return
            sent = headers.get(HEADER)
            unmasked = unmask_token(sent) if sent else None
            if issue_new or not unmasked or not hmac.compare_digest(unmasked, _raw_bytes(raw)):
                await self._reject(send, "missing or invalid token")
                return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and issue_new:
                h = MutableHeaders(scope=message)
                parts = [f"{COOKIE}={raw}", "Path=/", "HttpOnly", "SameSite=Strict", "Max-Age=31536000"]
                if self.secure:
                    parts.append("Secure")
                h.append("set-cookie", "; ".join(parts))
            await send(message)

        await self.app(scope, receive, send_wrapper)
