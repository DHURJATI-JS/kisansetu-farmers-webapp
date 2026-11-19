"""Shared async HTTP client for external APIs (weather, calamities, geocoding, search, GSTIN, messaging)."""
from __future__ import annotations

import httpx

from app.config import settings

_client: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=8.0),
            headers={"User-Agent": settings.GEOCODER_USER_AGENT},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=10),
        )
    return _client


async def close_http() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
