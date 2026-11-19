"""Multi-engine OCR (Tesseract + RapidOCR) cross-checked with Gemma 3 vision on the AI gateway.

Used for: GSTIN certificates, salary payment screenshots (amount can NEVER be typed by the owner),
insurance documents, and face/photo verification.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from PIL import Image

from app.config import VAR_DIR, settings
from app.services.images import is_clear, load_image, quality, to_jpeg_bytes
from app.utils import parse_json_loose

log = logging.getLogger("kisansetu.ocr")
_OCR_SEM = asyncio.Semaphore(2)
_rapid: Any = None
_rapid_failed = False
_tess_ok: bool | None = None


# ------------------------------------------------------------------ engines
def _tesseract_text(im: Image.Image) -> str | None:
    global _tess_ok
    if _tess_ok is False:
        return None
    try:
        import pytesseract

        if settings.TESSERACT_CMD:
            pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD
        if _tess_ok is None:
            pytesseract.get_tesseract_version()
            _tess_ok = True
        return pytesseract.image_to_string(im, config="--psm 6")
    except Exception as exc:
        if _tess_ok is None:
            log.info("Tesseract unavailable: %s", exc)
        _tess_ok = False
        return None


def _rapid_text(im: Image.Image) -> str | None:
    global _rapid, _rapid_failed
    if _rapid_failed:
        return None
    try:
        import numpy as np

        if _rapid is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError:  # newer package name
                from rapidocr import RapidOCR  # type: ignore
            _rapid = RapidOCR()
        out = _rapid(np.asarray(im))
        if isinstance(out, tuple):
            result = out[0] or []
            return "\n".join(r[1] for r in result)
        txts = getattr(out, "txts", None) or []
        return "\n".join(txts)
    except Exception as exc:
        log.warning("RapidOCR failed: %s", exc)
        _rapid_failed = True
        return None


ENGINES = {"tesseract": _tesseract_text, "rapidocr": _rapid_text}


async def ocr_all(im: Image.Image) -> dict[str, str]:
    """Run every enabled OCR engine (in threads, bounded concurrency)."""
    async with _OCR_SEM:
        names = [n for n in settings.ocr_engines if n in ENGINES]
        results = await asyncio.gather(*(asyncio.to_thread(ENGINES[n], im) for n in names), return_exceptions=True)
    return {n: r for n, r in zip(names, results) if isinstance(r, str) and r.strip()}


async def ai_vision_json(im: Image.Image, instruction: str, *, max_tokens: int = 250) -> dict | None:
    from app.services.ai.providers import AIUnavailable, router
    from app.services.ai.queue import PRIORITY_INTERACTIVE, ai_queue

    img = (to_jpeg_bytes(im), "image/jpeg")
    try:
        res = await ai_queue.run(
            lambda: router.chat([{"role": "user", "content": instruction}], images=[img], max_tokens=max_tokens, temperature=0.0),
            priority=PRIORITY_INTERACTIVE, timeout=120)
    except (AIUnavailable, Exception) as exc:
        log.info("AI vision unavailable: %s", exc)
        return None
    data = parse_json_loose(res.text)
    if isinstance(data, dict):
        data["_provider"] = res.provider
        return data
    return None


# ------------------------------------------------------------------ amounts (payment screenshots)
_CUR = re.compile(r"(?:₹|rs\.?|inr|rupees?)\s*[:\-]?\s*([0-9]{1,3}(?:,[0-9]{2,3})+(?:\.[0-9]{1,2})?|[0-9]{1,7}(?:\.[0-9]{1,2})?)", re.I)
_NUM = re.compile(r"(?<![\w.:/])([0-9]{1,3}(?:,[0-9]{2,3})+(?:\.[0-9]{1,2})?|[0-9]{1,7}\.[0-9]{2}|[0-9]{2,7})(?![\w:/])")
_GOOD = ("paid", "sent", "debited", "amount", "transferred", "successful", "success", "credited", "payment", "total", "received")
_BAD = ("balance", "fee", "charge", "cashback", "limit", "available")
_REF = re.compile(r"(?:upi\s*(?:ref|reference|transaction)?\s*(?:no|id|number)?|utr|ref(?:erence)?\s*(?:no|id)?|txn\s*id)\s*[:.#\-]?\s*([A-Z0-9]{10,22})", re.I)


def _dec(s: str) -> Decimal | None:
    try:
        d = Decimal(s.replace(",", ""))
        return d if Decimal("1") <= d <= Decimal("10000000") else None
    except (InvalidOperation, ValueError):
        return None


def amount_candidates(text: str) -> list[tuple[Decimal, float]]:
    scored: dict[Decimal, float] = {}
    for line in (text or "").splitlines():
        low = line.casefold()
        bonus = (2.0 if any(k in low for k in _GOOD) else 0.0) - (3.0 if any(k in low for k in _BAD) else 0.0)
        for m in _CUR.finditer(line):
            d = _dec(m.group(1))
            if d is not None:
                scored[d] = max(scored.get(d, -9), 3.0 + bonus + (0.5 if "." in m.group(1) else 0))
        for m in _NUM.finditer(line):
            d = _dec(m.group(1))
            if d is None:
                continue
            raw = m.group(1)
            s = bonus + (0.5 if "," in raw else 0) + (0.5 if "." in raw else 0)
            if re.fullmatch(r"(19|20)\d{2}", raw):
                s -= 3
            scored[d] = max(scored.get(d, -9), s)
    return sorted(scored.items(), key=lambda x: (-x[1], -x[0]))


def best_amount(text: str) -> Decimal | None:
    c = amount_candidates(text)
    return c[0][0] if c and c[0][1] >= 1.0 else None


def consensus(values: dict[str, Decimal | None]) -> tuple[Decimal | None, list[str]]:
    present = {k: v.quantize(Decimal("0.01")) for k, v in values.items() if v is not None}
    if not present:
        return None, []
    counts = Counter(present.values())
    amount, n = counts.most_common(1)[0]
    if n == 1 and len(present) > 1:
        return None, []  # engines disagree → ask for a clearer image
    return amount, [k for k, v in present.items() if v == amount]


async def analyze_payment_screenshot(data: bytes) -> dict[str, Any]:
    im, _fmt = load_image(data)
    q = quality(im)
    clear, reason = is_clear(q)
    if not clear:
        return {"ok": False, "reason": reason, "quality": q}
    texts = await ocr_all(im)
    per_engine: dict[str, Decimal | None] = {name: best_amount(txt) for name, txt in texts.items()}
    ai = await ai_vision_json(im, (
        "Read this Indian payment screenshot or cash receipt (UPI/bank transfer/receipt). Return ONLY JSON: "
        '{"amount": number|null, "status": "success|failed|pending|unknown", "reference": string|null, '
        '"date": string|null, "payee": string|null, "confidence": number}. The amount is the money actually '
        "transferred — never the balance, fee or cashback."))
    status = "unknown"
    ref = None
    if ai:
        status = str(ai.get("status") or "unknown").lower()
        try:
            per_engine["gemma"] = _dec(str(ai.get("amount"))) if ai.get("amount") is not None else None
        except Exception:
            per_engine["gemma"] = None
        ref = (str(ai.get("reference")) if ai.get("reference") else None)
    if not ref:
        for txt in texts.values():
            m = _REF.search(txt)
            if m:
                ref = m.group(1)
                break
    if status in ("failed", "pending"):
        return {"ok": False, "reason": "payment_not_successful", "quality": q, "engines": _fmt_engines(per_engine), "status": status}
    amount, agreeing = consensus(per_engine)
    sources = [k for k, v in per_engine.items() if v is not None]
    ok = amount is not None and (len(agreeing) >= 2 or (len(sources) == 1 and settings.TEST_MODE))
    reason = None
    if not ok:
        reason = "amount_unreadable" if not sources else ("engines_disagree" if amount is None else "single_source")
    ref_clean = re.sub(r"[^A-Z0-9]", "", ref.upper())[:22] if ref else None
    return {
        "ok": ok, "reason": reason, "amount": str(amount) if amount is not None else None, "agreeing": agreeing,
        "engines": _fmt_engines(per_engine), "status": status, "reference": ref_clean if ref_clean and len(ref_clean) >= 10 else None,
        "quality": q, "confidence": round(len(agreeing) / max(1, len(sources)), 2) if amount is not None else 0.0,
    }


def _fmt_engines(d: dict[str, Decimal | None]) -> dict[str, str | None]:
    return {k: (str(v) if v is not None else None) for k, v in d.items()}


# ------------------------------------------------------------------ faces
_face_cascade: Any = None
_face_loaded = False


def _load_cascade() -> Any:
    global _face_cascade, _face_loaded
    if _face_loaded:
        return _face_cascade
    _face_loaded = True
    try:
        import cv2

        candidates = [VAR_DIR / "models" / "haarcascade_frontalface_default.xml"]
        if getattr(cv2, "data", None) and getattr(cv2.data, "haarcascades", ""):
            candidates.append(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
        for p in candidates:
            if p.exists():
                c = cv2.CascadeClassifier(str(p))
                if not c.empty():
                    _face_cascade = c
                    break
    except Exception as exc:
        log.warning("OpenCV face detector unavailable: %s", exc)
    if _face_cascade is None:   # face checks would then rest on the AI alone — say so loudly
        log.warning("OpenCV face detector NOT available (need opencv 4.x with CascadeClassifier + the Haar model)")
    return _face_cascade


_alt_cascade: Any = None
_alt_loaded = False


def _load_alt_cascade() -> Any:
    """OpenCV's second frontal-face model (alt2), shipped with opencv-python — used only for a second look."""
    global _alt_cascade, _alt_loaded
    if _alt_loaded:
        return _alt_cascade
    _alt_loaded = True
    try:
        import cv2

        p = Path(cv2.data.haarcascades) / "haarcascade_frontalface_alt2.xml"
        if p.exists():
            c = cv2.CascadeClassifier(str(p))
            _alt_cascade = None if c.empty() else c
    except Exception as exc:  # noqa: BLE001
        log.info("second OpenCV face model unavailable: %s", exc)
    return _alt_cascade


def _opencv_faces(im: Image.Image, second_look: bool = False) -> list[tuple[int, int, int, int]] | None:
    """Faces covering ≥3 % of the photo. The strict pass is the default model on an equalised image. second_look tries
    the other model / settings in turn (laptop webcams: side light, glasses, a slight turn) and returns the first hit."""
    cascade = _load_cascade()
    if cascade is None:
        return None
    import cv2
    import numpy as np

    small = im.copy()
    small.thumbnail((900, 900))
    raw = cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2GRAY)
    eq = cv2.equalizeHist(raw)
    w, h = small.size
    alt = _load_alt_cascade() if second_look else None
    passes = [(cascade, eq, 6)] if not second_look else \
        [p for p in ((alt, eq, 6), (alt, raw, 6), (cascade, raw, 4), (alt, eq, 4)) if p[0] is not None]
    for model, gray, neighbours in passes:
        faces = model.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=neighbours, minSize=(60, 60))
        kept = [tuple(map(int, f)) for f in faces if (f[2] * f[3]) / float(w * h) >= 0.03]
        if kept:
            return kept
    return []


def main_faces(faces: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """The faces that matter: at least a quarter of the largest face's area. A small detection beside a large face is
    background (a poster, a pattern); faces of similar size are several people and still count."""
    if not faces:
        return []
    biggest = max(f[2] * f[3] for f in faces)
    return [f for f in faces if f[2] * f[3] >= 0.25 * biggest]


def _count(v: Any, default: int = 0) -> int:
    """A face count from the AI's JSON: a number, "1", or something unusable (→ default) — never an exception."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


async def face_check(data: bytes) -> dict[str, Any]:
    im, _ = load_image(data)
    q = quality(im)
    if min(q["width"], q["height"]) < 200:
        return {"ok": False, "reason": "too_small", "quality": q}
    faces = await asyncio.to_thread(_opencv_faces, im)
    texts = await ocr_all(im)
    text_len = max((len(re.sub(r"\s", "", t)) for t in texts.values()), default=0)
    ai = await ai_vision_json(im, (
        "Is this a real photograph of exactly ONE human face (not a drawing, cartoon, screen photo, document, ID card, "
        'object, animal or group)? Return ONLY JSON {"is_face": bool, "faces": number, "reason": string}'), max_tokens=120)
    second_look = False
    if faces is not None:
        faces = main_faces(faces)
        # The AI sees exactly one real face but the strict detector found none (typical of laptop webcams): give OpenCV's
        # other face model a second look. Without that clear AI answer the strict result stands — nothing is loosened.
        if not faces and ai is not None and ai.get("is_face") and _count(ai.get("faces"), 1) == 1:
            again = main_faces(await asyncio.to_thread(_opencv_faces, im, True) or [])
            if len(again) == 1:
                faces, second_look = again, True
    methods = {"opencv": None if faces is None else len(faces), "ocr_chars": text_len,
               "ai": None if ai is None else {"is_face": bool(ai.get("is_face")), "faces": ai.get("faces")}}
    if second_look:
        methods["opencv_second_look"] = True
    if text_len > 60:
        return {"ok": False, "reason": "looks_like_document", "methods": methods}
    if faces is not None and ai is not None:
        ok = len(faces) == 1 and bool(ai.get("is_face"))
    elif faces is not None:
        ok = len(faces) == 1
    elif ai is not None:
        ok = bool(ai.get("is_face")) and _count(ai.get("faces")) == 1
    else:
        return {"ok": settings.TEST_MODE, "reason": None if settings.TEST_MODE else "no_detector",
                "methods": methods, "unverified": True}
    reason = None
    if not ok:
        n = len(faces) if faces is not None else _count((ai or {}).get("faces"))
        reason = "no_face" if n == 0 else "multiple_faces" if n > 1 else "not_a_real_face"
    return {"ok": ok, "reason": reason, "methods": methods}
