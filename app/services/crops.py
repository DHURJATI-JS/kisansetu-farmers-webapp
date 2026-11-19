"""Crop knowledge base (indicative agronomy values for Indian conditions) + rule-based recommendations.

Values are approximate guidance only — the UI labels them "indicative" and suggests confirming with the local
Krishi Vigyan Kendra (KVK).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class Crop:
    key: str
    name: str
    category: str
    seasons: tuple[str, ...]          # kharif | rabi | zaid | perennial
    tmin: float                       # tolerance band (°C)
    tmax: float
    opt_lo: float                     # optimal band
    opt_hi: float
    rain_lo: int                      # annual rainfall mm (or irrigation equivalent)
    rain_hi: int
    farming: tuple[str, ...]
    states: tuple[str, ...] = ()
    hi: str = ""                      # Hindi name (also helps the AI / search)


AP, AR, AS, BR, CG, GA, GJ, HR, HP, JH, KA, KL, MP, MH, MN, ML, MZ, NL, OD, PB, RJ, SK, TN, TG, TR, UP, UK, WB, JK = (
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
    "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
    "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura",
    "Uttar Pradesh", "Uttarakhand", "West Bengal", "Jammu and Kashmir")

CROPS: list[Crop] = [
    Crop("rice", "Rice (Paddy)", "cereal", ("kharif",), 15, 40, 20, 35, 1000, 2500, ("intensive", "organic"), (WB, UP, PB, AP, TG, OD, TN, BR, CG, AS), "धान"),
    Crop("wheat", "Wheat", "cereal", ("rabi",), 3, 32, 10, 25, 400, 1000, ("intensive", "organic"), (UP, PB, HR, MP, RJ, BR, GJ), "गेहूं"),
    Crop("maize", "Maize", "cereal", ("kharif", "rabi"), 10, 38, 21, 30, 500, 1000, ("intensive", "mixed"), (KA, MP, BR, TG, MH, RJ, UP), "मक्का"),
    Crop("bajra", "Pearl millet (Bajra)", "millet", ("kharif",), 15, 42, 25, 35, 250, 700, ("dryland", "extensive"), (RJ, GJ, HR, UP, MH), "बाजरा"),
    Crop("jowar", "Sorghum (Jowar)", "millet", ("kharif", "rabi"), 12, 40, 25, 32, 400, 1000, ("dryland", "mixed"), (MH, KA, MP, TG, RJ), "ज्वार"),
    Crop("ragi", "Finger millet (Ragi)", "millet", ("kharif",), 12, 35, 20, 30, 500, 1000, ("dryland", "organic"), (KA, TN, UK, OD), "रागी"),
    Crop("barley", "Barley", "cereal", ("rabi",), 3, 30, 12, 25, 300, 800, ("dryland", "extensive"), (RJ, UP, MP, HR), "जौ"),
    Crop("chickpea", "Chickpea (Gram)", "pulse", ("rabi",), 5, 32, 15, 25, 300, 700, ("dryland", "mixed"), (MP, MH, RJ, KA, UP), "चना"),
    Crop("tur", "Pigeon pea (Tur/Arhar)", "pulse", ("kharif",), 10, 38, 20, 30, 600, 1000, ("dryland", "mixed"), (MH, KA, MP, UP, GJ, TG), "अरहर"),
    Crop("moong", "Green gram (Moong)", "pulse", ("kharif", "zaid"), 15, 40, 25, 35, 400, 700, ("mixed", "organic"), (RJ, MH, KA, AP, OD), "मूंग"),
    Crop("urad", "Black gram (Urad)", "pulse", ("kharif",), 15, 40, 25, 35, 400, 800, ("mixed",), (MP, UP, MH, AP, TN), "उड़द"),
    Crop("lentil", "Lentil (Masoor)", "pulse", ("rabi",), 5, 30, 15, 25, 300, 500, ("dryland", "mixed"), (MP, UP, BR, WB), "मसूर"),
    Crop("groundnut", "Groundnut", "oilseed", ("kharif",), 15, 38, 25, 30, 500, 1250, ("intensive", "mixed"), (GJ, RJ, TN, AP, KA), "मूंगफली"),
    Crop("mustard", "Mustard / Rapeseed", "oilseed", ("rabi",), 3, 30, 15, 25, 250, 600, ("extensive", "mixed"), (RJ, HR, MP, UP, WB), "सरसों"),
    Crop("soybean", "Soybean", "oilseed", ("kharif",), 12, 36, 20, 30, 600, 1000, ("intensive",), (MP, MH, RJ, KA), "सोयाबीन"),
    Crop("sunflower", "Sunflower", "oilseed", ("rabi", "kharif"), 10, 35, 20, 25, 500, 700, ("mixed",), (KA, MH, AP), "सूरजमुखी"),
    Crop("sesame", "Sesame (Til)", "oilseed", ("kharif",), 15, 40, 25, 30, 300, 600, ("dryland",), (GJ, RJ, MP, WB, UP), "तिल"),
    Crop("cotton", "Cotton", "cash", ("kharif",), 15, 40, 21, 30, 500, 1000, ("intensive",), (GJ, MH, TG, RJ, HR, PB, KA), "कपास"),
    Crop("sugarcane", "Sugarcane", "cash", ("perennial",), 10, 42, 20, 35, 750, 1500, ("intensive", "plantation"), (UP, MH, KA, TN, BR), "गन्ना"),
    Crop("jute", "Jute", "cash", ("kharif",), 18, 40, 24, 35, 1200, 2000, ("intensive",), (WB, AS, BR, OD), "जूट"),
    Crop("tea", "Tea", "plantation", ("perennial",), 8, 35, 13, 30, 1500, 3000, ("plantation",), (AS, WB, KL, TN), "चाय"),
    Crop("coffee", "Coffee", "plantation", ("perennial",), 10, 32, 15, 28, 1500, 2500, ("plantation", "agroforestry"), (KA, KL, TN), "कॉफी"),
    Crop("rubber", "Rubber", "plantation", ("perennial",), 20, 38, 25, 34, 2000, 4000, ("plantation",), (KL, TR, KA), "रबर"),
    Crop("coconut", "Coconut", "plantation", ("perennial",), 20, 38, 27, 32, 1000, 2500, ("plantation", "agroforestry"), (KL, TN, KA, AP, GA), "नारियल"),
    Crop("arecanut", "Arecanut", "plantation", ("perennial",), 10, 38, 14, 36, 1500, 4500, ("plantation",), (KA, KL, AS), "सुपारी"),
    Crop("banana", "Banana", "fruit", ("perennial",), 13, 40, 20, 35, 1000, 2500, ("horticulture", "intensive"), (TN, MH, GJ, AP, KL), "केला"),
    Crop("mango", "Mango", "fruit", ("perennial",), 5, 45, 24, 30, 750, 2500, ("horticulture", "plantation"), (UP, AP, KA, BR, GJ, MH), "आम"),
    Crop("guava", "Guava", "fruit", ("perennial",), 5, 45, 23, 28, 1000, 2000, ("horticulture",), (UP, MP, MH, BR), "अमरूद"),
    Crop("papaya", "Papaya", "fruit", ("perennial",), 10, 38, 22, 26, 1500, 2000, ("horticulture",), (AP, GJ, KA, MP), "पपीता"),
    Crop("pomegranate", "Pomegranate", "fruit", ("perennial",), 5, 42, 25, 35, 500, 800, ("horticulture", "dryland"), (MH, KA, GJ), "अनार"),
    Crop("grapes", "Grapes", "fruit", ("perennial",), 5, 42, 15, 40, 500, 900, ("horticulture", "intensive"), (MH, KA), "अंगूर"),
    Crop("apple", "Apple", "fruit", ("perennial",), -10, 32, 21, 24, 1000, 1250, ("horticulture",), (JK, HP, UK), "सेब"),
    Crop("citrus", "Citrus (Orange/Lemon)", "fruit", ("perennial",), 5, 42, 13, 37, 750, 1200, ("horticulture",), (MH, MP, PB, AS), "संतरा/नींबू"),
    Crop("litchi", "Litchi", "fruit", ("perennial",), 5, 38, 20, 30, 1200, 1600, ("horticulture",), (BR, WB, UK), "लीची"),
    Crop("potato", "Potato", "vegetable", ("rabi",), 5, 30, 15, 20, 500, 700, ("intensive",), (UP, WB, BR, PB, GJ), "आलू"),
    Crop("onion", "Onion", "vegetable", ("rabi", "kharif"), 5, 35, 13, 25, 650, 750, ("intensive",), (MH, KA, MP, GJ, RJ), "प्याज"),
    Crop("tomato", "Tomato", "vegetable", ("rabi", "kharif", "zaid"), 10, 35, 20, 28, 600, 1500, ("intensive", "hydroponic"), (AP, KA, MP, OD, GJ), "टमाटर"),
    Crop("brinjal", "Brinjal", "vegetable", ("kharif", "rabi"), 13, 37, 21, 30, 600, 1000, ("intensive", "organic"), (WB, OD, GJ, BR), "बैंगन"),
    Crop("okra", "Okra (Bhindi)", "vegetable", ("kharif", "zaid"), 18, 40, 24, 28, 600, 1000, ("intensive", "organic"), (UP, GJ, BR, WB), "भिंडी"),
    Crop("cauliflower", "Cauliflower", "vegetable", ("rabi",), 5, 28, 15, 20, 500, 800, ("intensive",), (WB, BR, MP, OD, HR), "फूलगोभी"),
    Crop("cabbage", "Cabbage", "vegetable", ("rabi",), 5, 28, 15, 20, 500, 800, ("intensive",), (WB, OD, BR, KA), "पत्तागोभी"),
    Crop("chilli", "Chilli", "spice", ("kharif", "rabi"), 15, 35, 20, 30, 600, 1200, ("intensive",), (AP, TG, KA, MP), "मिर्च"),
    Crop("turmeric", "Turmeric", "spice", ("kharif",), 18, 35, 20, 30, 1500, 2250, ("intensive", "organic"), (TG, MH, TN, KA, OD), "हल्दी"),
    Crop("ginger", "Ginger", "spice", ("kharif",), 15, 32, 19, 28, 1500, 3000, ("organic",), (KL, KA, AS, ML, SK), "अदरक"),
    Crop("cumin", "Cumin (Jeera)", "spice", ("rabi",), 5, 30, 15, 25, 300, 500, ("dryland",), (RJ, GJ), "जीरा"),
    Crop("cardamom", "Cardamom", "spice", ("perennial",), 10, 35, 15, 30, 1500, 4000, ("plantation", "agroforestry"), (KL, KA, SK), "इलायची"),
    Crop("pepper", "Black pepper", "spice", ("perennial",), 10, 40, 23, 32, 2000, 3000, ("plantation", "agroforestry"), (KL, KA), "काली मिर्च"),
    Crop("cashew", "Cashew", "plantation", ("perennial",), 18, 40, 20, 35, 1000, 2000, ("plantation",), (MH, GA, KL, AP, OD), "काजू"),
    Crop("fodder", "Fodder (Berseem/Napier)", "fodder", ("rabi", "perennial"), 5, 38, 15, 30, 500, 1500, ("mixed", "intensive"), (PB, HR, UP, GJ), "चारा"),
    Crop("marigold", "Marigold", "flower", ("rabi", "kharif"), 10, 35, 18, 30, 600, 1000, ("horticulture",), (KA, TN, MH, WB), "गेंदा"),
]
BY_KEY = {c.key: c for c in CROPS}


def find_crop(name: str | None) -> Crop | None:
    if not name:
        return None
    n = name.strip().casefold()
    if n in BY_KEY:
        return BY_KEY[n]
    for c in CROPS:
        names = {c.name.casefold(), c.hi.casefold(), c.key}
        names |= {p.strip().casefold() for p in c.name.replace(")", "").split("(")}
        names |= {p.strip().casefold() for p in c.name.replace(")", "").replace("(", "/").split("/")}
        if n in names or any(n == x for x in names if x):
            return c
    for c in CROPS:
        if n in c.name.casefold() or (c.hi and c.hi in name):
            return c
    return None


def season_for(d: date) -> str:
    m = d.month
    if 6 <= m <= 10:
        return "kharif"
    if m >= 11 or m <= 2:
        return "rabi"
    return "zaid"


def crop_weather_warnings(crop: Crop, daily: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for d in daily:
        tmax, tmin, rain = d.get("tmax"), d.get("tmin"), d.get("rain") or 0
        if tmax is not None and tmax > crop.tmax:
            out.append({"date": d["date"], "kind": "too_hot", "value": tmax, "limit": crop.tmax})
        if tmin is not None and tmin < crop.tmin:
            out.append({"date": d["date"], "kind": "too_cold", "value": tmin, "limit": crop.tmin})
        if rain >= 115.6 and crop.rain_hi < 1500:
            out.append({"date": d["date"], "kind": "waterlogging_risk", "value": rain, "limit": 115.6})
    return out


def recommend(state: str | None, avg_temp: float | None, on: date, annual_rain_hint: int | None = None, top: int = 6) -> list[dict[str, Any]]:
    season = season_for(on)
    scored = []
    for c in CROPS:
        s = 0.0
        reasons = []
        if state and state in c.states:
            s += 2.5
            reasons.append("region")
        if season in c.seasons or "perennial" in c.seasons:
            s += 1.5 if season in c.seasons else 0.8
            reasons.append("season")
        if avg_temp is not None:
            if c.opt_lo <= avg_temp <= c.opt_hi:
                s += 2.0
                reasons.append("optimal_temp")
            elif c.tmin <= avg_temp <= c.tmax:
                s += 0.8
                reasons.append("tolerable_temp")
            else:
                s -= 3
        if annual_rain_hint is not None and c.rain_lo <= annual_rain_hint <= c.rain_hi:
            s += 1.0
            reasons.append("rainfall")
        scored.append((s, c, reasons))
    scored.sort(key=lambda x: -x[0])
    return [{"key": c.key, "name": c.name, "hi": c.hi, "category": c.category, "score": round(s, 2),
             "farming_types": list(c.farming), "reasons": r, "season": season} for s, c, r in scored[:top] if s > 0]


def farming_type_advice(recs: list[dict[str, Any]]) -> str:
    cats = [r["category"] for r in recs[:4]]
    if cats.count("plantation") >= 2:
        return "plantation"
    if cats.count("millet") + cats.count("pulse") >= 2:
        return "dryland"
    if cats.count("vegetable") + cats.count("fruit") >= 2:
        return "horticulture"
    if cats.count("cereal") >= 2:
        return "intensive"
    return "mixed"


def crop_kb_text(c: Crop) -> str:
    return (f"{c.name} ({c.hi}) — {c.category}; seasons: {', '.join(c.seasons)}; tolerates {c.tmin}–{c.tmax}°C, optimal "
            f"{c.opt_lo}–{c.opt_hi}°C; rainfall {c.rain_lo}–{c.rain_hi} mm; farming: {', '.join(c.farming)}; "
            f"major states: {', '.join(c.states)}.")


async def seed_crop_knowledge(db: AsyncSession) -> int:
    """Embed the crop knowledge base into pgvector once (used for RAG in the AI assistant)."""
    from app.models import KnowledgeChunk
    from app.services.ai.embeddings import embed

    have = set((await db.execute(select(KnowledgeChunk.ref_id).where(KnowledgeChunk.source == "crop"))).scalars())
    todo = [c for c in CROPS if c.key not in have]
    if not todo:
        return 0
    texts = [crop_kb_text(c) for c in todo]
    vecs = await embed(texts)
    for c, text, v in zip(todo, texts, vecs):
        db.add(KnowledgeChunk(source="crop", ref_id=c.key, content=text, embedding=v))
    return len(todo)
