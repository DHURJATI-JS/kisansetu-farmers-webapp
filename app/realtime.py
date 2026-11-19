"""Socket.IO server instance + 'emit after commit' plumbing.

Emits queued on a DB session are only sent once that transaction commits (no phantom notifications).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import socketio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

log = logging.getLogger("kisansetu.realtime")

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=None,  # same-origin only (blocks cross-site WebSocket hijacking)
    ping_interval=25,
    ping_timeout=20,
    max_http_buffer_size=3_000_000,
    logger=False,
    engineio_logger=False,
)


def queue_emit(db: AsyncSession, room: str, event_name: str, data: dict[str, Any]) -> None:
    db.info.setdefault("pending_emits", []).append((room, event_name, data))


def after_commit(db: AsyncSession, fn: Callable[[], Awaitable[Any]]) -> None:
    """Run `fn()` (e.g. a cache invalidation) only once this transaction has committed — never on rollback."""
    db.info.setdefault("after_commit_tasks", []).append(fn)


async def _run_task(fn: Callable[[], Awaitable[Any]]) -> None:
    try:
        await fn()
    except Exception as exc:
        log.warning("after-commit task failed: %s", exc)


async def emit_now(room: str, event_name: str, data: dict[str, Any]) -> None:
    """`user:<id>` is resolved at send time to that account's live login sessions (`sess:<hash>` rooms)."""
    try:
        if room.startswith("user:"):
            from app.services.redis_client import kv

            for h in await kv().smembers(f"ks:usess:{room[5:]}"):
                h = h.decode() if isinstance(h, bytes) else h
                if await kv().get(f"ks:sess:{h}"):
                    await sio.emit(event_name, data, room=f"sess:{h}")
            return
        await sio.emit(event_name, data, room=room)
    except Exception as exc:  # realtime is best-effort
        log.debug("emit failed: %s", exc)


@event.listens_for(Session, "after_commit")
def _flush_emits(session: Session) -> None:
    emits = session.info.pop("pending_emits", None)
    tasks = session.info.pop("after_commit_tasks", None)
    if not emits and not tasks:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for room, name, data in emits or ():
        loop.create_task(emit_now(room, name, data))
    for fn in tasks or ():
        loop.create_task(_run_task(fn))


@event.listens_for(Session, "after_rollback")
def _drop_emits(session: Session) -> None:
    session.info.pop("pending_emits", None)
    session.info.pop("after_commit_tasks", None)
