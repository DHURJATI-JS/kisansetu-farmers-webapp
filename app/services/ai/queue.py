"""Parallel AI job queue: N workers, priorities (interactive before background), per-user in-flight caps."""
from __future__ import annotations

import asyncio
import itertools
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, TypeVar

from app.config import settings

log = logging.getLogger("kisansetu.aiqueue")
T = TypeVar("T")

PRIORITY_INTERACTIVE = 1
PRIORITY_NORMAL = 5
PRIORITY_BACKGROUND = 9


class QueueBusy(Exception):
    pass


PEOPLE_KEY = "ks:ai:people_at"     # when a person last asked the AI (any worker process) — background work waits


async def _mark_people_busy() -> None:
    import time

    try:
        from app.services.redis_client import kv

        await kv().set(PEOPLE_KEY, str(int(time.time())), ex=3600)
    except Exception:  # noqa: BLE001 - never let bookkeeping slow or fail a person's request
        pass


async def people_quiet_for() -> float:
    """Seconds since a person last asked the AI (large when nobody has)."""
    import time

    try:
        from app.services.redis_client import kv

        at = await kv().get(PEOPLE_KEY)
        return time.time() - float(at) if at else 1e9
    except Exception:  # noqa: BLE001
        return 0.0


class AIQueue:
    def __init__(self, concurrency: int) -> None:
        self.concurrency = max(1, concurrency)
        self._q: asyncio.PriorityQueue | None = None
        self._seq = itertools.count()
        self._workers: list[asyncio.Task] = []
        self._inflight: dict[str, int] = defaultdict(int)
        self.active = 0
        self.processed = 0
        self.failed = 0

    async def start(self) -> None:
        self._q = asyncio.PriorityQueue(maxsize=2000)
        self._bg_lane = asyncio.Semaphore(1)  # background work (translations, advisories) never starves interactive chat
        self._workers = [asyncio.create_task(self._worker(i), name=f"ai-worker-{i}") for i in range(self.concurrency)]
        log.info("AI queue started with %d workers", self.concurrency)

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    def stats(self) -> dict[str, Any]:
        return {"queued": self._q.qsize() if self._q else 0, "active": self.active, "workers": self.concurrency,
                "processed": self.processed, "failed": self.failed}

    async def run(self, fn: Callable[[], Awaitable[T]], *, priority: int = PRIORITY_NORMAL, timeout: float = 180,
                  user_key: str | None = None, max_per_user: int = 2) -> T:
        if self._q is None:  # queue not started (scripts/tests) → run inline
            return await fn()
        if priority >= PRIORITY_BACKGROUND:
            async with self._bg_lane:
                return await self._submit(fn, priority, timeout, user_key, max_per_user)
        await _mark_people_busy()
        return await self._submit(fn, priority, timeout, user_key, max_per_user)

    async def _submit(self, fn: Callable[[], Awaitable[T]], priority: int, timeout: float, user_key: str | None,
                      max_per_user: int) -> T:
        assert self._q is not None
        if user_key and self._inflight[user_key] >= max_per_user:
            raise QueueBusy("too many concurrent AI requests")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        try:
            self._q.put_nowait((priority, next(self._seq), fn, fut))
        except asyncio.QueueFull as exc:
            raise QueueBusy("AI queue full") from exc
        if user_key:
            self._inflight[user_key] += 1
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            if user_key:
                self._inflight[user_key] -= 1
                if self._inflight[user_key] <= 0:
                    self._inflight.pop(user_key, None)

    async def _worker(self, idx: int) -> None:
        assert self._q is not None
        while True:
            _prio, _seq, fn, fut = await self._q.get()
            try:
                if fut.done():
                    continue
                self.active += 1
                try:
                    result = await fn()
                    if not fut.done():
                        fut.set_result(result)
                    self.processed += 1
                except Exception as exc:
                    self.failed += 1
                    if not fut.done():
                        fut.set_exception(exc)
                finally:
                    self.active -= 1
            finally:
                self._q.task_done()


# AI_CONCURRENCY is the budget for the whole deployment: split it across worker processes (the gateway has few slots)
ai_queue = AIQueue(max(1, settings.AI_CONCURRENCY // max(1, settings.WEB_CONCURRENCY)))
