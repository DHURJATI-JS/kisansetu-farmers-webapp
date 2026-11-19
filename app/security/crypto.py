"""Field-level encryption (AES-256-GCM with key rotation + AAD binding) and blind indexes."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import unicodedata

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

_PREFIX = "v1"


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


class FieldCipher:
    """`v1.<kid>.<b64(nonce|ciphertext)>`; the AAD binds a ciphertext to its owner/field so rows can't be swapped."""

    def __init__(self, spec: str):
        self.keys: dict[str, bytes] = {}
        self.active: str | None = None
        for part in [p.strip() for p in spec.split(",") if p.strip()]:
            kid, _, b64 = part.partition(":")
            key = _b64d(b64)
            if len(key) != 32:
                raise ValueError(f"encryption key {kid!r} must be 32 bytes (AES-256)")
            self.keys[kid] = key
            self.active = self.active or kid
        if not self.active:
            raise ValueError("no field encryption keys configured")

    def encrypt(self, data: str | bytes, aad: str) -> str:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        nonce = os.urandom(12)
        ct = AESGCM(self.keys[self.active]).encrypt(nonce, raw, aad.encode())
        return f"{_PREFIX}.{self.active}.{_b64e(nonce + ct)}"

    def decrypt(self, token: str, aad: str) -> bytes:
        prefix, kid, body = token.split(".", 2)
        if prefix != _PREFIX or kid not in self.keys:
            raise ValueError("unknown ciphertext format/key")
        blob = _b64d(body)
        return AESGCM(self.keys[kid]).decrypt(blob[:12], blob[12:], aad.encode())

    def decrypt_str(self, token: str, aad: str) -> str:
        return self.decrypt(token, aad).decode("utf-8")

    def encrypt_bytes(self, data: bytes, aad: str) -> bytes:
        nonce = os.urandom(12)
        kid = self.active.encode()
        return b"KSE1" + bytes([len(kid)]) + kid + nonce + AESGCM(self.keys[self.active]).encrypt(nonce, data, aad.encode())

    def decrypt_bytes(self, blob: bytes, aad: str) -> bytes:
        if not blob.startswith(b"KSE1"):
            raise ValueError("not an encrypted blob")
        n = blob[4]
        kid = blob[5 : 5 + n].decode()
        nonce = blob[5 + n : 17 + n]
        return AESGCM(self.keys[kid]).decrypt(nonce, blob[17 + n :], aad.encode())

    def needs_rotation(self, token: str) -> bool:
        try:
            return token.split(".", 2)[1] != self.active
        except Exception:
            return False


def _normalize(value: str) -> str:
    v = unicodedata.normalize("NFKC", value or "")
    return "".join(ch for ch in v.upper() if ch.isalnum())


def blind_index(value: str, domain: str) -> str:
    """Deterministic HMAC so duplicates can be detected without decrypting."""
    msg = f"{domain}:{_normalize(value)}".encode()
    return hmac.new(settings.BLIND_INDEX_KEY.encode(), msg, hashlib.sha256).hexdigest()


def hmac_digest(value: str, purpose: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


cipher = FieldCipher(settings.FIELD_ENCRYPTION_KEYS)
