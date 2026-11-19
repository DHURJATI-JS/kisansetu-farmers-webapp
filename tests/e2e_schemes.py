"""Government schemes are not static — end-to-end against a RUNNING server (TEST_MODE=true):
superadmin adds a state scheme, people who opened it are told when it is updated / withdrawn, eligibility follows the
change immediately, unofficial links are refused, withdrawn schemes show a notice + replacement, restore works,
editing a built-in switches it to dynamic translation, and the link-health job runs.

    python tests/e2e_schemes.py
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import pyotp

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from e2e_flow import Client, admin_login_details, finish, ok, reset_limits, signup, sql  # noqa: E402

SAME = {"Sec-Fetch-Site": "same-origin"}


def phone(prefix: str) -> str:
    return prefix + str(uuid.uuid4().int % 10**9).zfill(9)


def admin_login() -> Client:
    a_phone, a_password, a_stored = admin_login_details()
    a = Client("admin")
    a.refresh("/login")
    r = a.post("/api/auth/login", json={"phone": a_phone, "password": a_password})
    ok(r.status_code == 200, "admin password login", r.text[:200])
    if r.json().get("totp_required"):
        sql("UPDATE users SET totp_enabled = false, totp_secret_enc = NULL WHERE phone = :p", p=a_stored)
        a = Client("admin")
        a.refresh("/login")
        a.post("/api/auth/login", json={"phone": a_phone, "password": a_password})
    a.refresh("/admin/2fa")
    s = a.post("/api/auth/totp/setup", json={})
    v = a.post("/api/auth/totp/verify", json={"code": pyotp.TOTP(s.json()["secret"]).now()})
    ok(v.status_code == 200, "admin TOTP verified", v.text[:120])
    a.refresh("/admin?tab=schemes")
    return a


def scheme(slug: str):
    rows = sql("SELECT id, status, version, link_status, i18n_managed, name FROM gov_schemes WHERE slug = :s", s=slug)
    return rows[0] if rows else None


def eligibility(c: Client, **profile) -> dict:
    r = c.post("/api/schemes/check", json=profile)
    return {x["id"]: x for x in r.json().get("results", [])} if r.status_code == 200 else {"_error": r.text}


def main() -> int:
    t0 = time.time()
    reset_limits()
    slug = f"e2e_rythu_{uuid.uuid4().hex[:6]}"
    admin = admin_login()
    farmer = Client("farmer")
    pf = phone("8")
    signup(farmer, "farmer", pf, "Lakshmi Devi", "582941", 17.38, 78.48, dob="1990-05-05")
    try:
        print("1. admin adds a state scheme (GO step) — unofficial links are refused")
        base = {"slug": slug, "name": "Rythu Bharosa (test)", "description": "Investment support for Telangana farmers per acre each season.",
                "audience": ["farmer", "owner"], "level": "state", "states": ["Telangana"], "rules": {"land": True, "min_age": 18},
                "change_note": "launched"}
        for bad in ("http://www.telangana.gov.in/", "https://telangana-gov.in.example.com/", "https://www.telangana.gov.in:8443/x"):
            _, r = admin.action("admin.scheme.save", {**base, "url": bad}, expect_ok=False)
            ok(r.status_code == 400, f"refused unofficial link {bad}", r.text[:120])
        _, r = admin.action("admin.scheme.save", {**base, "url": "https://www.telangana.gov.in/", "rules": {"run": "x"}}, expect_ok=False)
        ok(r.status_code == 400, "refused unknown rule keys", r.text[:120])
        _, g = admin.action("admin.scheme.save", {**base, "url": "https://www.telangana.gov.in/"})
        row = scheme(slug)
        ok(row is not None and row[1] == "active", "scheme stored as active")
        page = farmer.c.get("/schemes").text
        ok("Rythu Bharosa (test)" in page, "new scheme visible on /schemes immediately (cache bumped after commit)")

        print("2. eligibility follows the scheme's state + rules")
        res = eligibility(farmer, age=35, landholding_hectares=2, state="Telangana", role="farmer")
        ok(res.get(slug, {}).get("status") == "eligible", "Telangana farmer with land → eligible", res.get(slug))
        ok("new" in res.get(slug, {}).get("badges", []), "marked as New")
        res = eligibility(farmer, age=35, landholding_hectares=2, state="Punjab", role="farmer")
        ok(res.get(slug, {}).get("status") == "not_eligible", "Punjab farmer → not eligible (other state)", res.get(slug))

        print("3. a farmer opens the official link → remembered so they can be told about changes")
        r = farmer.c.get(f"/schemes/go/{slug}", headers=SAME)
        ok(r.status_code == 200 and "https://www.telangana.gov.in/" in r.text, "interstitial shows the official link")
        ok(bool(sql("SELECT 1 FROM scheme_visits v JOIN gov_schemes s ON s.id = v.scheme_id WHERE s.slug = :s", s=slug)), "visit recorded")
        r = farmer.c.get(f"/schemes/go/{slug}", headers={"Sec-Fetch-Site": "cross-site"})
        ok(sql("SELECT visits FROM scheme_visits v JOIN gov_schemes s ON s.id = v.scheme_id WHERE s.slug = :s", s=slug)[0][0] == 1,
           "cross-site navigation does not record a visit")
        ok(farmer.c.get("/schemes/go/..%2f..%2fetc").status_code == 404 and farmer.c.get("/schemes/go/NOPE").status_code == 404,
           "unknown / malformed slugs → 404 (no open redirect)")

        print("4. admin raises the age limit → the farmer is notified, results change at once")
        sid = str(row[0])
        prep, g = admin.action("admin.scheme.save", {**{k: v for k, v in base.items() if k != "slug"}, "scheme_id": sid,
                                                    "url": "https://www.telangana.gov.in/", "rules": {"land": True, "min_age": 40},
                                                    "change_note": "Minimum age raised to 40 by the new guidelines"})
        ok(prep and any("40" in s for s in prep["summary"]), "GO summary shows the rule change", prep and prep["summary"])
        ok(scheme(slug)[2] == 2, "version bumped to 2")
        res = eligibility(farmer, age=35, landholding_hectares=2, state="Telangana", role="farmer")
        ok(res.get(slug, {}).get("status") == "not_eligible", "35-year-old no longer eligible after the change", res.get(slug))
        n = sql("SELECT title_key, params FROM notifications n JOIN users u ON u.id = n.user_id WHERE u.phone = :p AND n.kind = 'scheme_update'",
                p="+91" + pf)
        ok(any(x[0] == "notif.scheme_updated.title" for x in n), "farmer got 'scheme updated' notification", n)
        _, r = admin.action("admin.scheme.save", {**{k: v for k, v in base.items() if k != "slug"}, "scheme_id": sid,
                                                  "url": "https://www.telangana.gov.in/", "rules": {"land": True, "min_age": 40}}, expect_ok=False)
        ok(r.status_code == 400, "saving without changes is refused", r.text[:100])

        print("5. admin withdraws it with a replacement → notice + notification, gone from eligibility")
        admin.action("admin.scheme.archive", {"scheme_id": sid, "reason": "Merged into PM-KISAN for the test", "replaced_by": "pm_kisan"})
        ok(scheme(slug)[1] == "archived", "status archived")
        res = eligibility(farmer, age=45, landholding_hectares=2, state="Telangana", role="farmer")
        ok(slug not in res, "withdrawn scheme no longer in eligibility results")
        r = farmer.c.get(f"/schemes/go/{slug}")
        ok("Merged into PM-KISAN for the test" in r.text and "/schemes/go/pm_kisan" in r.text and "telangana.gov.in" not in r.text,
           "go page shows the withdrawal reason + replacement and no outbound link")
        ok("Merged into PM-KISAN" in farmer.c.get("/schemes").text, "listed under recently withdrawn")
        n = sql("SELECT 1 FROM notifications n JOIN users u ON u.id = n.user_id WHERE u.phone = :p AND n.title_key = 'notif.scheme_withdrawn.title'",
                p="+91" + pf)
        ok(bool(n), "farmer got 'scheme withdrawn' notification")
        _, r = admin.action("admin.scheme.save", {**{k: v for k, v in base.items() if k != "slug"}, "scheme_id": sid,
                                                  "url": "https://www.telangana.gov.in/"}, expect_ok=False)
        ok(r.status_code == 409, "a withdrawn scheme cannot be edited before restoring", r.text[:100])

        print("6. restore → active again, link re-checked in the background")
        admin.action("admin.scheme.restore", {"scheme_id": sid})
        ok(scheme(slug)[1] == "active", "restored")
        for _ in range(30):
            if scheme(slug)[3] != "unknown":
                break
            time.sleep(1)
        ok(scheme(slug)[3] in ("ok", "unreachable", "moved", "broken", "insecure", "unverified"),
           f"link checked after restore → {scheme(slug)[3]}")

        print("7. editing a built-in switches it from catalog translations to dynamic translation")
        nb = sql("SELECT id, description, version, rules, url, audience FROM gov_schemes WHERE slug = 'nbhm'")[0]
        admin.action("admin.scheme.save", {"scheme_id": str(nb[0]), "name": "National Beekeeping & Honey Mission",
                                           "description": nb[1] + " Updated for the test.", "url": nb[4], "audience": nb[5],
                                           "level": "central", "states": [], "rules": nb[3], "notify_users": False})
        ok(scheme("nbhm")[4] is False, "built-in now uses its stored (edited) text")
        hi = Client("hindi")
        hi.c.cookies.set("ks_lang", "hi")
        page = hi.c.get("/schemes").text
        ok("Updated for the test." in page and "data-translate" in page.split("Updated for the test.")[0][-200:],
           "Hindi page marks edited text for dynamic translation")

        print("8. admin pages + link-health job")
        r = admin.c.get("/admin?tab=schemes")
        ok(r.status_code == 200 and slug in r.text, "admin schemes tab lists the scheme", r.status_code)
        admin.action("admin.run_job", {"job": "schemes"})
        for _ in range(90):
            done = sql("SELECT ok, details FROM job_runs WHERE job = 'schemes' AND started_at > now() - interval '3 minutes' ORDER BY started_at DESC LIMIT 1")
            if done:
                break
            time.sleep(2)
        ok(bool(done) and done[0][0] is True, "link-health job finished", done and done[0][1])
        if done:
            print("     ", done[0][1])
    finally:
        sql("DELETE FROM gov_schemes WHERE slug = :s", s=slug)
        import json

        en = json.loads((Path(__file__).resolve().parent.parent / "app/i18n/locales/en.json").read_text("utf-8"))
        sql("UPDATE gov_schemes SET name = :n, description = :d, i18n_managed = true, version = 1 WHERE slug = 'nbhm'",
            n=en["scheme.nbhm.name"], d=en["scheme.nbhm.desc"])
        sql("UPDATE users SET totp_enabled = false, totp_secret_enc = NULL WHERE phone = :p", p=admin_login_details()[2])
        import redis

        from app.config import settings
        redis.Redis.from_url(settings.REDIS_URL).incr("ks:schemes:ver")
    return finish(t0)


if __name__ == "__main__":
    sys.exit(main())
