"""Redis connection (sessions, rate limits, Socket.IO fan-out, distributed job locks, caches).

Production requires Redis. In development, if Redis is unreachable we fall back to an in-process store
(single worker only) and log a loud warning.
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import redis.asyncio as aioredis

from app.config import settings

log = logging.getLogger("kisansetu.redis")


class MemoryKV:
    """Tiny async subset of the Redis API used by the app (dev fallback only)."""

    def __init__(self) -> None:
        self._d: dict[str, Any] = {}
        self._exp: dict[str, float] = {}

    def _alive(self, k: str) -> bool:
        e = self._exp.get(k)
        if e is not None and e < time.time():
            self._d.pop(k, None)
            self._exp.pop(k, None)
            return False
        return k in self._d

    async def ping(self) -> bool:
        return True

    async def get(self, k: str) -> Any:
        return self._d.get(k) if self._alive(k) else None

    async def set(self, k: str, v: Any, ex: int | None = None, nx: bool = False) -> bool | None:
        if nx and self._alive(k):
            return None
        self._d[k] = v
        if ex:
            self._exp[k] = time.time() + ex
        else:
            self._exp.pop(k, None)
        return True

    async def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            if self._d.pop(k, None) is not None:
                n += 1
            self._exp.pop(k, None)
        return n

    async def expire(self, k: str, seconds: int) -> bool:
        if self._alive(k):
            self._exp[k] = time.time() + seconds
            return True
        return False

    async def incr(self, k: str) -> int:
        v = int(await self.get(k) or 0) + 1
        self._d[k] = v
        return v

    async def sadd(self, k: str, *members: str) -> int:
        s = self._d.setdefault(k, set()) if self._alive(k) or k not in self._d else self._d[k]
        if not isinstance(s, set):
            s = set()
            self._d[k] = s
        before = len(s)
        s.update(members)
        return len(s) - before

    async def srem(self, k: str, *members: str) -> int:
        s = self._d.get(k) if self._alive(k) else None
        if not isinstance(s, set):
            return 0
        before = len(s)
        s.difference_update(members)
        return before - len(s)

    async def smembers(self, k: str) -> set[str]:
        s = self._d.get(k) if self._alive(k) else None
        return set(s) if isinstance(s, set) else set()

    async def scan_iter(self, match: str = "*"):
        for k in list(self._d):
            if self._alive(k) and fnmatch.fnmatch(k, match):
                yield k

    async def ttl(self, k: str) -> int:
        if not self._alive(k):
            return -2
        e = self._exp.get(k)
        return int(e - time.time()) if e else -1

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        # only used for compare-and-delete lock release
        key, token = args[0], args[1]
        if await self.get(key) == token:
            await self.delete(key)
            return 1
        return 0

    async def aclose(self) -> None:
        return None


_client: Any = None
MODE = "none"


async def init_redis() -> None:
    global _client, MODE
    required = settings.REDIS_REQUIRED or settings.is_production
    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=5,
                              socket_connect_timeout=5, health_check_interval=30, retry_on_timeout=True)
        await r.ping()
        _client, MODE = r, "redis"
        log.info("redis connected")
    except Exception as exc:
        if required:
            raise RuntimeError(f"Redis is required but unreachable at REDIS_URL: {exc}") from exc
        log.warning("⚠ Redis unreachable (%s) — using IN-MEMORY sessions/limits (dev only, single worker!)", exc)
        _client, MODE = MemoryKV(), "memory"


def kv() -> Any:
    if _client is None:
        raise RuntimeError("redis not initialised")
    return _client


def is_redis() -> bool:
    return MODE == "redis"


async def close_redis() -> None:
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass


_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


@asynccontextmanager
async def distributed_lock(name: str, ttl: int = 600) -> AsyncIterator[bool]:
    """Yields True if this worker acquired the lock (so only one worker runs a scheduled job)."""
    key = f"ks:lock:{name}"
    token = secrets.token_hex(16)
    acquired = bool(await kv().set(key, token, ex=ttl, nx=True))
    try:
        yield acquired
    finally:
        if acquired:
            try:
                await kv().eval(_RELEASE, 1, key, token)
            except Exception:
                pass


async def cache_get_json(key: str) -> Any:
    import json

    raw = await kv().get(key)
    return json.loads(raw) if raw else None


async def cache_set_json(key: str, value: Any, ttl: int) -> None:
    import json

    await kv().set(key, json.dumps(value, default=str), ex=ttl)


async def wait_for_redis(timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            await kv().ping()
            return
        except Exception:
            await asyncio.sleep(0.5)
