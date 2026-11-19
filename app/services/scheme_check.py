"""Does the scheme still exist, and did its terms change? — read from the OFFICIAL page itself.

Every day, with the link-health probe (same download, same SSRF-safe fetch), the page's visible text is reduced to
  * a fingerprint (bottom-k sketch of 5-word shingles) → similarity with the version an admin last REVIEWED;
  * its key sentences (amounts, dates, eligibility, deadlines) → exactly what was added / removed since then;
  * whether the scheme's name is still there, and whether the page says it ended, closed or merged.
Outcome: ok | changed | may_have_ended | unreadable (JavaScript-only pages cannot be read without a browser).

Nothing is changed automatically. People see "the official page changed on <date> — check it before applying";
admins see the sentences, and optionally the AI's reading of them, where every point must quote the page word for word
and every number it mentions must be inside that quote — anything else is dropped as a possible hallucination.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import timedelta
from html.parser import HTMLParser
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GovScheme, SchemeSnapshot
from app.utils import now_utc

log = logging.getLogger("kisansetu.schemes.check")

K = 128                     # sketch size
CHANGED_BELOW = 0.80        # similarity with the reviewed version below this → "changed"
MIN_TEXT = 300              # fewer visible characters → the page needs JavaScript (unreadable here)
KEEP_SNAPSHOTS = 12
ENDED = ("scheme has been discontinued", "scheme is discontinued", "has been discontinued", "scheme has been closed",
         "scheme is closed", "scheme has ended", "has been merged", "merged with", "no longer accepting", "scheme stands closed",
         "has been withdrawn", "scheme is withdrawn", "योजना बंद", "योजना समाप्त", "बंद कर दी गई", "समाप्त कर दी गई")


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "object"}
    BLOCK = {"p", "div", "li", "td", "th", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
             "footer", "nav", "ul", "ol", "table", "option", "label", "title", "dt", "dd", "blockquote", "main", "aside"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def page_text(html: str) -> str:
    """Visible text only (no scripts / styles), one block per line, whitespace collapsed."""
    p = _Text()
    try:
        p.feed(html[:400_000])
        p.close()
    except Exception:  # broken markup: keep what was read
        pass
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in "".join(p.parts).split("\n"))
    return "\n".join(ln for ln in lines if ln)[:80_000]


_KEY = re.compile(r"(₹|\brs\.?\s?\d|rupee|\d{1,3}(,\d{2,3})+|\d+\s?(%|percent|per cent|hectare|acre|years?|months?|days?|lakh|crore)|"
                  r"eligib|benefit|instal+ment|premium|subsidy|pension|apply|application|last date|deadline|closed|discontinu|"
                  r"merged|extended|revised|amended|w\.e\.f|with effect|notified|age limit|income|पात्र|लाभ|किस्त|आवेदन|अंतिम तिथि)",
                  re.I)


def key_sentences(text: str, limit: int = 120) -> list[str]:
    """Sentences about money, dates, eligibility or deadlines — what applicants care about."""
    out, seen = [], set()
    for line in text.split("\n"):
        for s in re.split(r"(?<=[.!?।])\s+", line):
            s = s.strip()
            if 25 <= len(s) <= 400 and _KEY.search(s):
                k = re.sub(r"\W+", " ", s.casefold()).strip()
                if k not in seen:
                    seen.add(k)
                    out.append(s)
                    if len(out) >= limit:
                        return out
    return out


def sketch(text: str) -> list[int]:
    words = re.findall(r"\w+", text.casefold())
    if len(words) < 5:
        return []
    hs = {int.from_bytes(hashlib.blake2b(" ".join(words[i:i + 5]).encode(), digest_size=6).digest(), "big")
          for i in range(len(words) - 4)}
    return sorted(hs)[:K]


def similarity(a: list[int] | None, b: list[int] | None) -> float | None:
    """Estimated Jaccard similarity of two pages from their bottom-k sketches."""
    if not a or not b:
        return None
    sa, sb = set(a), set(b)
    union_k = sorted(sa | sb)[:K]
    return round(sum(1 for h in union_k if h in sa and h in sb) / len(union_k), 3)


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", " ", s.casefold()).strip()


def name_found(names: list[str], text: str) -> bool:
    low = f" {_norm(text)} "
    for n in names:
        base = re.sub(r"\(.*?\)", " ", n)                 # "(crop insurance)" is our gloss, not the official name
        nn = _norm(base)
        if nn and f" {nn} " in low:
            return True
        toks = [w for w in nn.split() if len(w) > 3]
        if toks and sum(1 for w in toks if f" {w} " in low) / len(toks) >= 0.75:
            return True
    return False


def ended_hint(text: str) -> str | None:
    low = text.casefold()
    return next((h for h in ENDED if h in low), None)


def _diff(base: list[str], cur: list[str]) -> tuple[list[str], list[str]]:
    b, c = {_norm(s): s for s in base}, {_norm(s): s for s in cur}
    return [c[k] for k in c if k not in b][:25], [b[k] for k in b if k not in c][:25]


async def cross_check(db: AsyncSession, s: GovScheme, *, names: list[str], final_url: str | None, html: str | None = None,
                      text: str | None = None, via: str = "fetch") -> dict[str, Any]:
    """Store a snapshot when the page text changed and update s.official_check. Returns the new check record.
    `text` comes from scripts/render_scheme_pages.py (JavaScript-only portals rendered OUTSIDE the app, via='browser')."""
    prev = dict(s.official_check or {})
    now = now_utc()
    text = text if text is not None else page_text(html or "")
    rec: dict[str, Any] = {k: prev[k] for k in ("baseline_id", "changed_at", "name_seen", "ai") if k in prev}
    rec["checked_at"], rec["via"] = now.isoformat(), via
    if via == "fetch" and prev.get("via") == "browser" and prev.get("checked_at", "") >= (now - timedelta(days=3)).isoformat()             and (len(text) < MIN_TEXT or len(text) < 0.5 * int(prev.get("chars") or 0)):
        return prev                         # a recent browser-rendered reading beats a JavaScript-less shell of the page
    if len(text) < MIN_TEXT:
        rec.update(status="unreadable", chars=len(text))
        s.official_check = rec
        return rec
    digest = hashlib.sha256(text.encode()).hexdigest()
    sk, keys = sketch(text), key_sentences(text)
    found, hint = name_found(names, text), ended_hint(text)
    latest = (await db.execute(select(SchemeSnapshot).where(SchemeSnapshot.scheme_id == s.id)
                               .order_by(SchemeSnapshot.fetched_at.desc()).limit(1))).scalar_one_or_none()
    snap = latest
    if latest is None or latest.text_hash != digest:
        snap = SchemeSnapshot(scheme_id=s.id, url=s.url, final_url=(final_url or s.url)[:500], text_hash=digest, chars=len(text),
                              sketch=sk, key_sentences=keys, name_found=found, ended_hint=hint, fetched_at=now)
        db.add(snap)
        await db.flush()
    baseline = await db.get(SchemeSnapshot, prev["baseline_id"]) if prev.get("baseline_id") else None
    if baseline is None:                      # first readable version = the reference until an admin reviews a newer one
        baseline = snap
        rec["baseline_id"] = str(snap.id)
    sim = similarity(baseline.sketch, sk) if baseline.id != snap.id else 1.0
    added, removed = _diff(baseline.key_sentences or [], keys)
    rec["name_seen"] = bool(rec.get("name_seen")) or found or bool(baseline.name_found)
    if hint or (rec["name_seen"] and not found and baseline.name_found):
        status = "may_have_ended"
    elif sim is not None and sim < CHANGED_BELOW:
        status = "changed"
    else:
        status = "ok"
    if status != "ok" and prev.get("status") != status:
        rec["changed_at"] = now.isoformat()
    if status == "ok":
        rec.pop("changed_at", None)
        rec.pop("ai", None)
    rec.update(status=status, similarity=sim, name_found=found, hint=hint, added=added, removed=removed, snapshot_id=str(snap.id),
               chars=len(text))
    s.official_check = rec
    # keep the history short — but never drop the reviewed baseline
    old = (await db.execute(select(SchemeSnapshot.id).where(SchemeSnapshot.scheme_id == s.id)
                            .order_by(SchemeSnapshot.fetched_at.desc()).offset(KEEP_SNAPSHOTS))).scalars().all()
    drop = [i for i in old if str(i) != rec.get("baseline_id")]
    if drop:
        await db.execute(delete(SchemeSnapshot).where(SchemeSnapshot.id.in_(drop)))
    return rec


def accept_current(s: GovScheme) -> None:
    """An admin reviewed the scheme against today's official page: that page becomes the new reference."""
    rec = dict(s.official_check or {})
    if rec.get("snapshot_id"):
        rec["baseline_id"] = rec["snapshot_id"]
    for k in ("changed_at", "ai", "added", "removed"):
        rec.pop(k, None)
    if rec.get("status") in ("changed", "may_have_ended"):
        rec["status"] = "ok"
    rec["reviewed_at"] = now_utc().isoformat()
    s.official_check = rec


_NUM = re.compile(r"\d[\d,.]*")


def _grounded(claim: str, quote: str) -> bool:
    """Every number in the claim must appear in its quote (₹6,000 ≠ ₹8,000 — invented figures are dropped)."""
    q = quote.replace(",", "")
    return all(n.replace(",", "").rstrip(".") in q for n in _NUM.findall(claim))


async def ai_reading(name: str, rules: str, added: list[str], removed: list[str]) -> dict[str, Any] | None:
    """The AI's plain-language reading of what changed — validated against the page text, or nothing."""
    if not added and not removed:
        return None
    from app.services.ai.providers import router
    from app.utils import parse_json_loose

    corpus = "\n".join(added + removed)
    system = ("You compare an Indian government scheme's official web page with its earlier version. Use ONLY the sentences "
              "given. For each change that matters to an applicant, quote the exact words from those sentences. Do not add "
              "facts, numbers or dates that are not in the quotes. Return ONLY JSON: "
              '{"summary":"one or two plain sentences","points":[{"change":"what changed for applicants","quote":"exact words"}]}')
    user = (f"Scheme: {name}\nOur current summary of who can apply: {rules}\n"
            f"ADDED on the official page:\n" + "\n".join(f"+ {x}" for x in added[:15]) +
            "\nREMOVED from the official page:\n" + "\n".join(f"- {x}" for x in removed[:15]))
    try:
        res = await router.chat([{"role": "user", "content": user[:5000]}], system=system, max_tokens=400, temperature=0.0, timeout=90)
    except Exception as exc:
        log.info("AI reading unavailable: %s", exc)
        return None
    data = parse_json_loose(res.text) or {}
    low = _norm(corpus)
    points = []
    for p in (data.get("points") or [])[:8]:
        if not isinstance(p, dict):
            continue
        change, quote = str(p.get("change", ""))[:240], str(p.get("quote", ""))[:400]
        if len(_norm(quote)) >= 8 and _norm(quote) in low and _grounded(change, quote):
            points.append({"change": change, "quote": quote})
    summary = str(data.get("summary", ""))[:300]
    if not _grounded(summary, corpus):
        summary = ""
    if not points and not summary:
        return None
    return {"summary": summary, "points": points, "dropped": max(0, len(data.get("points") or []) - len(points)),
            "at": now_utc().isoformat()}
