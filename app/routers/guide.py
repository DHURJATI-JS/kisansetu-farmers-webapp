"""Guide endpoints: the tour / glossary catalog (cacheable) and the plain-language terms explainer."""
from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_user, get_db, lang_of
from app.errors import NotFound
from app.i18n import is_supported, t
from app.models import Farm, User
from app.security.ratelimit import limiter
from app.services.guide import boilerplate_points, catalog, explain_terms, speech_code
from app.services.settings_store import get_all

router = APIRouter()
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


@router.get("/api/guide/catalog")
@limiter.limit("60/minute")
async def guide_catalog(request: Request, lang: str = "en"):
    lang = lang if is_supported(lang) else lang_of(request)
    hit = _CACHE.get(lang)
    if not hit or time.monotonic() - hit[0] > 300:          # catalogs can be filled in at run time: refresh every 5 min
        hit = (time.monotonic(), catalog(lang))
        _CACHE[lang] = hit
    return JSONResponse(hit[1], headers={"Cache-Control": "public, max-age=3600"})


class ExplainIn(BaseModel):
    farm_id: uuid.UUID


@router.post("/api/terms/explain")
@limiter.limit("10/minute;300/day")   # cached per text + single-flight: the AI cost is bounded; many farmers share one mobile IP
async def terms_explain(request: Request, body: ExplainIn, user: User | None = Depends(current_user),
                        db: AsyncSession = Depends(get_db)):
    farm = await db.get(Farm, body.farm_id)
    if not farm or farm.status == "rejected":
        raise NotFound("errors.farm_not_found")
    lang = lang_of(request)
    custom = farm.terms_mode == "custom" and bool(farm.terms_text)
    text = farm.terms_text if custom else None
    cfg = await get_all(db)
    await db.commit()               # no DB connection is held while the AI reads (it can take a minute)
    if custom:
        res = await explain_terms(text, lang)
    else:
        res = {"points": boilerplate_points(lang, pct=cfg["holiday_premium_pct"], hours=cfg["max_work_hours"], days=cfg["notice_days"]),
               "ai": False, "risks": []}
    for r in res["risks"]:          # curated explanations — never AI text
        r.update(title=t(f"terms.risk.{r['id']}.title", lang), why=t(f"terms.risk.{r['id']}.why", lang),
                 label=t(f"terms.level.{r['level']}", lang))
    return {**res, "custom": custom, "speech": speech_code(lang)}
