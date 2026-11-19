"""AI translation: back-fills missing catalog keys for every language and translates dynamic text (cached in Postgres)."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import SessionLocal
from app.i18n import apply_generated, english, lang_meta, missing_keys, persist_generated, translation_ok
from app.models import TranslationCache
from app.utils import parse_json_loose

log = logging.getLogger("kisansetu.i18n_ai")
_PH = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
_filling: set[str] = set()
BATCH = 15  # small batches keep each call well inside an 8k-context local model's limits
BG_BATCH = 8  # background fill: ~25 s per call on a local model, so a person who asks meanwhile waits little


def _placeholders(s: str) -> set[str]:
    return set(_PH.findall(s or ""))


def _system(lang: str) -> str:
    m = lang_meta(lang)
    return (f"Translate the JSON values from English to {m['name']} ({m['native']}) for KisanSetu, an Indian app for farm workers "
            "and farm owners, many with low literacy. Use simple, everyday, respectful words in the native script. Keep "
            "placeholders like {name} EXACTLY as they are, keep 'KisanSetu', numbers, ₹, URLs and emojis unchanged. "
            "Return ONLY a JSON object with exactly the same keys.")


async def _translate_mapping(lang: str, mapping: dict[str, str]) -> dict[str, str]:
    from app.services.ai.providers import router
    from app.services.ai.queue import PRIORITY_BACKGROUND, ai_queue
    import json

    res = await ai_queue.run(lambda: router.chat([{"role": "user", "content": json.dumps(mapping, ensure_ascii=False)}],
                                                 system=_system(lang), max_tokens=2500, temperature=0.1, timeout=420),
                             priority=PRIORITY_BACKGROUND, timeout=900)
    data = parse_json_loose(res.text)
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, src in mapping.items():
        v = data.get(k)
        # wrong alphabet, English handed back, changed {placeholders}, markup or rambling: dropped (stays English)
        if isinstance(v, str) and translation_ok(lang, src, v):
            out[k] = v.strip()
    return out


async def fill_catalog(lang: str, *, full: bool = True, limit: int | None = None) -> int:
    """Translate missing UI keys for `lang` and persist them to locales/generated/<lang>.json."""
    if lang == "en" or lang in _filling:
        return 0
    from app.services.redis_client import distributed_lock

    _filling.add(lang)
    done = 0
    try:
        async with distributed_lock(f"i18n-fill:{lang}", 3 * 3600) as mine:
            if not mine:          # another worker process is already translating this language
                return 0
            done = await _fill(lang, full, limit)
    finally:
        _filling.discard(lang)
    if done:
        log.info("i18n: AI-translated %d keys for %s", done, lang)
    return done


async def _fill(lang: str, full: bool, limit: int | None) -> int:
    done = 0
    en = english()
    keys = missing_keys(lang, full=full)
    if limit:
        keys = keys[:limit]
    for i in range(0, len(keys), BATCH):
        chunk = {k: en[k] for k in keys[i : i + BATCH] if k in en}
        try:
            got = await _translate_mapping(lang, chunk)
        except Exception as exc:
            log.warning("catalog fill %s failed: %s", lang, exc)
            break
        apply_generated(lang, got)
        await asyncio.to_thread(persist_generated, lang, got)  # file I/O off the event loop
        done += len(got)
    return done


_seen_mtime: dict[str, float] = {}
_checked_at: dict[str, float] = {}


async def _reload_if_changed(lang: str) -> None:
    """Other worker processes translate too: pick up their locales/generated/<lang>.json (at most every 30 s)."""
    import time

    from app.i18n import GENERATED_DIR, read_generated

    now = time.monotonic()
    if now - _checked_at.get(lang, 0.0) < 30:
        return
    _checked_at[lang] = now
    path = GENERATED_DIR / f"{lang}.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return
    if mtime <= _seen_mtime.get(lang, 0.0):
        return
    _seen_mtime[lang] = mtime
    data = await asyncio.to_thread(read_generated, lang)
    if data:
        apply_generated(lang, data)


# What everyone sees first: menus, buttons, sign-up / login, dashboard, jobs, onboarding, errors, page scripts. The
# background job fills these for every language in use before the rest of the catalog (admin pages, load tests…).
CORE_PREFIXES = ("nav.", "common.", "footer.", "js.", "signup.", "login.", "forgot.", "otp.", "home.", "dash.", "jobs.",
                 "job.", "onboard.", "account.", "field.", "help.", "errors.", "msg.", "role.", "status.", "loc.", "camera.",
                 "state.", "ai.", "guide.", "gloss.", "meta.", "banner.", "a11y.", "offline.", "logout.", "unit.", "cat.",
                 "day.", "gender.", "salary.", "farming.", "warn.", "sum.", "action.", "shop.", "cart.", "order.", "payment.",
                 "survey.", "report.", "schemes.", "scheme.", "community.", "weather.", "legal.", "policies.", "terms.")
# What a newcomer sees on the first visit (menus, home page, sign-up, dashboard, jobs, helper, page scripts): translated
# into EVERY supported language, so picking any language in the menu shows that language — not only the languages that
# registered people already use.
FIRST_VISIT_PREFIXES = ("nav.", "common.", "footer.", "home.", "signup.", "login.", "forgot.", "otp.", "meta.", "banner.",
                        "a11y.", "offline.", "loc.", "state.", "role.", "field.", "help.", "js.", "dash.", "onboard.", "ai.",
                        "guide.", "jobs.", "job.", "status.", "camera.", "logout.", "owner_up.")
_FAILED_TTL = 7 * 86400


def ordered_missing(lang: str) -> list[str]:
    """Missing keys for `lang`, the ones everyone sees first."""
    keys = missing_keys(lang, full=True)
    rank = {p: i for i, p in enumerate(CORE_PREFIXES)}
    return sorted(keys, key=lambda k: (rank.get(k.split(".")[0] + ".", len(CORE_PREFIXES)), k))


async def fill_batch(lang: str, prefixes: tuple[str, ...] | None = None) -> int | None:
    """Translate the next batch of missing keys for `lang` (core first). None = nothing left to try (or another process
    is translating this language right now). Keys the AI keeps getting wrong are set aside for a week."""
    from app.services.redis_client import distributed_lock, kv

    if lang == "en" or lang in _filling:
        return None
    failed_key = f"ks:i18n:failed:{lang}"
    try:
        failed = set(await kv().smembers(failed_key) or ())
    except Exception:  # noqa: BLE001
        failed = set()
    en = english()
    keys = [k for k in ordered_missing(lang) if k not in failed and (not prefixes or k.startswith(prefixes))][:BG_BATCH]
    if not keys:
        return None
    _filling.add(lang)
    try:
        async with distributed_lock(f"i18n-fill:{lang}", 900) as mine:
            if not mine:
                return None
            try:
                got = await _translate_mapping(lang, {k: en[k] for k in keys})
            except Exception as exc:  # noqa: BLE001 - gateway down / timeout: try this language again next run
                log.info("catalog fill %s paused: %s", lang, exc)
                return None
            apply_generated(lang, got)
            await asyncio.to_thread(persist_generated, lang, got)
    finally:
        _filling.discard(lang)
    rejected = [k for k in keys if k not in got]
    if rejected:
        try:
            await kv().sadd(failed_key, *rejected)
            await kv().expire(failed_key, _FAILED_TTL)
        except Exception:  # noqa: BLE001
            pass
    return len(got)


def schedule_fill(lang: str) -> None:
    if lang == "en":
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_reload_if_changed(lang))
    if lang in _filling or not missing_keys(lang):
        return
    loop.create_task(fill_catalog(lang, full=False))


def _hash(lang: str, text: str) -> str:
    return hashlib.sha256(f"{lang}\x00{text}".encode()).hexdigest()


_pending: dict[str, asyncio.Task] = {}   # source hash → background translation already running for it
_client_pending: dict[str, int] = {}     # who asked → how many of those are theirs
FRESH_PER_REQUEST = 12                   # new texts one request may queue; the rest follow on the next view
# Background AI work runs one job at a time (ai_queue's background lane): a flood of new texts must not become an endless
# backlog in front of everybody else's translations — beyond these bounds a text simply stays English until a later view.
MAX_PENDING = 240                        # per worker process
MAX_PENDING_PER_CLIENT = 24              # per address / account


def _job_done(h: str, client: str | None) -> None:
    _pending.pop(h, None)
    if client:
        n = _client_pending.get(client, 0) - 1
        if n > 0:
            _client_pending[client] = n
        else:
            _client_pending.pop(client, None)


async def _translate_and_store(lang: str, items: dict[str, tuple[str, str]]) -> dict[str, str]:
    """items: hash → (key, text). Translates via the AI queue, stores in the cache. Holds no DB connection while waiting."""
    try:
        got = await _translate_mapping(lang, {k: t for k, t in items.values()})
    except Exception as exc:
        log.info("dynamic translation unavailable: %s", exc)
        return {}
    by_key = {k: h for h, (k, _t) in items.items()}
    out = {by_key[k]: v for k, v in got.items() if k in by_key}
    if out:
        async with SessionLocal() as db:
            await db.execute(pg_insert(TranslationCache).values([
                dict(lang=lang, source_hash=h, source_text=items[h][1], translated=v, provider="ai") for h, v in out.items()])
                .on_conflict_do_nothing(index_elements=["lang", "source_hash"]))
            await db.commit()
    return out


async def translate_texts(texts: list[str], lang: str, wait: float = 3.0, client: str | None = None) -> list[str]:
    """Translate arbitrary (user / AI generated) text, cached forever in Postgres.

    Cache hits return at once. Misses start ONE background AI job per text (deduplicated across requests) and this
    call waits at most `wait` seconds — slower translations still land in the cache for the next view, so a busy AI
    never ties up the request or a database connection."""
    texts = [t[:2000] for t in texts]
    if lang == "en" and all(t.isascii() for t in texts):
        return texts
    hashes = [_hash(lang, t) for t in texts]
    async with SessionLocal() as db:
        cached = dict((await db.execute(select(TranslationCache.source_hash, TranslationCache.translated).where(
            TranslationCache.lang == lang, TranslationCache.source_hash.in_(hashes)))).all())
    missing = {h: t for t, h in zip(texts, hashes) if h not in cached and t.strip()}
    if missing:
        # ONE model call per text: texts from different people never share a prompt, so text sent alongside another
        # (by anyone, without signing in) can never steer how that other text is translated and cached for everybody
        room = min(FRESH_PER_REQUEST, MAX_PENDING - len(_pending),
                   MAX_PENDING_PER_CLIENT - _client_pending.get(client, 0) if client else FRESH_PER_REQUEST)
        fresh = [(h, t) for h, t in missing.items() if h not in _pending][:max(0, room)]
        loop = asyncio.get_running_loop()
        for h, t in fresh:
            task = loop.create_task(_translate_and_store(lang, {h: ("t0", t)}))
            _pending[h] = task
            if client:
                _client_pending[client] = _client_pending.get(client, 0) + 1
            task.add_done_callback(lambda _t, h=h, c=client: _job_done(h, c))
        tasks = {_pending[h] for h in missing if h in _pending}
        if tasks and wait > 0:
            done, _ = await asyncio.wait({asyncio.shield(t) for t in tasks}, timeout=wait)
            for d in done:
                if not d.cancelled() and d.exception() is None:
                    cached.update(d.result())
    return [cached.get(h, t) for t, h in zip(texts, hashes)]


def stats() -> dict[str, Any]:
    from app.i18n import LANGUAGES, coverage

    return {l["code"]: coverage(l["code"]) for l in LANGUAGES}
