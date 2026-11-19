"""Internationalisation for all 22 scheduled languages of India (+ English + widely spoken extras).

Every UI line is a catalog key. Lookup order: hand-written catalog → AI-generated catalog → English.
Missing keys are recorded and back-filled in the background by the AI translator (app/services/i18n_ai.py),
so every line eventually renders in the user's language.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

log = logging.getLogger("kisansetu.i18n")

LOCALES_DIR = Path(__file__).parent / "locales"
GENERATED_DIR = LOCALES_DIR / "generated"

# code, English name, native name, script/font family, direction, Web-Speech voice, ai_only (no hand catalog yet)
LANGUAGES: list[dict[str, Any]] = [
    {"code": "en", "name": "English", "native": "English", "font": "Noto Sans", "dir": "ltr", "speech": "en-IN"},
    {"code": "hi", "name": "Hindi", "native": "हिन्दी", "font": "Noto Sans Devanagari", "dir": "ltr", "speech": "hi-IN"},
    {"code": "bn", "name": "Bengali", "native": "বাংলা", "font": "Noto Sans Bengali", "dir": "ltr", "speech": "bn-IN"},
    {"code": "te", "name": "Telugu", "native": "తెలుగు", "font": "Noto Sans Telugu", "dir": "ltr", "speech": "te-IN"},
    {"code": "mr", "name": "Marathi", "native": "मराठी", "font": "Noto Sans Devanagari", "dir": "ltr", "speech": "mr-IN"},
    {"code": "ta", "name": "Tamil", "native": "தமிழ்", "font": "Noto Sans Tamil", "dir": "ltr", "speech": "ta-IN"},
    {"code": "ur", "name": "Urdu", "native": "اردو", "font": "Noto Nastaliq Urdu", "dir": "rtl", "speech": "ur-IN"},
    {"code": "gu", "name": "Gujarati", "native": "ગુજરાતી", "font": "Noto Sans Gujarati", "dir": "ltr", "speech": "gu-IN"},
    {"code": "kn", "name": "Kannada", "native": "ಕನ್ನಡ", "font": "Noto Sans Kannada", "dir": "ltr", "speech": "kn-IN"},
    {"code": "or", "name": "Odia", "native": "ଓଡ଼ିଆ", "font": "Noto Sans Oriya", "dir": "ltr", "speech": "or-IN"},
    {"code": "ml", "name": "Malayalam", "native": "മലയാളം", "font": "Noto Sans Malayalam", "dir": "ltr", "speech": "ml-IN"},
    {"code": "pa", "name": "Punjabi", "native": "ਪੰਜਾਬੀ", "font": "Noto Sans Gurmukhi", "dir": "ltr", "speech": "pa-IN"},
    {"code": "as", "name": "Assamese", "native": "অসমীয়া", "font": "Noto Sans Bengali", "dir": "ltr", "speech": "as-IN"},
    {"code": "mai", "name": "Maithili", "native": "मैथिली", "font": "Noto Sans Devanagari", "dir": "ltr", "speech": "hi-IN"},
    {"code": "sat", "name": "Santali", "native": "ᱥᱟᱱᱛᱟᱲᱤ", "font": "Noto Sans Ol Chiki", "dir": "ltr", "speech": "hi-IN"},
    {"code": "ks", "name": "Kashmiri", "native": "کٲشُر", "font": "Noto Nastaliq Urdu", "dir": "rtl", "speech": "ur-IN"},
    {"code": "ne", "name": "Nepali", "native": "नेपाली", "font": "Noto Sans Devanagari", "dir": "ltr", "speech": "ne-NP"},
    {"code": "sd", "name": "Sindhi", "native": "سنڌي", "font": "Noto Naskh Arabic", "dir": "rtl", "speech": "ur-IN"},
    {"code": "kok", "name": "Konkani", "native": "कोंकणी", "font": "Noto Sans Devanagari", "dir": "ltr", "speech": "mr-IN"},
    {"code": "doi", "name": "Dogri", "native": "डोगरी", "font": "Noto Sans Devanagari", "dir": "ltr", "speech": "hi-IN"},
    {"code": "mni", "name": "Manipuri (Meitei)", "native": "ꯃꯤꯇꯩꯂꯣꯟ", "font": "Noto Sans Meetei Mayek", "dir": "ltr", "speech": "bn-IN"},
    {"code": "brx", "name": "Bodo", "native": "बड़ो", "font": "Noto Sans Devanagari", "dir": "ltr", "speech": "hi-IN"},
    {"code": "sa", "name": "Sanskrit", "native": "संस्कृतम्", "font": "Noto Sans Devanagari", "dir": "ltr", "speech": "hi-IN"},
    # widely spoken non-scheduled languages (AI translated)
    {"code": "bho", "name": "Bhojpuri", "native": "भोजपुरी", "font": "Noto Sans Devanagari", "dir": "ltr", "speech": "hi-IN"},
    {"code": "raj", "name": "Rajasthani", "native": "राजस्थानी", "font": "Noto Sans Devanagari", "dir": "ltr", "speech": "hi-IN"},
    {"code": "hne", "name": "Chhattisgarhi", "native": "छत्तीसगढ़ी", "font": "Noto Sans Devanagari", "dir": "ltr", "speech": "hi-IN"},
    {"code": "tcy", "name": "Tulu", "native": "ತುಳು", "font": "Noto Sans Kannada", "dir": "ltr", "speech": "kn-IN"},
    {"code": "kha", "name": "Khasi", "native": "Ka Ktien Khasi", "font": "Noto Sans", "dir": "ltr", "speech": "en-IN"},
    {"code": "lus", "name": "Mizo", "native": "Mizo ṭawng", "font": "Noto Sans", "dir": "ltr", "speech": "en-IN"},
    {"code": "trp", "name": "Kokborok", "native": "Kók Borok", "font": "Noto Sans", "dir": "ltr", "speech": "en-IN"},
]
LANG_BY_CODE = {l["code"]: l for l in LANGUAGES}
DEFAULT_LANG = "en"

_catalogs: dict[str, dict[str, str]] = {}
_handwritten: dict[str, set[str]] = defaultdict(set)
_missing: dict[str, set[str]] = defaultdict(set)
_lock = threading.Lock()


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _read_json(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text("utf-8"))
        return {k: v for k, v in data.items() if isinstance(v, str)}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # malformed catalog must never take the site down
        log.error("bad catalog %s: %s", path, exc)
        return {}


# ------------------------------------------------------------------ quality gate for machine translations
_SCRIPT_BY_FONT = {"Noto Sans": "Latin", "Noto Sans Devanagari": "Devanagari", "Noto Sans Bengali": "Bengali",
                   "Noto Sans Telugu": "Telugu", "Noto Sans Tamil": "Tamil", "Noto Nastaliq Urdu": "Arabic",
                   "Noto Naskh Arabic": "Arabic", "Noto Sans Gujarati": "Gujarati", "Noto Sans Kannada": "Kannada",
                   "Noto Sans Oriya": "Oriya", "Noto Sans Malayalam": "Malayalam", "Noto Sans Gurmukhi": "Gurmukhi",
                   "Noto Sans Ol Chiki": "Ol Chiki", "Noto Sans Meetei Mayek": "Meetei Mayek"}
for _l in LANGUAGES:
    _l.setdefault("script", _SCRIPT_BY_FONT[_l["font"]])
_RANGES = (("Latin", 0x41, 0x5A), ("Latin", 0x61, 0x7A), ("Latin", 0xC0, 0x24F), ("Latin", 0x1E00, 0x1EFF),
           ("Devanagari", 0x900, 0x97F), ("Devanagari", 0xA8E0, 0xA8FF), ("Devanagari", 0x1CD0, 0x1CFF),
           ("Bengali", 0x980, 0x9FF), ("Gurmukhi", 0xA00, 0xA7F), ("Gujarati", 0xA80, 0xAFF), ("Oriya", 0xB00, 0xB7F),
           ("Tamil", 0xB80, 0xBFF), ("Telugu", 0xC00, 0xC7F), ("Kannada", 0xC80, 0xCFF), ("Malayalam", 0xD00, 0xD7F),
           ("Arabic", 0x600, 0x6FF), ("Arabic", 0x750, 0x77F), ("Arabic", 0x8A0, 0x8FF), ("Arabic", 0xFB50, 0xFDFF),
           ("Arabic", 0xFE70, 0xFEFF), ("Ol Chiki", 0x1C50, 0x1C7F), ("Meetei Mayek", 0xABC0, 0xABFF),
           ("Meetei Mayek", 0xAAE0, 0xAAFF))
_PH_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
# English words a translation may keep as they are (names of things people see printed in English)
_KEEP = frozenset({"kisansetu", "upi", "gst", "gstin", "otp", "pin", "pdf", "csv", "tds", "epf", "pan", "ifsc", "sms",
                   "whatsapp", "gps", "url", "totp", "aadhaar", "ok", "kg", "km", "app", "faq", "cod", "id", "neft", "imps"})


def _script(ch: str) -> str | None:
    """The script of a letter or vowel sign; None for digits, punctuation, symbols and spaces."""
    if not unicodedata.category(ch).startswith(("L", "M")):
        return None
    o = ord(ch)
    for name, lo, hi in _RANGES:
        if lo <= o <= hi:
            return name
    return "Other"


def translation_ok(lang: str, src: str, out: Any) -> bool:
    """Is `out` a usable machine translation of the English `src` into `lang`? Rejects wrong alphabets (e.g. Cyrillic or
    Hindi letters in Bengali), English handed back untranslated, changed {placeholders}, markup, line breaks and
    rambling. A rejected line simply stays in English — never shown half-wrong."""
    if not isinstance(out, str) or not isinstance(src, str):
        return False
    o = out.strip()
    if not o or len(o) > 3 * len(src) + 60 or "<" in o or "\n" in o or "`" in o:
        return False
    if sorted(_PH_RE.findall(o)) != sorted(_PH_RE.findall(src)):
        return False
    want = lang_meta(lang).get("script", "Latin")
    counts: dict[str, int] = defaultdict(int)
    for ch in _PH_RE.sub("", o):
        s = _script(ch)
        if s:
            counts[s] += 1
    words = [w for w in re.findall(r"[A-Za-z]{3,}", _PH_RE.sub("", src)) if w.casefold() not in _KEEP]
    if want == "Latin":
        if any(n for s, n in counts.items() if s != "Latin"):
            return False
        same = _PH_RE.sub("", o).strip().casefold() == _PH_RE.sub("", src).strip().casefold()
        return not (same and len(words) >= 3)
    if any(n for s, n in counts.items() if s not in (want, "Latin")):
        return False
    if not words:                       # nothing to translate ("KisanSetu", "UPI", "{n} km")
        return True
    return counts[want] >= max(2, counts["Latin"])


def _src_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def load_catalogs() -> None:
    with _lock:
        _catalogs.clear()
        _handwritten.clear()
        en = _read_json(LOCALES_DIR / "en.json")
        for lang in LANGUAGES:
            code = lang["code"]
            generated = _read_json(GENERATED_DIR / f"{code}.json")
            if code != DEFAULT_LANG and generated:
                # a machine translation is used only if it passes the quality gate AND its English source is unchanged
                srcs = _read_json(GENERATED_DIR / f"{code}.src.json")
                generated = {k: v for k, v in generated.items()
                             if k in en and translation_ok(code, en[k], v) and srcs.get(k, _src_hash(en[k])) == _src_hash(en[k])}
            hand = _read_json(LOCALES_DIR / f"{code}.json")
            merged = {**generated, **hand}
            _catalogs[code] = merged
            _handwritten[code] = set(hand)
    log.info("i18n: %d languages, %d english keys", len(_catalogs), len(_catalogs.get("en", {})))


def english() -> dict[str, str]:
    if not _catalogs:
        load_catalogs()
    return _catalogs.get("en", {})


def t(key: str, lang: str = DEFAULT_LANG, **params: Any) -> str:
    if not _catalogs:
        load_catalogs()
    cat = _catalogs.get(lang) or {}
    text = cat.get(key)
    if text is None:
        if lang != DEFAULT_LANG and key in _catalogs.get(DEFAULT_LANG, {}):
            _missing[lang].add(key)
        text = _catalogs.get(DEFAULT_LANG, {}).get(key, key)
    if params:
        try:
            return text.format_map(_SafeDict({k: ("" if v is None else v) for k, v in params.items()}))
        except Exception:
            fallback = _catalogs.get(DEFAULT_LANG, {}).get(key, key)
            try:
                return fallback.format_map(_SafeDict(params))
            except Exception:
                return fallback
    return text


def translator(lang: str):
    def _t(key: str, **params: Any) -> str:
        return t(key, lang, **params)

    return _t


def js_catalog(lang: str) -> dict[str, str]:
    en = english()
    cat = _catalogs.get(lang, {})
    return {k: cat.get(k, v) for k, v in en.items() if k.startswith("js.")}


def lang_meta(code: str) -> dict[str, Any]:
    return LANG_BY_CODE.get(code, LANG_BY_CODE[DEFAULT_LANG])


def is_supported(code: str | None) -> bool:
    return bool(code) and code in LANG_BY_CODE


def negotiate(accept_language: str | None) -> str:
    if not accept_language:
        return DEFAULT_LANG
    for part in accept_language.split(","):
        code = part.split(";")[0].strip().lower()
        base = code.split("-")[0]
        if code in LANG_BY_CODE:
            return code
        if base in LANG_BY_CODE:
            return base
    return DEFAULT_LANG


def missing_keys(lang: str, full: bool = False) -> list[str]:
    """Keys not present for `lang`. full=True scans the whole English catalog."""
    if lang == DEFAULT_LANG:
        return []
    if full:
        cat = _catalogs.get(lang, {})
        return [k for k in english() if k not in cat]
    return sorted(_missing.get(lang, set()))


def apply_generated(lang: str, mapping: dict[str, str]) -> None:
    """Use AI translations right away (in memory, never overriding hand-written ones). Call on the event loop."""
    cat = _catalogs.setdefault(lang, {})
    en = english()
    for k, v in mapping.items():
        if k not in _handwritten.get(lang, set()) and k in en and translation_ok(lang, en[k], v):
            cat[k] = v
    _missing[lang].difference_update(mapping.keys())


def read_generated(lang: str) -> dict[str, str]:
    """The usable machine translations on disk for `lang`: past the quality gate, English source unchanged."""
    en = english()
    srcs = _read_json(GENERATED_DIR / f"{lang}.src.json")
    return {k: v for k, v in _read_json(GENERATED_DIR / f"{lang}.json").items()
            if k in en and translation_ok(lang, en[k], v) and srcs.get(k, _src_hash(en[k])) == _src_hash(en[k])}


def persist_generated(lang: str, mapping: dict[str, str]) -> None:
    """Write AI translations to locales/generated/<lang>.json — blocking file I/O, so run it in a worker thread."""
    if not mapping:
        return
    en = english()
    mapping = {k: v for k, v in mapping.items() if k in en and translation_ok(lang, en[k], v)}
    if not mapping:
        return
    with _lock:
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        for path, data in ((GENERATED_DIR / f"{lang}.json", mapping),
                           (GENERATED_DIR / f"{lang}.src.json", {k: _src_hash(en[k]) for k in mapping})):
            existing = _read_json(path)       # .src.json: which English text each translation was made from
            existing.update(data)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")
            tmp.replace(path)


def merge_generated(lang: str, mapping: dict[str, str]) -> None:
    """Store AI translations and persist them (scripts / sync callers)."""
    if not mapping:
        return
    persist_generated(lang, mapping)
    apply_generated(lang, mapping)


def coverage(lang: str) -> float:
    en = english()
    if not en:
        return 0.0
    cat = _catalogs.get(lang, {})
    return round(100.0 * sum(1 for k in en if k in cat) / len(en), 1)


def fonts_href(lang: str) -> str:
    fam = lang_meta(lang)["font"]
    families = ["Cinzel:wght@500;700", "Noto+Sans:wght@400;500;600;700"]
    if fam != "Noto Sans":
        families.append(fam.replace(" ", "+") + ":wght@400;600;700")
    return "https://fonts.googleapis.com/css2?" + "&".join(f"family={f}" for f in families) + "&display=swap"
