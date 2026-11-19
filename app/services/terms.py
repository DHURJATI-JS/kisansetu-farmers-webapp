"""Farm terms & conditions: fair boilerplate (fully translated) or custom terms reviewed by AI + rules.

Custom terms with arbitrary / unjust / unlawful clauses block farm creation until fixed.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import TermsReview

log = logging.getLogger("kisansetu.terms")

BOILERPLATE_VERSION = "boiler-v1"
BOILERPLATE_KEYS = [f"terms.boiler.{i}" for i in range(1, 15)]
MAX_CUSTOM_CHARS = 8000

RULES: list[tuple[str, str, str]] = [
    (r"\b(no|without)\s+(salary|wages?|pay|payment)\b", "unpaid_work", "Code on Wages, 2019"),
    (r"\b(forfeit\w*|withh?old\w*)\b.{0,40}\b(salary|wages?|pay)\b", "wage_withholding", "Code on Wages, 2019"),
    (r"\b(9|1[0-9]|2[0-4])\s*(hours|hrs|hour)\b", "excess_hours", "OSH Code 2020; platform max 8 h/day"),
    (r"\b(without|no)\s+(any\s+)?notice\b|\bterminat\w*\s+(at\s+any\s+time|immediately|anytime)\b", "no_notice", "Platform rule: 1-month notice"),
    (r"\b(child|children|minors?|under\s*(14|18)\s*(years)?)\b", "child_labour", "Child and Adolescent Labour (P&R) Act, 1986"),
    (r"\b(keep|retain|hold|deposit|surrender)\w*\b.{0,40}\b(aadhaa?r|id\s*cards?|identity|documents?|passports?|ration\s*cards?)\b", "id_retention", "Bonded Labour System (Abolition) Act, 1976"),
    (r"\b(bonded|cannot\s+leave|not\s+allowed\s+to\s+leave|must\s+work\s+off\s+(the\s+)?advance)\b", "bonded_labour", "Bonded Labour System (Abolition) Act, 1976"),
    (r"\b(fined?|fines|penalt(y|ies))\b", "fines", "Code on Wages, 2019"),
    (r"\bno\s+(drinking\s+)?(water|food|lunch|breaks?|rest|leave|holidays?)\b", "denial_of_essentials", "OSH Code 2020"),
    (r"\b(waive|give\s+up|surrender)\w*\b.{0,30}\b(rights?|claims?|complaints?)\b|\bnot\s+(allowed|permitted)\s+to\s+(complain|report)\b", "rights_waiver", "Platform grievance policy"),
    (r"\b(women|female|caste|religion|dalit|tribal)\b.{0,40}\b(less|lower|not\s+allowed|cannot|half)\b", "discrimination", "Code on Wages, 2019 (equal remuneration); Art. 15"),
    (r"(वेतन|मजदूरी|तनख्वाह)\s*(नहीं|काट)", "unpaid_work", "Code on Wages, 2019"),
    (r"बिना\s*(किसी\s*)?(नोटिस|सूचना)", "no_notice", "Platform rule: 1-month notice"),
    (r"(बच्चे|नाबालिग)", "child_labour", "Child and Adolescent Labour (P&R) Act, 1986"),
    (r"(आधार|पहचान\s*पत्र|दस्तावेज).{0,20}(जमा|रख)", "id_retention", "Bonded Labour System (Abolition) Act, 1976"),
    (r"(पानी|खाना|भोजन|आराम|छुट्टी).{0,12}नहीं", "denial_of_essentials", "OSH Code 2020"),
    (r"जुर्माना", "fines", "Code on Wages, 2019"),
]
_COMPILED = [(re.compile(p, re.I), k, law) for p, k, law in RULES]

# Never allowed on KisanSetu, whatever the AI says: money taken FROM workers to get, keep or leave a job (joining /
# recruitment fees, security deposits, bonds) — that is how debt bondage starts — ID documents kept by the farm, and
# advances to be "worked off". A sentence that says the opposite ("no joining fee", "we never keep your Aadhaar") or in
# which the farm is the one paying ("we pay a ₹500 joining bonus") is not flagged.
_WORKER = r"(you|workers?|employees?|labou?rers?|candidates?|applicants?|each\s+(?:worker|person|employee)|the\s+(?:worker|employee|labou?rer|candidate))"
_MUST = r"(must|need\s+to|needs\s+to|has\s+to|have\s+to|shall|should|will|are\s+required\s+to|is\s+required\s+to|required\s+to)"
_PAYV = r"(pay|give|deposit|submit|transfer|bring|hand\s+over|send)"
_FEE = "Bonded Labour System (Abolition) Act, 1976; platform rule: workers never pay to work"
STRONG_RULES: list[tuple[str, str, str]] = [
    (rf"\b{_WORKER}\b[^.;\n।]{{0,30}}\b{_MUST}\s+{_PAYV}\b", "fee_from_worker", _FEE),
    (r"\b(pay|give|deposit|submit)\w*\b[^.;\n।]{0,50}\bto\s+(join|start|work|be\s+hired|be\s+selected|get\s+(?:the|this|a)\s+job|register)\b",
     "fee_from_worker", _FEE),
    (r"\b(bring|transfer|send)\w*\b[^.;\n।]{0,40}(₹|\brs\b|rupees?|\blakhs?\b|\d)[^.;\n।]{0,30}\bto\s+(join|start|work|be\s+hired|register)\b",
     "fee_from_worker", _FEE),
    (r"\b(joining|registration|recruitment|placement|processing|training|agent|agency|application|selection|onboarding|entry|admission)"
     r"\s+(fees?|charges?|money|amount|cost|deposit)\b", "fee_from_worker", _FEE),
    (r"\b(security|caution|refundable|non[-\s]?refundable)\s+(deposit|money|amount)\b", "fee_from_worker", _FEE),
    (r"\b(deposit|bond|surety)\s+(of|amount|money|worth)\b[^.;\n।]{0,20}(₹|\brs\b|rupees?|\blakhs?\b|\d)", "fee_from_worker", _FEE),
    (r"(₹|\brs\b)\s*[\d,]+(\s*(lakhs?|thousand))?\s+(security\s+)?(deposit|bond|fees?)\b", "fee_from_worker", _FEE),
    (r"(जॉइन|ज्वाइन|नौकरी|काम|भर्ती)[^।.\n]{0,25}(के\s*लिए|से\s*पहले|पाने)[^।.\n]{0,30}(₹|रुपये|रुपए|रूपये|लाख|हज़ार|हजार|पैसे|फीस|शुल्क|जमा)",
     "fee_from_worker", _FEE),
    (r"(भर्ती|पंजीकरण|रजिस्ट्रेशन|जॉइनिंग|एजेंट|प्रशिक्षण)\s*(फीस|शुल्क|चार्ज)|(जमानत|ज़मानत|सिक्योरिटी)\s*(राशि|पैसे|डिपॉजिट|जमा)",
     "fee_from_worker", _FEE),
    (r"\b(join|kaam|naukri|bharti)\b[^.;\n]{0,25}\b(ke\s+liye|se\s+pehle|karne\s+ke\s+liye)\b[^.;\n]{0,30}"
     r"\b(paise|rupaye|rupees?|lakh|hazaa?r|fees?|jama|deposit)\b", "fee_from_worker", _FEE),
    (r"\b(keep|retain|hold|deposit|surrender|submit|hand\s+over)\w*\b.{0,40}\b(original\s+)?(aadhaa?r|id\s*cards?|identity\s+(?:cards?|documents?)|passports?|ration\s*cards?|bank\s*pass\s*books?|atm\s*cards?)\b",
     "id_retention", "Bonded Labour System (Abolition) Act, 1976"),
    (r"(आधार|पहचान\s*पत्र|पासबुक|एटीएम).{0,20}(जमा|रख|सौंप)", "id_retention", "Bonded Labour System (Abolition) Act, 1976"),
    (r"\b(must|has\s+to|have\s+to|will)\s+work\s+off\b|\bbonded\s+(labou?r|work)", "bonded_labour", "Bonded Labour System (Abolition) Act, 1976"),
]
_STRONG = [(re.compile(p, re.I), k, law) for p, k, law in STRONG_RULES]
_NEGATION = re.compile(r"\b(no|not|never|free|nothing|zero|nil|don'?t|doesn'?t|won'?t|neither|nor|nahi|nahin|muft)\b"
                       r"|नहीं|कभी\s*नहीं|मुफ़्त|मुफ्त|निःशुल्क", re.I)
_FARM_PAYS = re.compile(r"^\s*(we|the\s+(farm|employer|owner|company|management)|kisansetu|our\s+farm)\b[^.;\n]{0,25}"
                        r"\b(pay|give|provide|offer|deposit|transfer|credit)", re.I)


def _clauses(text: str) -> list[str]:
    norm = re.sub(r"\b(rs|inr)\.\s*", "rs ", text, flags=re.I)          # "Rs. 5,000" is not a sentence end
    return [c for c in re.split(r"[.!?।;\n]+", norm) if c.strip()]


def strong_flags(text: str) -> list[dict[str, str]]:
    """Clauses never allowed on KisanSetu (see STRONG_RULES) — they block the terms even when the AI calls them fine."""
    out, seen = [], set()
    for clause in _clauses(text or ""):
        if _NEGATION.search(clause) or _FARM_PAYS.search(clause):
            continue
        for rx, code, law in _STRONG:
            if rx.search(clause) and (code, clause) not in seen:
                seen.add((code, clause))
                out.append({"code": code, "clause": clause.strip()[:300], "law": law, "source": "rules"})
                break
    return out


def issue_text(issue: dict[str, Any], tr: Any) -> str:
    """A reviewer issue in the reader's language: rule issues by their code, AI issues as written."""
    code = issue.get("code")
    if issue.get("source") == "rules" and code:
        txt = tr(f"terms.rule.{code}")
        if txt != f"terms.rule.{code}":
            return txt
    return issue.get("problem") or code or ""


def terms_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text.strip()).encode()).hexdigest()


def terms_version_for(mode: str, text: str | None) -> str:
    return BOILERPLATE_VERSION if mode != "custom" or not text else "custom-" + terms_hash(text)[:16]


def rule_flags(text: str) -> list[dict[str, str]]:
    out, seen = [], set()
    for rx, key, law in _COMPILED:
        for m in rx.finditer(text):
            if key in seen:
                continue
            seen.add(key)
            s = max(0, m.start() - 40)
            out.append({"code": key, "clause": text[s : m.end() + 40].strip(), "law": law, "source": "rules"})
    return out


def _chunks(text: str, size: int = 2500) -> list[str]:
    paras, out, cur = re.split(r"\n\s*\n|\n", text), [], ""
    for p in paras:
        if len(cur) + len(p) > size and cur:
            out.append(cur)
            cur = ""
        cur += p + "\n"
    if cur.strip():
        out.append(cur)
    return out


async def _ai_review(text: str, hints: list[dict[str, str]]) -> list[dict[str, str]] | None:
    from app.services.ai.providers import router
    from app.services.ai.queue import PRIORITY_INTERACTIVE, ai_queue
    from app.utils import parse_json_loose

    system = (
        "You are an Indian labour-law compliance reviewer for farm employment terms. Flag ONLY clauses that are arbitrary, "
        "unjust or unlawful for workers in India: ANY money, fee, deposit, bond or commission the WORKER must pay to get, "
        "keep or leave the job (joining / recruitment / registration fees, security deposits); "
        "wages below minimum wage; withholding/forfeiting earned wages; fines or "
        "deductions beyond law; more than 8 working hours/day; no weekly rest; termination by employer without one month "
        "notice; bonded/forced labour or debt bondage; child labour; retaining ID documents; restricting movement; waiving "
        "legal or grievance rights; discrimination; no drinking water/food/breaks/safety gear; no extra pay for holiday work; "
        "penalties for complaints. Clauses that PROTECT workers are fine. Return ONLY JSON "
        '{"verdict":"ok"|"violations","issues":[{"clause":"short quote","problem":"why","law":"Act/rule"}]}'
    )
    issues: list[dict[str, str]] = []
    for chunk in _chunks(text):
        hint = f"Rule-based hints (may be false positives): {[h['code'] for h in hints]}\n" if hints else ""
        try:
            res = await ai_queue.run(lambda c=chunk: router.chat(
                [{"role": "user", "content": f"{hint}TERMS:\n<<<\n{c}\n>>>"}], system=system, max_tokens=600, temperature=0.0),
                priority=PRIORITY_INTERACTIVE, timeout=150)
        except Exception as exc:
            log.info("AI terms review unavailable: %s", exc)
            return None
        data = parse_json_loose(res.text)
        if not isinstance(data, dict):
            return None
        for it in data.get("issues") or []:
            if isinstance(it, dict) and it.get("clause"):
                issues.append({"clause": str(it.get("clause"))[:300], "problem": str(it.get("problem", ""))[:300],
                               "law": str(it.get("law", ""))[:160], "source": "ai"})
    return issues


async def review_terms(db: AsyncSession, text: str) -> dict[str, Any]:
    """Returns {"ok": bool, "issues": [...], "ai": bool}. Cached by text hash."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "issues": [{"code": "empty", "clause": "", "problem": "empty", "law": ""}], "ai": False}
    if len(text) > MAX_CUSTOM_CHARS:
        return {"ok": False, "issues": [{"code": "too_long", "clause": "", "problem": "too_long", "law": ""}], "ai": False}
    strong = strong_flags(text)        # always re-applied: a cached AI "ok" never hides money taken from workers
    h = terms_hash(text)
    cached = (await db.execute(select(TermsReview).where(TermsReview.text_hash == h))).scalar_one_or_none()
    if cached and cached.verdict in ("ok", "violations"):
        issues = strong + [i for i in (cached.issues or []) if i.get("source") != "rules"]
        return {"ok": not issues, "issues": issues, "ai": cached.provider == "ai", "cached": True}
    hints = rule_flags(text)
    ai_issues = await _ai_review(text, hints)
    if ai_issues is None:
        # AI unavailable: conservative — rule hits block; no hits pass only in TEST_MODE
        codes = {i["code"] for i in strong}
        issues = strong + [x for x in hints if x["code"] not in codes]
        ok = not issues and settings.TEST_MODE
        if not issues and not ok:
            issues = [{"code": "ai_unavailable", "clause": "", "problem": "ai_unavailable", "law": ""}]
        return {"ok": ok, "issues": issues, "ai": False}
    db.add(TermsReview(text_hash=h, verdict="ok" if not ai_issues else "violations", issues=ai_issues, provider="ai"))
    issues = strong + ai_issues
    return {"ok": not issues, "issues": issues, "ai": True}
