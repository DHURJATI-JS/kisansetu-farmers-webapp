"""Guide, terms explainer and vernacular AI — end-to-end against a RUNNING server (TEST_MODE=true) with the AI gateway.

    python tests/e2e_guide.py

* The tour / glossary catalog is served per language (cacheable) and never leaks anything but public texts.
* Terms explainer: risky-but-legal clauses are always flagged (rules, curated words); AI points must quote the terms.
* Vernacular: a question in Tamil gets a Tamil answer, Hinglish gets Hinglish; the voice code follows the answer.
* The assistant may point only at screen parts the page reported (and that exist) — never at anything else.
"""
from __future__ import annotations

import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from e2e_flow import Client, finish, ok, reset_limits, signup, sql  # noqa: E402
from e2e_lifecycle import create_farm  # noqa: E402

RISKY = ("1. Workers get Rs 450 per day for 8 hours.\n2. A security deposit of Rs 2,000 is taken on joining.\n"
         "3. Salary is paid at the end of the season after harvest.\n4. The worker must submit original Aadhaar card to the office.\n"
         "5. Sunday is the weekly day off.")


def phone(prefix: str) -> str:
    return prefix + str(uuid.uuid4().int % 10**9).zfill(9)


def main() -> int:
    t0 = time.time()
    reset_limits()
    print("1. catalog per language (public texts only, cacheable)")
    c = Client("guest")
    c.c.headers["X-Forwarded-For"] = "10.86.0.1"
    for lang, title in (("en", "Your job"), ("hi", "आपकी नौकरी")):
        r = c.c.get("/api/guide/catalog", params={"lang": lang})
        d = r.json()
        ok(r.status_code == 200 and d["guide"]["my-job"]["title"] == title and len(d["gloss"]) >= 25,
           f"{lang}: tours + {len(d['gloss'])} glossary words", d.get("lang"))
        ok("public" in r.headers.get("cache-control", ""), f"{lang}: cacheable", r.headers.get("cache-control"))
    r = c.c.get("/api/guide/catalog", params={"lang": "../../etc"})
    ok(r.status_code == 200 and r.json()["lang"] in ("en", "hi"), "unknown language falls back safely")
    d = r.json()
    ok(set(d) == {"lang", "speech", "guide", "gloss"} and all(set(v) == {"title", "text"} for v in d["guide"].values())
       and all(set(g) == {"id", "terms", "word", "simple"} for g in d["gloss"]), "only guide / glossary texts are exposed")

    print("2. terms explainer: risky clauses flagged with the farm's own words")
    o = Client("owner")
    o.c.headers["X-Forwarded-For"] = "10.86.0.2"
    po = phone("9")
    signup(o, "owner", po, "Guide Owner", "OwnerGuide2026x", 12.97, 77.59, email=f"go{po[-5:]}@kisansetu.in", dob="1980-01-01")
    sql("UPDATE users SET photo_verified = true WHERE phone = :p", p="+91" + po)
    farm, _branch = create_farm(o)
    sql("UPDATE farms SET terms_mode = 'custom', terms_text = :t WHERE id = :f", t=RISKY, f=farm)
    c.refresh(f"/f/{farm}")
    r = c.post("/api/terms/explain", json={"farm_id": farm})
    d = r.json()
    ids = {x["id"] for x in d.get("risks", [])}
    ok(r.status_code == 200 and {"deposit", "wage_held", "documents"} <= ids, "deposit, pay held to harvest, documents flagged", ids)
    ok(all(x["quote"] in RISKY and x["why"] and x["label"] for x in d["risks"]), "each warning quotes the terms and explains simply")
    ok(all(re.sub(r"\s+", " ", p["quote"]).lower() in re.sub(r"\s+", " ", RISKY).lower() for p in d.get("points", [])),
       f"every AI point quotes the terms ({len(d.get('points', []))} points, ai={d.get('ai')})")
    r = c.post("/api/terms/explain", json={"farm_id": str(uuid.uuid4())})
    ok(r.status_code == 404, "unknown farm → 404")
    sql("UPDATE farms SET terms_mode = 'boilerplate', terms_text = NULL WHERE id = :f", f=farm)
    r = c.post("/api/terms/explain", json={"farm_id": farm})
    ok(r.status_code == 200 and not r.json()["risks"] and len(r.json()["points"]) == 8, "platform terms: 8 curated points, no risks")

    print("3. vernacular: answer in the language the person wrote in")
    w = Client("worker")
    w.c.headers["X-Forwarded-For"] = "10.86.0.3"
    signup(w, "farmer", phone("8"), "Guide Worker", "583921", 12.98, 77.60, dob="1996-01-01")
    for msg, want, script in (("எனக்கு அருகில் வேலை எப்படி கண்டுபிடிப்பது?", "ta", r"[஀-௿]"),
                              ("mujhe paas mein kaam kaise milega bhai?", "hi-Latn", r"[A-Za-z]"),
                              ("मुझे वेतन कब मिलेगा?", "hi", r"[ऀ-ॿ]")):
        t1 = time.time()
        r = w.post("/api/ai/chat", json={"message": msg, "session": "guide", "page_keys": ["my-job", "contacts", "report"]})
        d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        reply = d.get("reply", "")
        share = len(re.findall(script, reply)) / max(1, len(re.findall(r"\w", reply)))
        ok(r.status_code == 200 and d.get("lang") == want, f"{want}: detected and answered ({time.time() - t1:.0f}s, voice {d.get('speech')})",
           r.text[:200])
        ok(share > 0.5 or d.get("provider") is None, f"{want}: reply written in that script ({share:.0%})", reply[:120])
        ok(d.get("highlight") in (None, "my-job", "contacts", "report"), "highlight only ever a reported screen part", d.get("highlight"))
    r = w.post("/api/ai/chat", json={"message": "hi", "page_keys": ["<script>"]})
    ok(r.status_code in (400, 422), "screen-part keys are validated", r.status_code)
    return finish(t0)


if __name__ == "__main__":
    sys.exit(main())
