"""Server-side sessions stored in Redis — ONE login per account.

* The cookie carries only an opaque 256-bit random id. Redis keys use SHA-256(id), so a Redis dump never exposes usable
  session ids.
* Every login mints a fresh random *login key*: the session keeps the key, the user row keeps only SHA-256(key).
  Each request (page reload, API call, form POST, socket event) compares the two before any handler runs — on a mismatch
  the device is signed out and nothing is changed. A new login therefore signs out every other device.
* When a session is ended by the server (signed in elsewhere, password changed, banned, deleted, signed out everywhere)
  a short-lived "tombstone" records WHY, so the signed-out device can tell the person instead of silently dropping them;
  its open Socket.IO connections get a `session:revoked` message and are disconnected (no more live data reaches it).
* Sessions have an absolute lifetime and a sliding idle timeout.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from http.cookies import SimpleCookie
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings
from app.services.redis_client import kv

log = logging.getLogger("kisansetu.sessions")
COOKIE = "ks_session"
TOUCH_EVERY = 300                  # seconds between sliding-expiry refreshes
END_TTL = 7 * 86400                # how long "why was I signed out" is remembered
REASONS = ("replaced", "password_changed", "password_reset", "signed_out_all", "banned", "deleted", "expired", "admin")


def _h(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def _skey(h: str) -> str:
    return f"ks:sess:{h}"


def _ukey(uid: str) -> str:
    return f"ks:usess:{uid}"


def _ekey(h: str) -> str:
    return f"ks:send:{h}"


def _sockkey(h: str) -> str:
    return f"ks:ssock:{h}"


def _idle() -> int:
    return settings.SESSION_IDLE_HOURS * 3600


# ------------------------------------------------------------------ login keys
def new_login_key() -> tuple[str, str]:
    """(key kept in the Redis session, SHA-256 hex stored on the user row)."""
    key = secrets.token_urlsafe(32)
    return key, hashlib.sha256(key.encode()).hexdigest()


def key_matches(session: dict[str, Any] | None, key_hash: str | None) -> bool:
    lk = (session or {}).get("lk")
    if not lk or not key_hash:
        return False
    return hmac.compare_digest(hashlib.sha256(lk.encode()).hexdigest(), key_hash)


_BROWSERS = (("Edg", "Edge"), ("OPR", "Opera"), ("SamsungBrowser", "Samsung Internet"), ("Firefox", "Firefox"),
             ("Chrome", "Chrome"), ("Safari", "Safari"))
_OSES = (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("Windows", "Windows"), ("Mac OS X", "macOS"),
         ("CrOS", "ChromeOS"), ("Linux", "Linux"))


def describe_device(ua: str | None) -> str:
    """'Chrome on Android' — enough for a person to recognise their device; never the raw user-agent."""
    ua = ua or ""
    browser = next((name for tok, name in _BROWSERS if tok in ua), None)
    os_ = next((name for tok, name in _OSES if tok in ua), None)
    if not browser and not os_:
        return "Unknown device"
    return f"{browser or 'Browser'} on {os_ or 'unknown system'}"


def mask_ip(ip: str | None) -> str:
    if not ip:
        return "?"
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
        a, b, _c, _d = ip.split(".")
        return f"{a}.{b}.x.x"
    return ip.split(":")[0] + ":…"


# ------------------------------------------------------------------ lifecycle
async def create_session(user_id: uuid.UUID, token_version: int, ip: str | None, ua: str | None,
                         extra: dict[str, Any] | None = None) -> str:
    sid = secrets.token_urlsafe(32)
    now = int(time.time())
    data = {"uid": str(user_id), "ver": token_version, "ip": ip, "ua": (ua or "")[:200],
            "created": now, "seen": now, "abs_exp": now + settings.SESSION_TTL_DAYS * 86400, **(extra or {})}
    h = _h(sid)
    ttl = min(_idle(), settings.SESSION_TTL_DAYS * 86400)
    await kv().set(_skey(h), json.dumps(data), ex=ttl)
    await kv().sadd(_ukey(str(user_id)), h)
    await kv().expire(_ukey(str(user_id)), settings.SESSION_TTL_DAYS * 86400)
    return sid


async def load_session(sid: str | None) -> dict[str, Any] | None:
    if not sid or len(sid) > 100:
        return None
    h = _h(sid)
    raw = await kv().get(_skey(h))
    if not raw:
        return None
    data = json.loads(raw)
    now = int(time.time())
    if now >= data.get("abs_exp", 0):
        await end_session(h, data.get("uid"), "expired")
        return None
    if now - data.get("seen", 0) > TOUCH_EVERY:
        data["seen"] = now
        ttl = max(60, min(_idle(), data["abs_exp"] - now))
        await kv().set(_skey(h), json.dumps(data), ex=ttl)
    data["_h"] = h
    return data


async def session_by_hash(h: str | None) -> dict[str, Any] | None:
    """The live session behind a socket (sockets keep the hash, never the cookie)."""
    if not h:
        return None
    raw = await kv().get(_skey(h))
    if not raw:
        return None
    data = json.loads(raw)
    data["_h"] = h
    return data


async def session_end(sid: str | None) -> dict[str, Any] | None:
    """Why a (no longer existing) session was ended, if the server ended it."""
    if not sid or len(sid) > 100:
        return None
    raw = await kv().get(_ekey(_h(sid)))
    return json.loads(raw) if raw else None


async def update_session(sid_hash: str, **changes: Any) -> None:
    raw = await kv().get(_skey(sid_hash))
    if not raw:
        return
    data = json.loads(raw)
    data.update(changes)
    ttl = max(60, min(_idle(), data["abs_exp"] - int(time.time())))
    await kv().set(_skey(sid_hash), json.dumps(data), ex=ttl)


async def _cut_sockets(h: str, uid: str | None, payload: dict[str, Any]) -> None:
    """Tell this session's live sockets why (room emit reaches every worker via Redis), then close the room and
    disconnect them. The session already left the account's session set, so no personal event is routed to it."""
    from app.realtime import sio

    room = f"sess:{h}"
    try:
        await sio.emit("session:revoked", payload, room=room)
    except Exception as exc:
        log.debug("socket notify failed: %s", exc)
    try:
        socks = [s.decode() if isinstance(s, bytes) else s for s in await kv().smembers(_sockkey(h))]
        await kv().delete(_sockkey(h))
    except Exception:
        socks = []

    async def later() -> None:
        await asyncio.sleep(1.5)  # let the reason arrive before the connection closes
        try:
            await sio.close_room(room)
        except Exception:
            pass
        for s in socks:
            try:
                await sio.disconnect(s)
            except Exception:
                pass

    asyncio.get_running_loop().create_task(later())


async def end_session(h: str, uid: str | None, reason: str, details: dict[str, Any] | None = None) -> None:
    """Server-side sign-out of one session, remembering the reason for the device that held it."""
    payload = {"reason": reason if reason in REASONS else "signed_out_all", "at": int(time.time()), **(details or {})}
    await kv().set(_ekey(h), json.dumps(payload), ex=END_TTL)
    await kv().delete(_skey(h))
    if uid:
        await kv().srem(_ukey(uid), h)
    await _cut_sockets(h, uid, {"reason": payload["reason"], "redirect": logout_url(payload)})


async def destroy_session(sid: str | None = None, *, sid_hash: str | None = None, user_id: str | None = None) -> None:
    """The person signed out on this device (no tombstone — the cookie is cleared in the same response)."""
    h = sid_hash or (_h(sid) if sid else None)
    if not h:
        return
    await kv().delete(_skey(h))
    if user_id:
        await kv().srem(_ukey(user_id), h)
    await _cut_sockets(h, user_id, {"reason": "logout", "redirect": "/"})


async def destroy_user_sessions(user_id: uuid.UUID | str, except_hash: str | None = None, *, reason: str = "signed_out_all",
                                details: dict[str, Any] | None = None) -> int:
    uid = str(user_id)
    n = 0
    for h in await kv().smembers(_ukey(uid)):
        h = h.decode() if isinstance(h, bytes) else h
        if h == except_hash:
            continue
        if await kv().get(_skey(h)):
            n += 1
        await end_session(h, uid, reason, details)
    return n


async def prune_stale_sessions(user_id: uuid.UUID | str, key_hash: str | None, details: dict[str, Any] | None = None) -> int:
    """After a login: end every session of this user whose login key is not the one now on the user row.
    Two logins racing each other can both create a session; only the committed key may stay usable."""
    uid = str(user_id)
    n = 0
    for h in await kv().smembers(_ukey(uid)):
        h = h.decode() if isinstance(h, bytes) else h
        raw = await kv().get(_skey(h))
        if not raw:
            await kv().srem(_ukey(uid), h)
            continue
        if not key_matches(json.loads(raw), key_hash):
            await end_session(h, uid, "replaced", details)
            n += 1
    return n


async def register_socket(h: str, socket_id: str) -> None:
    await kv().sadd(_sockkey(h), socket_id)
    await kv().expire(_sockkey(h), settings.SESSION_TTL_DAYS * 86400)


async def unregister_socket(h: str, socket_id: str) -> None:
    await kv().srem(_sockkey(h), socket_id)


def logout_url(end: dict[str, Any]) -> str:
    """/login?reason=…&d=<signed details> — details are signed so nobody can forge a scary sign-out notice."""
    from urllib.parse import urlencode

    from app.security.tokens import sign_purpose

    reason = end.get("reason") if end.get("reason") in REASONS else "signed_out_all"
    q = {"reason": reason}
    info = {k: end[k] for k in ("device", "at", "ip") if end.get(k)}
    if info:
        q["d"] = sign_purpose("signout", info, minutes=24 * 60)
    return "/login?" + urlencode(q)


async def list_user_sessions(user_id: uuid.UUID | str) -> list[dict[str, Any]]:
    uid = str(user_id)
    out = []
    for h in await kv().smembers(_ukey(uid)):
        h = h.decode() if isinstance(h, bytes) else h
        raw = await kv().get(_skey(h))
        if not raw:
            await kv().srem(_ukey(uid), h)
            continue
        d = json.loads(raw)
        out.append({"id": h[:12], "hash": h, "ip": mask_ip(d.get("ip")), "device": describe_device(d.get("ua")),
                    "created": d.get("created"), "seen": d.get("seen")})
    return sorted(out, key=lambda x: x.get("seen") or 0, reverse=True)


def set_session_cookie(response: Response, sid: str) -> None:
    response.set_cookie(COOKIE, sid, max_age=settings.SESSION_TTL_DAYS * 86400, httponly=True,
                        secure=settings.https, samesite="lax", path="/")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", secure=settings.https, httponly=True, samesite="lax")


def sid_from_cookie_header(cookie_header: str | None) -> str | None:
    if not cookie_header:
        return None
    c = SimpleCookie()
    try:
        c.load(cookie_header)
    except Exception:
        return None
    return c[COOKIE].value if COOKIE in c else None


class SessionMiddleware:
    """Resolves the Redis session once per request → scope.state.session / user_id (used by deps & limiter).
    A cookie whose session the server ended carries the reason in scope.state.session_end."""

    def __init__(self, app: ASGIApp, skip_prefixes: tuple[str, ...] = ("/static/",)):
        self.app = app
        self.skip_prefixes = skip_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "").startswith(self.skip_prefixes):
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        sid = sid_from_cookie_header(Headers(scope=scope).get("cookie"))
        data = ended = None
        if sid:
            try:
                data = await load_session(sid)
                if data is None:
                    ended = await session_end(sid)
            except Exception:
                data = None
        state["sid"] = sid if data else None
        state["session"] = data
        state["user_id"] = data["uid"] if data else None
        state["session_end"] = ended
        await self.app(scope, receive, send)
