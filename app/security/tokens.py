"""Purpose-bound signed tokens (e.g. data-export download links). Sessions live in Redis (sessions.py)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import jwt

from app.config import settings
from app.utils import now_utc

ALGO = "HS256"


def sign_purpose(purpose: str, data: dict[str, Any], minutes: int) -> str:
    now = now_utc()
    payload = {**data, "typ": purpose, "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=minutes)).timestamp())}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGO)


def verify_purpose(purpose: str, token: str) -> dict[str, Any] | None:
    try:
        data = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGO])
    except jwt.PyJWTError:
        return None
    return data if data.get("typ") == purpose else None
