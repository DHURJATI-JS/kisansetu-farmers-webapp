"""Global input sanitisation (text) + output neutralisation (CSV) — defence in depth on top of strict validation and
output escaping (Jinja autoescape, `textContent` in JS).

Every string that enters through a JSON body, a form body or a query string is cleaned by `SanitizeMiddleware`
before any handler sees it; multipart text fields, socket messages and file names are cleaned where they are read.

clean_text():
* NFC normalisation (safe for every Indian script),
* removes NUL / C0 / C1 control characters (keeps tab and newline), unifies line breaks,
* removes Unicode bidi overrides/embeddings/isolates ("Trojan Source" spoofing) and invisible characters
  (zero-width space, word joiner, BOM, soft hyphen) — but KEEPS ZWJ/ZWNJ, which Indic scripts and emoji need,
* removes lone surrogates (they cannot be stored as UTF-8 and used to crash inserts),
* strips HTML/XML markup: script/style/iframe/… elements with their content, then any other tag (text kept);
  plain text such as "<5 acres" or "a < b" is untouched.
Secrets (passwords, OTP/TOTP codes, tokens) are never altered.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qsl, urlencode

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SURROGATES = re.compile("[\ud800-\udfff]")
_LINEBREAKS = re.compile("\r\n|[\r\x85  ]")
_CONTROLS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]")
_BIDI = re.compile("[‪-‮⁦-⁩]")
_INVISIBLE = re.compile("[​⁠﻿­᠎⁡-⁤]")
_DANGEROUS = re.compile(r"<\s*(script|style|iframe|frame|object|embed|applet|svg|math|template|noscript|xml|meta|link|base|form)"
                        r"\b[^>]*>.*?<\s*/\s*\1\s*>", re.I | re.S)
_DANGEROUS_OPEN = re.compile(r"<\s*/?\s*(script|style|iframe|frame|object|embed|applet|svg|math|template|noscript|xml|meta|link|base|form)\b[^>]*>?", re.I)
_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9:-]*(?:\s[^<>]*)?/?>")
_COMMENT = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<![A-Za-z][^>]*>|<\?.*?\?>", re.S)
SECRET_KEYS = frozenset({"password", "new_password", "new_password_again", "current_password", "otp", "totp", "code",
                         "secret", "token", "d"})


def clean_text(value: str, *, single_line: bool = False, max_len: int | None = None) -> str:
    if not value:
        return value
    s = _SURROGATES.sub("", value)
    s = _LINEBREAKS.sub("\n", s)
    s = unicodedata.normalize("NFC", s)
    s = _CONTROLS.sub("", s)
    s = _BIDI.sub("", s)
    s = _INVISIBLE.sub("", s)
    if "<" in s:
        s = _COMMENT.sub("", s)
        s = _DANGEROUS.sub("", s)
        s = _DANGEROUS_OPEN.sub("", s)
        s = _TAG.sub("", s)
    if single_line:
        s = re.sub(r"[\t\n]+", " ", s)
    s = s.strip()
    return s[:max_len] if max_len else s


def clean_obj(obj: Any, key: str | None = None) -> Any:
    """Recursively clean every string in a JSON-like structure (secret fields untouched)."""
    if isinstance(obj, str):
        return obj if (key or "").lower() in SECRET_KEYS else clean_text(obj)
    if isinstance(obj, list):
        return [clean_obj(v, key) for v in obj]
    if isinstance(obj, dict):
        return {(clean_text(k) if isinstance(k, str) else k): clean_obj(v, k if isinstance(k, str) else None) for k, v in obj.items()}
    return obj


def safe_filename(name: str | None, default: str = "file") -> str:
    """Basename only, no control/bidi characters or markup, no leading dots, sane length."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = clean_text(base, single_line=True)
    base = re.sub(r"[^\w.\- ()]+", "_", base).lstrip(".").strip()
    return (base or default)[:120]


_FORMULA = ("=", "+", "-", "@", "\t", "\r", "＝", "＋", "－", "＠")


def csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formula injection (=HYPERLINK(...), +cmd|..., @SUM...) in exported CSV cells.
    Numbers stay numbers; text that would start a formula gets a leading apostrophe."""
    if isinstance(value, str) and value.startswith(_FORMULA):
        return "'" + value
    return value


class SanitizeMiddleware:
    """Cleans query strings and JSON / urlencoded request bodies before routing (pure ASGI, bodies are size-capped
    upstream by GuardMiddleware). Multipart uploads pass through — their text fields are cleaned in the handlers."""

    EXEMPT = ("/api/csp-report", "/socket.io", "/static/")

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "").startswith(self.EXEMPT):
            await self.app(scope, receive, send)
            return
        qs = scope.get("query_string", b"")
        if qs:
            try:
                pairs = parse_qsl(qs.decode("utf-8", "replace"), keep_blank_values=True)
                cleaned = [(clean_text(k), v if k.lower() in SECRET_KEYS else clean_text(v)) for k, v in pairs]
                if cleaned != pairs:
                    scope = dict(scope, query_string=urlencode(cleaned).encode())
            except Exception:
                pass
        if scope.get("method") not in ("POST", "PUT", "PATCH", "DELETE"):
            await self.app(scope, receive, send)
            return
        ctype = Headers(scope=scope).get("content-type", "").split(";")[0].strip().lower()
        if ctype not in ("application/json", "application/x-www-form-urlencoded"):
            await self.app(scope, receive, send)
            return
        chunks, more = [], True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)
        new_body = body
        try:
            if ctype == "application/json" and body.strip():
                data = json.loads(body)
                cleaned = clean_obj(data)
                if cleaned != data:
                    new_body = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            elif ctype == "application/x-www-form-urlencoded" and body:
                pairs = parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True)
                cleaned = [(k, v if k.lower() in SECRET_KEYS else clean_text(v)) for k, v in pairs]
                if cleaned != pairs:
                    new_body = urlencode(cleaned).encode()
        except (ValueError, UnicodeDecodeError):
            new_body = body  # malformed JSON: let validation answer 400/422 as usual
        if new_body is not body:
            headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(new_body)).encode()))
            scope = dict(scope, headers=headers)
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": new_body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
