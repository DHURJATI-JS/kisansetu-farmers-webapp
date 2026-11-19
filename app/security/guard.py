"""Perimeter guard (pure ASGI): IP bans (AI/admin/auto) + request body size limits."""
from __future__ import annotations

import json
import time

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# ip -> (expiry epoch seconds | None = permanent, shared). Rebuilt from the DB every 30 s; updated at once on ban/lift.
BANNED_IPS: dict[str, tuple[float | None, bool]] = {}
_RECHECK: dict[str, float] = {}      # ip -> when to ask Redis again whether a superadmin lifted the ban (another worker)
_NOT_BANNED: dict[str, float] = {}   # short negative cache so clean traffic costs one Redis read per IP per 2 s


def client_ip(scope: Scope) -> str:
    client = scope.get("client")
    return client[0] if client else "unknown"


def _parse_ban(raw: bytes | str) -> tuple[float | None, bool]:
    try:
        d = json.loads(raw)
        return (float(d["e"]) if d.get("e") else None), bool(d.get("s"))
    except (ValueError, TypeError, KeyError):
        return None, False          # older plain-text value: permanent until its Redis TTL, not shared


async def ip_ban_state(ip: str) -> tuple[bool, bool]:
    """(banned, shared). Bans made by ANY worker apply at once (Redis ks:ipban:<ip>); a lift by any worker is seen by
    all within 5 s (Redis ks:iplift:<ip>), even when the ban came from this worker's DB-refreshed table."""
    now = time.time()
    local = BANNED_IPS.get(ip)
    if local is not None:
        exp, shared = local
        if exp is not None and exp < now:
            BANNED_IPS.pop(ip, None)
        elif _RECHECK.get(ip, 0.0) > now:
            return True, shared
        else:
            _RECHECK[ip] = now + 5.0
            try:
                from app.services.redis_client import kv

                if not await kv().exists(f"ks:iplift:{ip}"):
                    return True, shared
            except Exception:
                return True, shared       # Redis down: keep enforcing what we know
            BANNED_IPS.pop(ip, None)
            _NOT_BANNED[ip] = now + 2.0
            return False, False
    if _NOT_BANNED.get(ip, 0.0) > now:
        return False, False
    try:
        from app.services.redis_client import kv

        raw = await kv().get(f"ks:ipban:{ip}")
    except Exception:
        return False, False
    if raw:
        exp, shared = _parse_ban(raw)
        BANNED_IPS[ip] = (exp, shared)
        _RECHECK[ip] = now + 5.0
        return True, shared
    if len(_NOT_BANNED) > 50_000:
        _NOT_BANNED.clear()
    _NOT_BANNED[ip] = now + 2.0
    return False, False


async def ip_banned(ip: str) -> bool:
    return (await ip_ban_state(ip))[0]


async def _live_session(scope: Scope) -> bool:
    """A request carrying a live login session (its Redis record exists). On a SHARED address (a mobile carrier's
    CGNAT) an automatic ban blocks anonymous traffic only: signed-in farmers are judged by their own account."""
    try:
        import hashlib

        from app.security.sessions import sid_from_cookie_header
        from app.services.redis_client import kv

        sid = sid_from_cookie_header(Headers(scope=scope).get("cookie"))
        return bool(sid) and bool(await kv().exists(f"ks:sess:{hashlib.sha256(sid.encode()).hexdigest()}"))
    except Exception:
        return False


class _TooLarge(Exception):
    pass


class GuardMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int, exempt_ips: tuple[str, ...] = ("127.0.0.1", "::1")):
        self.app = app
        self.max_body = max_body_bytes
        self.exempt_ips = exempt_ips

    async def _respond(self, send: Send, status: int, key: str, scope: Scope | None = None) -> None:
        """Same error shape as every API error (error / message_key / message in the reader's language); a browser
        navigating to a page gets a tiny self-contained HTML page instead of raw JSON."""
        message, lang, wants_html = "", "en", False
        try:
            from http.cookies import SimpleCookie

            from app.i18n import is_supported, t

            h = Headers(scope=scope) if scope else Headers()
            c = SimpleCookie()
            c.load(h.get("cookie") or "")
            if "ks_lang" in c and is_supported(c["ks_lang"].value):
                lang = c["ks_lang"].value
            message = t(f"errors.{key}", lang)
            wants_html = "text/html" in (h.get("accept") or "") and not (scope or {}).get("path", "").startswith("/api/")
        except Exception:
            pass
        if wants_html:
            import html

            body = (f'<!doctype html><html lang="{lang}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
                    f'initial-scale=1"><title>{status}</title><body style="font:18px/1.5 system-ui,sans-serif;margin:0;'
                    f'display:grid;place-items:center;min-height:100vh;background:#eaf5ee;color:#10302a"><main style="max-width:'
                    f'32rem;padding:1.5rem;text-align:center"><p>{html.escape(message)}</p></main></body></html>').encode()
            headers = [(b"content-type", b"text/html; charset=utf-8"),
                       (b"content-security-policy", b"default-src 'none'; style-src 'unsafe-inline'")]
        else:
            body = json.dumps({"error": f"errors.{key}", "message_key": f"errors.{key}", "message": message},
                              ensure_ascii=False).encode()
            headers = [(b"content-type", b"application/json")]
        await send({"type": "http.response.start", "status": status,
                    "headers": headers + [(b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        ip = client_ip(scope)
        banned, shared = (False, False) if ip in self.exempt_ips else await ip_ban_state(ip)
        if banned and shared and await _live_session(scope):
            banned = False
        if banned:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self._respond(send, 403, "ip_banned", scope)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        cl = headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > self.max_body:
            await self._respond(send, 413, "too_large", scope)
            return

        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    raise _TooLarge()
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            if not started:
                await self._respond(send, 413, "too_large", scope)
