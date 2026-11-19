"""Socket.IO events: authenticated rooms (user:<id>), AI chat over websockets (rate limited + queued)."""
from __future__ import annotations

import re

import logging
import time
import uuid
from http.cookies import SimpleCookie

from app.database import SessionLocal
from app.errors import AppError
from app.i18n import is_supported, t
from app.models import User
from app.realtime import sio
from app.security.guard import ip_ban_state
from app.security.sanitize import clean_text
from app.security.sessions import (
    key_matches, load_session, register_socket, session_by_hash, sid_from_cookie_header, unregister_socket,
)
from app.services.redis_client import kv

log = logging.getLogger("kisansetu.sockets")
SOCKET_LIMIT_PER_MIN = 10


def _ip(environ: dict) -> str:
    scope = environ.get("asgi.scope") or {}
    client = scope.get("client")
    return environ.get("REMOTE_ADDR") or (client[0] if client else "unknown")


async def _verified_user(db, sess: dict | None) -> User | None:
    """Same rule as every HTTP request: the session's login key must match the one on the user row."""
    if not sess:
        return None
    user = await db.get(User, uuid.UUID(sess["uid"]))
    if not user or user.status != "active" or user.token_version != sess.get("ver") or not key_matches(sess, user.session_key_hash):
        return None
    return user


@sio.event
async def connect(sid: str, environ: dict, auth: dict | None = None) -> bool:
    ip = _ip(environ)
    banned, shared = await ip_ban_state(ip)
    cookie = environ.get("HTTP_COOKIE", "")
    sess = await load_session(sid_from_cookie_header(cookie))
    if banned and not (shared and sess):   # shared address (CGNAT): signed-in farmers still get their live channel
        return False
    c = SimpleCookie()
    try:
        c.load(cookie)
    except Exception:
        pass
    lang = c["ks_lang"].value if "ks_lang" in c and is_supported(c["ks_lang"].value) else "en"
    uid = None
    if sess:
        async with SessionLocal() as db:
            if await _verified_user(db, sess) is None:
                return False  # a signed-out device never gets a live channel
        uid = sess["uid"]
    await sio.save_session(sid, {"uid": uid, "ver": sess.get("ver") if sess else None, "h": sess["_h"] if sess else None,
                                 "ip": ip, "lang": lang})
    if uid:
        # the socket joins its LOGIN SESSION's room, never a per-user room: personal events are routed to the account's
        # current session(s) at send time, so a signed-out device can't receive them — whichever worker it is on
        await sio.enter_room(sid, f"sess:{sess['_h']}")
        await register_socket(sess["_h"], sid)  # so a later sign-out can cut this connection
    return True


@sio.event
async def disconnect(sid: str, *_args) -> None:
    try:
        s = await sio.get_session(sid)
    except Exception:
        return
    if s.get("h"):
        await unregister_socket(s["h"], sid)


async def _allowed(key: str) -> bool:
    bucket = f"ks:sio:{key}:{int(time.time() // 60)}"
    try:
        n = int(await kv().incr(bucket))
        if n == 1:
            await kv().expire(bucket, 70)
        return n <= SOCKET_LIMIT_PER_MIN
    except Exception:
        return True


@sio.on("ai:ask")
async def ai_ask(sid: str, data: dict) -> None:
    from app.services.ai import assistant
    from app.services.ai.moderation import abuse_monitor

    s = await sio.get_session(sid)
    lang = s.get("lang", "en")
    req_id = str((data or {}).get("id", ""))[:40]
    message = clean_text(str((data or {}).get("message", "")))[:1500]
    key = f"u:{s['uid']}" if s.get("uid") else f"ip:{s['ip']}"
    if not await _allowed(key):
        await sio.emit("ai:reply", {"id": req_id, "error": t("errors.rate_limited", lang)}, to=sid)
        await abuse_monitor.on_rate_limit(s["ip"], s.get("uid"), "socket:ai:ask", f"{SOCKET_LIMIT_PER_MIN}/minute")
        return
    await sio.emit("ai:typing", {"id": req_id}, to=sid)
    async with SessionLocal() as db:
        user = None
        if s.get("uid"):  # re-verify on every event: signed out elsewhere → nothing runs
            user = await _verified_user(db, await session_by_hash(s.get("h")))
            if user is None:
                await sio.emit("ai:reply", {"id": req_id, "error": t("logout.reason.replaced.short", lang)}, to=sid)
                await sio.disconnect(sid)
                return
        try:
            keys = [k for k in ((data or {}).get("page_keys") or [])[:30] if isinstance(k, str) and re.fullmatch(r"[a-z0-9-]{1,40}", k)]
            res = await assistant.chat(db, user, user.language if user else lang, message,
                                       session_key=f"u:{user.id}" if user else f"g:{sid}", ip=s["ip"], page_keys=keys)
            await db.commit()
        except AppError as exc:
            await db.rollback()
            res = {"error": t(exc.key, lang, **exc.params)}
        except Exception:
            log.exception("socket ai:ask failed")
            await db.rollback()
            res = {"error": t("errors.server", lang)}
    await sio.emit("ai:reply", {"id": req_id, **res}, to=sid)
