"""Event-loop lag monitor: how late a 250 ms timer fires tells whether the single async worker is saturated."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time

STATE = {"last_ms": 0.0, "max_ms": 0.0, "samples": 0}
HOST = socket.gethostname()      # one app instance = one host (workers share it)
INTERVAL = 0.25


async def monitor(stop: asyncio.Event) -> None:
    while not stop.is_set():
        t0 = time.perf_counter()
        await asyncio.sleep(INTERVAL)
        lag = max(0.0, (time.perf_counter() - t0 - INTERVAL) * 1000)
        STATE["last_ms"] = lag
        STATE["max_ms"] = max(STATE["max_ms"], lag)
        STATE["samples"] += 1


def read_and_reset() -> dict[str, float]:
    out = {"loop_lag_ms": round(STATE["last_ms"], 1), "loop_lag_max_ms": round(STATE["max_ms"], 1)}
    STATE["max_ms"] = 0.0
    return out


def _rss_mb() -> float | None:
    try:
        with open("/proc/self/statm") as fh:                       # Linux / containers
            return round(int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20, 1)
    except (OSError, ValueError, AttributeError):
        try:
            import psutil                                          # Windows dev boxes (optional)

            return round(psutil.Process().memory_info().rss / 2**20, 1)
        except Exception:
            return None


async def publisher(stop: asyncio.Event) -> None:
    """Every 2 s this worker writes its numbers to Redis (ks:metrics:<pid>, 8 s TTL) so /internal/metrics sees all workers."""
    from app.database import engine
    from app.services.ai.queue import ai_queue
    from app.services.redis_client import kv

    while not stop.is_set():
        await asyncio.sleep(2)
        pool = engine.pool
        data = {"pid": os.getpid(), "db_out": pool.checkedout(), "db_overflow": max(0, pool.overflow()), "ai": ai_queue.stats(),
                "lag_ms": round(STATE["last_ms"], 1), "lag_max_ms": round(STATE["max_ms"], 1),
                "cpu_s": round(time.process_time(), 3), "rss_mb": _rss_mb(), "t": round(time.time(), 2)}
        STATE["max_ms"] = 0.0
        try:
            await kv().set(f"ks:metrics:{HOST}:{os.getpid()}", json.dumps(data), ex=8)
        except Exception:
            pass
