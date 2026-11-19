"""Argon2id password hashing with a server-side pepper (HMAC) + strength rules."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import settings

_ph = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2, hash_len=32, salt_len=16)

COMMON = {
    "123456", "1234567", "12345678", "123456789", "1234567890", "000000", "111111", "121212", "123123",
    "654321", "666666", "696969", "112233", "999999", "password", "password1", "qwerty", "qwerty123",
    "iloveyou", "admin", "admin123", "welcome", "kisan123", "farmer123", "india123", "abc123", "abcd1234",
}


def _peppered(password: str) -> str:
    return hmac.new(settings.PASSWORD_PEPPER.encode(), password.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_password_sync(password: str) -> str:
    return _ph.hash(_peppered(password))


def verify_password_sync(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, _peppered(password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(hash_password_sync, password)


async def verify_password(password_hash: str, password: str) -> bool:
    return await asyncio.to_thread(verify_password_sync, password_hash, password)


def needs_rehash(password_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(password_hash)
    except Exception:
        return False


def password_problems(password: str, role: str) -> list[str]:
    """Farmers may use a 6+ digit PIN (low-literacy friendly); owners/admins need a strong passphrase."""
    p = password or ""
    issues: list[str] = []
    if p.lower() in COMMON or len(set(p)) <= 2:
        issues.append("errors.password_common")
    if role in ("owner", "superadmin"):
        if len(p) < 10:
            issues.append("errors.password_short_strong")
        if not (re.search(r"[A-Za-z]", p) and re.search(r"\d", p)):
            issues.append("errors.password_mix")
    else:
        if len(p) < 6:
            issues.append("errors.password_short")
        if p.isdigit() and (p in "01234567890123456789" or p in "98765432109876543210"):
            issues.append("errors.password_sequence")
    if len(p) > 128:
        issues.append("errors.password_long")
    return issues
