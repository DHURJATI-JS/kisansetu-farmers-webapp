"""Pre-generate UI translations with the configured AI providers (resumable — safe to stop and restart).

    python scripts/build_translations.py --lang mr ta te       # specific languages
    python scripts/build_translations.py --all                  # every supported language (takes hours on a local model)
    python scripts/build_translations.py --status               # coverage report

Hand-written catalogs (app/i18n/locales/<code>.json) always win; AI output goes to locales/generated/<code>.json.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def run(langs: list[str], limit: int | None) -> None:
    from app.i18n import coverage, load_catalogs
    from app.services.i18n_ai import fill_catalog
    from app.services.redis_client import init_redis

    await init_redis()
    load_catalogs()
    for code in langs:
        t0 = time.time()
        n = await fill_catalog(code, full=True, limit=limit)
        print(f"{code}: +{n} keys in {time.time() - t0:.0f}s → {coverage(code)}% covered", flush=True)


def main() -> int:
    from app.i18n import LANGUAGES, coverage, load_catalogs

    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", nargs="*", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="max keys per language this run")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    codes = [l["code"] for l in LANGUAGES if l["code"] != "en"]
    if a.status:
        load_catalogs()
        for c in codes:
            print(f"{c:4} {coverage(c):5.1f}%")
        return 0
    langs = codes if a.all else [c for c in a.lang if c in codes]
    if not langs:
        ap.error("choose --lang <codes…> or --all")
    asyncio.run(run(langs, a.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
