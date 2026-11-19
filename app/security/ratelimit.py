"""Async rate limiting on Redis (per user when signed in, else per IP) + AI-supervised abuse escalation.

Replaces slowapi, whose storage is synchronous: every check did blocking Redis I/O on the event loop, so one slow
Redis connect (found by the Locust bug hunt: 2 s on Windows' IPv6-first `localhost`) froze every request in flight.

* Sliding-window counters (current + previous fixed window, weighted) — accurate without per-request sorted sets.
* One Lua round trip per request for all of a route's windows.
* Same API as before: `@limiter.limit("40/minute;300/day", key_func=ip_key)`, `@limiter.exempt`, default limits via
  `RateLimitMiddleware`. Fails open (requests are served) if Redis blips — the outage itself is logged.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("kisansetu.ratelimit")
_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
_SPEC = re.compile(r"^\s*(\d+)\s*/\s*(\d+)?\s*(second|minute|hour|day)s?\s*$")
# KEYS: cur1, prev1, cur2, prev2, ...   ARGV: ttl1, ttl2, ...   → {cur1, prev1, cur2, prev2, ...}
_LUA = """
local out = {}
for i = 1, #KEYS, 2 do
  local cur = redis.call('INCR', KEYS[i])
  if cur == 1 then redis.call('EXPIRE', KEYS[i], ARGV[(i + 1) / 2]) end
  out[#out + 1] = cur
  out[#out + 1] = tonumber(redis.call('GET', KEYS[i + 1]) or '0')
end
return out
"""


@dataclass(frozen=True)
class Rate:
    amount: int
    seconds: int
    text: str


def parse_rates(spec: str) -> tuple[Rate, ...]:
    out = []
    for part in spec.split(";"):
        m = _SPEC.match(part)
        if not m:
            raise ValueError(f"bad rate limit {part!r}")
        out.append(Rate(int(m.group(1)), int(m.group(2) or 1) * _UNITS[m.group(3)], part.strip()))
    return tuple(out)


class RateLimitExceeded(Exception):
    def __init__(self, rate: Rate, retry_after: int) -> None:
        super().__init__(rate.text)
        self.detail = rate.text
        self.retry_after = max(1, retry_after)


def client_ip_from_request(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_key(request: Request) -> str:
    """User id comes from the Redis session validated by SessionMiddleware (forged cookies can't mint buckets)."""
    uid = getattr(request.state, "user_id", None)
    return f"u:{uid}" if uid else f"ip:{client_ip_from_request(request)}"


def ip_key(request: Request) -> str:
    return f"ip:{client_ip_from_request(request)}"


async def hit(scope: str, key: str, rates: tuple[Rate, ...]) -> RateLimitExceeded | None:
    """Count one request against every window; return the violation (if any)."""
    from app.services.redis_client import is_redis, kv

    now = time.time()
    keys: list[str] = []
    ttls: list[int] = []
    for r in rates:
        w = int(now // r.seconds)
        base = f"ks:rl:{scope}:{key}:{r.seconds}:"
        keys += [base + str(w), base + str(w - 1)]
        ttls.append(r.seconds * 2 + 1)
    try:
        if is_redis():
            vals = [int(v) for v in await kv().eval(_LUA, len(keys), *keys, *ttls)]
        else:  # in-process dev fallback
            vals = []
            for i in range(0, len(keys), 2):
                cur = int(await kv().incr(keys[i]))
                if cur == 1:
                    await kv().expire(keys[i], ttls[i // 2])
                vals += [cur, int(await kv().get(keys[i + 1]) or 0)]
    except Exception as exc:
        log.warning("rate limiter unavailable (%s) — request allowed", exc)
        return None
    for i, r in enumerate(rates):
        cur, prev = vals[2 * i], vals[2 * i + 1]
        elapsed = (now % r.seconds) / r.seconds
        if cur + prev * (1 - elapsed) > r.amount:
            return RateLimitExceeded(r, int(r.seconds * (1 - elapsed)) + 1)
    return None


class Limiter:
    def __init__(self, key_func: Callable[[Request], str], default_limits: str) -> None:
        self.key_func = key_func
        self.default = parse_rates(default_limits)
        self.enabled = True

    def limit(self, spec: str, key_func: Callable[[Request], str] | None = None):
        rates = parse_rates(spec)

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            if "request" not in inspect.signature(fn).parameters:
                raise TypeError(f"{fn.__name__} needs a `request: Request` parameter to be rate limited")
            scope = fn.__name__

            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                request = kwargs.get("request") or next((a for a in args if isinstance(a, Request)), None)
                if self.enabled and request is not None:
                    exc = await hit(scope, (key_func or self.key_func)(request), rates)
                    if exc:
                        raise exc
                return await fn(*args, **kwargs)

            return wrapper

        return deco

    def exempt(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        return fn  # default limits are applied by path in RateLimitMiddleware (see EXEMPT_PREFIXES)


limiter = Limiter(key_func=rate_key, default_limits="900/minute")
EXEMPT_PREFIXES = ("/static/", "/healthz", "/readyz", "/internal/", "/sw.js", "/manifest.webmanifest", "/robots.txt",
                   "/.well-known/", "/favicon")


class RateLimitMiddleware:
    """Default limit for every request (pure ASGI — no BaseHTTPMiddleware task per request)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not limiter.enabled or scope.get("path", "").startswith(EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        exc = await hit("default", rate_key(request), limiter.default)
        if exc:
            response = await rate_limit_exceeded_handler(request, exc)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    from app.i18n import t
    from app.services.ai.moderation import abuse_monitor

    ip = client_ip_from_request(request)
    uid = getattr(request.state, "user_id", None)
    asyncio.get_running_loop().create_task(abuse_monitor.on_rate_limit(ip, uid, request.url.path, str(exc.detail)))
    lang = getattr(request.state, "lang", "en")
    msg = t("errors.rate_limited", lang)
    headers = {"Retry-After": str(getattr(exc, "retry_after", 60))}
    if request.url.path.startswith("/api") or "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"error": "rate_limited", "message": msg}, status_code=429, headers=headers)
    from app.templating import render_error

    resp = render_error(request, 429, msg)
    resp.headers.update(headers)
    return resp
