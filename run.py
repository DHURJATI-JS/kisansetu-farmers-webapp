"""Start KisanSetu with production-safe uvicorn flags.

    python run.py            # http://localhost:8088
"""
import os
import sys

import uvicorn

from app.config import settings

if sys.platform == "win32" and settings.WEB_CONCURRENCY > 1 and os.environ.get("KS_ALLOW_WINDOWS_WORKERS") != "1":
    # uvicorn's multi-process mode shares the listening socket between Windows processes; the multi-worker test saw
    # requests stall before reaching any worker (never in the app). Run several workers on Linux / Docker instead.
    print(f"WEB_CONCURRENCY={settings.WEB_CONCURRENCY} ignored on Windows — starting 1 worker (use Docker/Linux to scale out)")
    settings.WEB_CONCURRENCY = 1

if __name__ == "__main__":
    uvicorn.run(
        "app.main:asgi",
        host=settings.HOST,
        port=settings.PORT,
        proxy_headers=True,
        forwarded_allow_ips=settings.TRUSTED_PROXIES,
        server_header=False,
        date_header=True,
        workers=max(1, settings.WEB_CONCURRENCY),  # all shared state lives in Redis / Postgres (see README › Scaling)
        log_level="debug" if settings.DEBUG else "info",
        # longer than any normal pause between clicks and than load-balancer idle timeouts (60 s): a client never sends
        # a request on a connection the server is closing at that instant (the final stress test saw 4 such drops at 10 s)
        timeout_keep_alive=settings.KEEP_ALIVE_SECONDS,
        limit_concurrency=1000,
    )
