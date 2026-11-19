"""Missing-intermediate TLS repair ("AIA chasing"), the way browsers do it.

Some government portals send only their own certificate and not the intermediate CA that signed it. Browsers fetch the
missing intermediate from the address written inside the certificate (Authority Information Access); Python does not,
so the scheme link checker saw those portals as broken. Here:
  1. read the server's certificate (a handshake only — nothing else is sent or received);
  2. download the intermediate it names (public address only, ≤ 16 KB, must be a CA certificate);
  3. connect again trusting the normal root store PLUS that intermediate.
The chain must still end at a trusted root: the intermediate is never accepted as a trust anchor on its own
(partial-chain verification stays off), so a forged intermediate gets nowhere.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from datetime import datetime
from urllib.parse import urlsplit

import certifi
import httpx
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import AuthorityInformationAccessOID

log = logging.getLogger("kisansetu.tls")
_CACHE: dict[str, bytes] = {}            # AIA URL → intermediate (PEM)


async def peer_certificate(host: str, port: int = 443) -> x509.Certificate | None:
    """The server's own certificate, read without trusting it (used only to learn where its issuer lives / expiry)."""
    def _get() -> bytes | None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=10) as sock, ctx.wrap_socket(sock, server_hostname=host) as s:
            return s.getpeercert(binary_form=True)
    try:
        der = await asyncio.to_thread(_get)
    except OSError:
        return None
    return x509.load_der_x509_certificate(der) if der else None


def _issuer_urls(cert: x509.Certificate) -> list[str]:
    try:
        ext = cert.extensions.get_extension_for_class(x509.AuthorityInformationAccess)
    except x509.ExtensionNotFound:
        return []
    return [d.access_location.value for d in ext.value if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS]


async def _intermediate(url: str, public_host) -> bytes | None:
    if url in _CACHE:
        return _CACHE[url]
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or not await public_host(parts.hostname):
        return None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as c:
            r = await c.get(url)
    except httpx.HTTPError:
        return None
    if r.status_code != 200 or not r.content or len(r.content) > 16_384:
        return None
    try:
        cert = x509.load_der_x509_certificate(r.content)
    except ValueError:
        try:
            cert = x509.load_pem_x509_certificate(r.content)
        except ValueError:
            return None
    try:
        if not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            return None
    except x509.ExtensionNotFound:
        return None
    _CACHE[url] = cert.public_bytes(Encoding.PEM)
    return _CACHE[url]


async def repaired_context(host: str, public_host) -> ssl.SSLContext | None:
    """A verifying SSL context for `host` that also knows its missing intermediate — or None if it cannot be repaired."""
    leaf = await peer_certificate(host)
    if leaf is None:
        return None
    for url in _issuer_urls(leaf)[:2]:
        pem = await _intermediate(url, public_host)
        if not pem:
            continue
        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)   # the root must still be a trusted one
        ctx.load_verify_locations(cadata=pem.decode())
        log.info("TLS: %s does not send its intermediate; using %s", host, url)
        return ctx
    return None


async def expiry(host: str) -> datetime | None:
    leaf = await peer_certificate(host)
    return leaf.not_valid_after_utc if leaf else None
