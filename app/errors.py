"""Typed application errors carrying an i18n key (translated at the edge)."""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    status = 400

    def __init__(self, key: str, status: int | None = None, **params: Any) -> None:
        super().__init__(key)
        self.key = key
        self.params = params
        if status is not None:
            self.status = status


class NotFound(AppError):
    status = 404

    def __init__(self, key: str = "errors.not_found", **params: Any) -> None:
        super().__init__(key, **params)


class Forbidden(AppError):
    status = 403

    def __init__(self, key: str = "errors.forbidden", **params: Any) -> None:
        super().__init__(key, **params)


class Conflict(AppError):
    status = 409


class SessionRevoked(Exception):
    """The server ended this device's session (signed in elsewhere, password changed, banned, ...).
    Handled globally: cookie cleared, nothing executed, the person is told why."""

    def __init__(self, end: dict) -> None:
        super().__init__(end.get("reason", "signed_out_all"))
        self.end = end
