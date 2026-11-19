"""FastAPI dependencies: DB session, current user (from the Redis session), role guards, page context."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.errors import AppError, Forbidden, SessionRevoked
from app.models import Notification, User
from app.security.sessions import end_session, key_matches, mask_ip

__all__ = ["get_db", "current_user", "require_user", "require_active", "require_admin", "client_ip", "lang_of", "LoginRequired"]


class LoginRequired(Exception):
    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def lang_of(request: Request) -> str:
    return getattr(request.state, "lang", "en")


AUTH_PATHS = ("/login", "/signup", "/forgot", "/offline", "/legal/", "/api/auth/")


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    """The signed-in user — verified against the database on EVERY request (one login per account).

    The session must carry the login key whose hash is on the user row (a newer login replaces it), the user's token
    version (bumped by password reset / ban / deletion) and an active account. Otherwise the session is ended here,
    before any handler runs (so nothing is changed), and SessionRevoked tells the device why."""
    if hasattr(request.state, "user"):
        return request.state.user
    uid = getattr(request.state, "user_id", None)
    sess = getattr(request.state, "session", None) or {}
    user, end = None, None
    if uid:
        try:
            user = await db.get(User, uuid.UUID(uid))
        except ValueError:
            user = None
        if not user or user.status == "deleted":
            end = {"reason": "deleted"}
        elif sess.get("ver") != user.token_version:
            end = {"reason": "signed_out_all"}
        elif not user.session_key_hash:
            end = {"reason": "expired"}
        elif not key_matches(sess, user.session_key_hash):
            end = {"reason": "replaced", "device": user.session_device, "ip": mask_ip(user.session_ip),
                   "at": int(user.session_started_at.timestamp()) if user.session_started_at else None}
        if end:
            await end_session(sess.get("_h", ""), uid, end["reason"], {k: v for k, v in end.items() if k != "reason" and v})
            user = None
    else:
        end = getattr(request.state, "session_end", None)
    request.state.user = user
    if end and not request.url.path.startswith(AUTH_PATHS):
        raise SessionRevoked(end)
    if user and user.language and not request.cookies.get("ks_lang"):
        request.state.lang = user.language
    return user


async def require_user(request: Request, user: User | None = Depends(current_user)) -> User:
    if user is None:
        if request.url.path.startswith("/api"):
            raise AppError("errors.login_required", status=401)
        raise LoginRequired(str(request.url.path))
    return user


async def require_active(user: User = Depends(require_user)) -> User:
    if user.status == "banned":
        raise Forbidden("errors.banned")
    return user


async def require_admin(request: Request, user: User = Depends(require_active)) -> User:
    if not user.is_admin:
        raise Forbidden("errors.admin_only")
    sess = getattr(request.state, "session", None) or {}
    if settings.REQUIRE_ADMIN_2FA and not sess.get("mfa"):
        if request.url.path.startswith("/api"):
            raise Forbidden("errors.admin_2fa_required")
        raise LoginRequired("/admin/2fa")
    return user


async def nav_counts(db: AsyncSession, user: User | None) -> dict[str, Any]:
    if not user:
        return {"unread": 0}
    unread = (await db.execute(select(func.count()).select_from(Notification).where(
        Notification.user_id == user.id, Notification.read_at.is_(None)))).scalar_one()
    return {"unread": int(unread)}
