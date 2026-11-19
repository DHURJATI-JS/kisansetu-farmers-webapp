"""GSTIN: format + checksum validation, OCR extraction from certificates, API verification (auto in TEST_MODE)."""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

from app.config import settings
from app.services.http import http
from app.services.images import load_image
from app.services.india_data import GST_STATE_CODES
from app.services.ocr import ai_vision_json, ocr_all

log = logging.getLogger("kisansetu.gstin")

CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
# expected char class per position: d=digit, a=letter, x=alnum, z='Z'
PATTERN = "ddaaaaaddddaxzx"
TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6", "T": "7"}
TO_ALPHA = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G", "7": "T", "4": "A"}


# business words that say nothing about WHO is registered
_NAME_NOISE = {"farm", "farms", "agro", "agri", "agriculture", "pvt", "private", "ltd", "limited", "llp", "and", "the", "co",
               "company", "enterprise", "enterprises", "traders", "trading", "industries", "sons", "brothers", "group", "india",
               "services", "producer", "producers", "organic", "dairy", "foods", "mr", "mrs", "smt", "shri", "sri"}


def names_match(person: str | None, registered: str | None) -> bool | None:
    """Does the name on a GST registration share a real word with the person claiming it? None = nothing to compare.
    Only a hint for the admin who approves the farm (a company GSTIN can legitimately carry another name)."""
    def words(s: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]+", s.casefold()) if len(w) >= 3 and w not in _NAME_NOISE}
    if not person or not registered:
        return None
    reg, mine = words(registered), words(person)
    return bool(reg & mine) if reg and mine else None      # a name typed in another script: nothing to compare


def checksum_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        prod = CHARS.index(ch) * (2 if i % 2 else 1)
        total += prod // 36 + prod % 36
    return CHARS[(36 - total % 36) % 36]


def is_valid_gstin(g: str | None) -> bool:
    if not g:
        return False
    g = g.strip().upper()
    if not GSTIN_RE.match(g) or g[:2] not in GST_STATE_CODES:
        return False
    return checksum_char(g[:14]) == g[14]


def _repair(window: str) -> str | None:
    out = []
    for ch, kind in zip(window, PATTERN):
        if kind == "d":
            ch = TO_DIGIT.get(ch, ch)
            if not ch.isdigit():
                return None
        elif kind == "a":
            ch = TO_ALPHA.get(ch, ch)
            if not ch.isalpha():
                return None
        elif kind == "z":
            ch = "Z" if ch in ("Z", "2", "7") else ch
            if ch != "Z":
                return None
        out.append(ch)
    return "".join(out)


def extract_gstins(text: str) -> list[str]:
    found: list[str] = []
    for line in (text or "").upper().splitlines():
        compact = re.sub(r"[^A-Z0-9]", "", line)
        for i in range(0, max(0, len(compact) - 14)):
            cand = _repair(compact[i : i + 15])
            if cand and is_valid_gstin(cand):
                found.append(cand)
    return list(dict.fromkeys(found))


async def scan_certificate(data: bytes) -> dict[str, Any]:
    im, _ = load_image(data)
    texts = await ocr_all(im)
    per_engine = {name: (extract_gstins(txt) or [None])[0] for name, txt in texts.items()}
    ai = await ai_vision_json(im, (
        "This is an Indian GST registration certificate (Form GST REG-06). Return ONLY JSON "
        '{"gstin": string|null, "legal_name": string|null, "trade_name": string|null, "confidence": number}'))
    legal_name = trade_name = None
    if ai:
        g = str(ai.get("gstin") or "").upper().replace(" ", "")
        per_engine["gemma"] = g if is_valid_gstin(g) else None
        legal_name = (ai.get("legal_name") or None)
        trade_name = (ai.get("trade_name") or None)
    values = [v for v in per_engine.values() if v]
    gstin, votes = (Counter(values).most_common(1)[0] if values else (None, 0))
    agreeing = [k for k, v in per_engine.items() if v and v == gstin]
    ok = bool(gstin) and (votes >= 2 or (votes == 1 and settings.TEST_MODE))
    return {"ok": ok, "gstin": gstin, "engines": per_engine, "agreeing": agreeing,
            "legal_name": legal_name, "trade_name": trade_name,
            "reason": None if ok else ("unreadable" if not values else "engines_disagree")}


async def verify_via_api(gstin: str) -> dict[str, Any]:
    """Live GSTIN check. TEST_MODE → auto-approved (format & checksum already validated)."""
    if settings.TEST_MODE:
        return {"ok": True, "method": "test_mode_auto", "status": "Active", "legal_name": None}
    if not settings.GSTIN_API_URL:
        return {"ok": False, "method": "api", "error": "gstin_api_not_configured"}
    url = settings.GSTIN_API_URL.format(key=settings.GSTIN_API_KEY, gstin=gstin)
    try:
        r = await http().get(url, timeout=20)
        data = r.json()
    except Exception as exc:
        return {"ok": False, "method": "api", "error": f"api_error: {exc}"}
    info = data.get("data") or data
    status = str(info.get("sts") or info.get("status") or "").strip()
    flag = data.get("flag", True)
    ok = bool(flag) and status.lower() == "active"
    return {"ok": ok, "method": "api", "status": status, "legal_name": info.get("lgnm") or info.get("legal_name"),
            "trade_name": info.get("tradeNam") or info.get("trade_name"), "raw": {k: info.get(k) for k in ("sts", "lgnm", "tradeNam", "rgdt", "ctb")}}
