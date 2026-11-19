from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog
from app.utils import to_jsonable


def redact(obj: Any) -> Any:
    """Passwords, PINs, OTPs, TOTP codes, tokens and sealed values → "***", at any depth."""
    from app.security.sanitize import SECRET_KEYS

    if isinstance(obj, dict):
        return {k: ("***" if isinstance(k, str) and k.lower() in SECRET_KEYS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


async def audit(db: AsyncSession, actor_id: uuid.UUID | None, action: str, target_type: str | None = None,
                target_id: Any = None, details: dict | None = None, ip: str | None = None) -> None:
    db.add(AuditLog(actor_id=actor_id, action=action, target_type=target_type,
                    target_id=str(target_id) if target_id is not None else None,
                    details=redact(to_jsonable(details or {})), ip=ip))
