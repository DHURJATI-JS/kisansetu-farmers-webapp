"""End-to-end flow against a RUNNING server (python run.py) with TEST_MODE=true.

    python tests/e2e_flow.py            # BASE=http://localhost:8088 by default

Covers: OTP sign-up, face-check rejection, GSTIN OCR + verify, farm creation (insurance + crops + terms),
hiring within 30 km, salary payment read from a screenshot (OCR + Gemma), shop order lifecycle with
idempotency, AI-proposed actions, wellbeing survey escalation, superadmin TOTP + admin pages.
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import sys
import time
import uuid
from datetime import date, time as dtime
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
BASE = os.environ.get("BASE", "http://127.0.0.1:8088")  # not localhost: IPv6-first lookup costs ~2 s per connect on Windows
RUN = uuid.uuid4().hex[:4]
FAILS: list[str] = []


def ok(cond: bool, label: str, extra: object = "") -> bool:
    print(("  ✔ " if cond else "  ✘ ") + label + (f"  → {extra}" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(label)
    return cond


def font(size: int):
    for name in ("arial.ttf", "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def text_image(lines: list[str], size=(900, 520), fs=44) -> bytes:
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    y = 30
    for ln in lines:
        d.text((40, y), ln, fill="black", font=font(fs))
        y += int(fs * 1.6)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class Client:
    def __init__(self, name: str):
        self.name = name
        self.c = httpx.Client(base_url=BASE, timeout=240, follow_redirects=False)
        self.csrf = ""
        self.refresh()

    def refresh(self, path: str = "/login") -> str:
        r = self.c.get(path)
        m = re.search(r'name="csrf-token" content="([^"]+)"', r.text)
        if m:
            self.csrf = m.group(1)
        return r.text

    def post(self, url: str, **kw):
        h = kw.pop("headers", {})
        h["X-CSRF-Token"] = self.csrf
        return self.c.post(url, headers=h, **kw)

    def action(self, type_: str, payload: dict, expect_ok: bool = True):
        r = self.post("/api/actions/prepare", json={"type": type_, "payload": payload})
        if r.status_code != 200:
            if expect_ok:
                ok(False, f"[{self.name}] prepare {type_}", r.text[:300])
            return None, r
        prep = r.json()
        g = self.post(f"/api/actions/{prep['action_id']}/go", json={})
        if expect_ok:
            ok(g.status_code == 200, f"[{self.name}] {type_} → {g.json().get('message', '')[:90] if g.status_code == 200 else ''}", g.text[:300])
        return prep, g


async def db_exec(sql: str, **params):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.config import settings

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            res = await conn.execute(text(sql), params)
            try:
                return res.fetchall()
            except Exception:
                return None
    finally:
        await engine.dispose()


def sql(q: str, **p):
    return asyncio.run(db_exec(q, **p))


async def _run_survey_tick() -> None:
    from app.database import engine
    from app.services.redis_client import init_redis
    from app.services.welfare import create_due_surveys
    from app.database import SessionLocal

    await init_redis()
    async with SessionLocal() as db:
        await create_due_surveys(db)
        await db.commit()
    await engine.dispose()



def admin_login_details() -> tuple[str, str, str]:
    """(login phone, password, stored E.164 phone) of the superadmin the server creates from .env on start.
    Never written into the tests: the repository can be public."""
    from app.config import settings
    from app.utils import normalize_phone

    stored = normalize_phone(settings.SUPERADMIN_PHONE or "")
    if not (stored and settings.SUPERADMIN_PASSWORD):
        sys.exit("Set SUPERADMIN_PHONE and SUPERADMIN_PASSWORD in .env (the superadmin the server creates on start).")
    return stored[3:], settings.SUPERADMIN_PASSWORD, stored


def signup(cl: Client, role: str, phone: str, name: str, password: str, lat: float, lon: float, email: str | None = None, dob: str = "1990-01-01"):
    cl.refresh("/signup")
    r = cl.post("/api/auth/otp", json={"phone": phone, "purpose": "signup"})
    code = r.json().get("test_code")
    ok(bool(code), f"[{cl.name}] OTP issued (test mode code shown)", r.text[:200])
    r = cl.post("/api/auth/signup", json={"role": role, "full_name": name, "phone": phone, "otp": code, "password": password,
                                          "email": email, "language": "en", "dob": dob, "lat": lat, "lon": lon,
                                          "accept_terms": True, "accept_privacy": True})
    ok(r.status_code == 200, f"[{cl.name}] signup as {role}", r.text[:300])
    cl.refresh("/dashboard")


def reset_limits() -> None:
    """Rate limits are per IP; the test clients all come from 127.0.0.1, so clear counters between runs."""
    import redis

    from app.config import settings

    r = redis.Redis.from_url(settings.REDIS_URL)
    for pattern in ("ks:rl:*", "ks:rl429:*", "ks:sio:*", "ks:strikes:ip:10.*", "ks:judged:ip:10.*", "ks:msgs:ip:10.*"):
        for k in r.scan_iter(pattern):
            r.delete(k)
    # earlier runs may have banned the test networks (10.x): lift those bans so this run starts clean (≤ 5 s to apply)
    lifted = [row[0] for row in sql("UPDATE ip_bans SET lifted_at = now(), lift_note = 'test reset' WHERE lifted_at IS NULL "
                                    "AND ip LIKE '10.%' RETURNING ip") or []]
    for ip in set(lifted):
        r.delete(f"ks:ipban:{ip}")
        r.set(f"ks:iplift:{ip}", "1", ex=120)
    if lifted:
        time.sleep(5.5)


def main() -> int:
    t0 = time.time()
    reset_limits()
    print(f"E2E against {BASE} (run {RUN})")
    owner, farmer, admin = Client("owner"), Client("farmer"), Client("admin")
    p_owner, p_farmer = f"98765{RUN[:2]}{int(RUN[2:], 16) % 1000:03d}"[:10], f"97654{RUN[:2]}{int(RUN[2:], 16) % 1000:03d}"[:10]
    p_owner = "9" + str(int(uuid.uuid4().int % 10**9)).zfill(9)
    p_farmer = "8" + str(int(uuid.uuid4().int % 10**9)).zfill(9)

    print("1. sign-up")
    signup(owner, "owner", p_owner, "Ravi Patil", "OwnerPass2026", 20.0, 73.79, email=f"ravi{RUN}@kisansetu.in", dob="1985-05-05")
    signup(farmer, "farmer", p_farmer, "Sita Devi", "582914", 20.01, 73.80, dob="1995-02-02")
    r = owner.post("/api/auth/signup", json={})
    ok(r.status_code == 422, "invalid signup payload rejected (422)")

    print("2. photo face-check rejects non-faces")
    blank = io.BytesIO()
    Image.new("RGB", (640, 640), (40, 160, 90)).save(blank, "PNG")
    r = owner.post("/api/account/photo", files={"file": ("x.png", blank.getvalue(), "image/png")})
    ok(r.status_code == 400, "green square is not accepted as a face photo", r.text[:200])
    sql("UPDATE users SET photo_verified = true WHERE phone IN (:a, :b)", a="+91" + p_owner, b="+91" + p_farmer)

    print("3. GSTIN OCR + verification")
    import random
    import string

    from app.services.gstin import checksum_char

    letters = "".join(random.choices("ABCDEFGHJKMNPRTUVWXY", k=5))
    first14 = "27" + letters + f"{random.randint(1000, 9999)}" + random.choice("ABCDEFGHJKLMNP") + "1Z"
    gstin = first14 + checksum_char(first14)
    gst = text_image(["Form GST REG-06", "Registration Certificate", f"GSTIN: {gstin}", "Legal Name: Ravi Patil Farms"])
    r = owner.post("/api/gstin/scan", files={"file": ("gst.png", gst, "image/png")})
    ok(r.status_code == 200 and r.json().get("gstin") == gstin, f"GSTIN {gstin} read by OCR engines", r.text[:300])
    if r.status_code != 200:
        return finish(t0)
    print("     engines:", r.json().get("engines"))
    owner.action("gstin.verify", {"verification_id": r.json()["verification_id"]})
    vid = r.json()["verification_id"]

    print("4. farm creation (insurance + crops + boilerplate terms)")
    ups = []
    for kind_label in ("Salary insurance policy", "Health insurance policy"):
        u = owner.post("/api/uploads", data={"kind": "insurance"}, files={"file": ("p.png", text_image([kind_label, "Policy No 123"]), "image/png")})
        ok(u.status_code == 200, f"upload {kind_label}", u.text[:200])
        ups.append(u.json()["upload_id"])
    today = date.today()
    farm_payload = {
        "gstin_verification_id": vid, "name": f"Green Valley {RUN}", "farming_type": "mixed", "hiring_limit": 10, "pay_day": 5,
        "payment_method": "upi", "terms_mode": "boilerplate", "accept_platform_terms": True,
        "branch": {"name": "Main farm", "lat": 20.0, "lon": 73.79, "work_start": "08:00", "work_end": "16:00", "weekly_offs": [6],
                   "crops": [{"name": "Onion"}, {"name": "Grapes"}]},
        "insurance": [
            {"policy_type": "salary", "insurer": "Test Insurer", "policy_number": f"SAL{RUN}", "sum_insured": 500000, "covers_salaries": True,
             "valid_from": str(today.replace(day=1)), "valid_to": str(today.replace(year=today.year + 1)), "upload_id": ups[0]},
            {"policy_type": "health", "insurer": "Test Insurer", "policy_number": f"HLT{RUN}", "sum_insured": 300000,
             "valid_from": str(today.replace(day=1)), "valid_to": str(today.replace(year=today.year + 1)), "upload_id": ups[1]}]}
    bad = dict(farm_payload, branch=dict(farm_payload["branch"], work_end="18:30"))
    _, r = owner.action("farm.create", bad, expect_ok=False)
    ok(r.status_code == 400 and "hours" in r.text.lower(), "10.5-hour working day is refused (max 8 h)", r.text[:200])
    prep, g = owner.action("farm.create", farm_payload)
    farm_id = g.json().get("farm_id") if g is not None and g.status_code == 200 else None
    if not farm_id:
        return finish(t0)
    print("     summary:", (prep or {}).get("summary", [])[:3])
    branch_id = str(sql("SELECT id FROM branches WHERE farm_id = :f", f=farm_id)[0][0])

    print("5. hiring (minimum wage, 30 km, T&C)")
    _, r = owner.action("job.create", {"farm_id": farm_id, "branch_id": branch_id, "title": "Picker", "salary_type": "daily",
                                        "salary_amount": 100, "positions": 2}, expect_ok=False)
    ok(r.status_code == 400, "salary below minimum wage is refused", r.text[:160])
    _, g = owner.action("job.create", {"farm_id": farm_id, "branch_id": branch_id, "title": "Grape picker", "salary_type": "daily",
                                        "salary_amount": 500, "positions": 2})
    job_id = g.json()["job_id"]
    farmer.refresh(f"/jobs/{job_id}")
    _, r = farmer.action("job.apply", {"job_id": job_id, "lat": 21.5, "lon": 75.0, "accept_terms": True}, expect_ok=False)
    ok(r.status_code == 400, "applying from >30 km is refused", r.text[:160])
    farmer.action("job.apply", {"job_id": job_id, "lat": 20.01, "lon": 73.80, "accept_terms": True})
    app_id = str(sql("SELECT id FROM job_applications WHERE posting_id = :j", j=job_id)[0][0])
    owner.action("application.decide", {"application_id": app_id, "decision": "accept"})
    member_id = str(sql("SELECT id FROM farm_members WHERE farm_id = :f AND role = 'employee'", f=farm_id)[0][0])

    print("5b. location turned off: typed address at signup, saved location for joining and delivery")
    typed = Client("typed-worker")
    p_typed = "7" + str(int(uuid.uuid4().int % 10**9)).zfill(9)
    typed.refresh("/signup")
    r = typed.post("/api/auth/location/typed", json={"state": "Maharashtra", "district": "Pune", "place": "Pune"})
    ok(r.status_code == 200 and r.json().get("ok") and r.json().get("state") == "Maharashtra", "typed address found without GPS", r.text[:200])
    r = typed.post("/api/auth/location/typed", json={"state": "Atlantis", "district": "Xx", "place": "Yy"})
    ok(r.status_code == 422, "a state outside India is refused", r.status_code)
    r = typed.post("/api/auth/location/typed", json={"state": "Kerala", "district": "Qwzxvq", "place": "Zzqxvb"})
    ok(r.status_code == 200 and r.json().get("ok") is False, "an unknown place is never invented", r.text[:200])
    code = typed.post("/api/auth/otp", json={"phone": p_typed, "purpose": "signup"}).json().get("test_code")
    r = typed.post("/api/auth/signup", json={"role": "farmer", "full_name": "Typed Worker", "phone": p_typed, "otp": code,
                                            "password": "582914", "language": "en", "dob": "1994-04-04",
                                            "address": {"state": "Maharashtra", "district": "Pune", "place": "Pune"},
                                            "accept_terms": True, "accept_privacy": True})
    ok(r.status_code == 200, "signup with a typed address (location off)", r.text[:200])
    row = sql("SELECT location_verified_at, state, district, city, lat FROM users WHERE phone = :p", p="+91" + p_typed)[0]
    ok(row[0] is None and tuple(row[1:4]) == ("Maharashtra", "Pune", "Pune") and row[4] is not None,
       "typed location saved as typed (not marked GPS-verified)", row)
    sql("UPDATE users SET photo_verified = true WHERE phone = :p", p="+91" + p_typed)
    typed.refresh(f"/jobs/{job_id}")
    _, r = typed.action("job.apply", {"job_id": job_id, "accept_terms": True}, expect_ok=False)
    ok(r is not None and r.status_code == 400 and "saved location" in r.text,
       "location off, saved address ~165 km away: refused, and told how to fix it", r.text[:220] if r is not None else "")
    typed.refresh("/onboarding")
    prep, _ = typed.action("location.update", {"address": {"state": "Maharashtra", "district": "Nashik", "place": "Nashik"}})
    ok(bool(prep) and any("typed address" in s for s in prep["summary"]), "typed location update shown before GO", (prep or {}).get("summary"))
    typed.refresh(f"/jobs/{job_id}")
    prep, _ = typed.action("job.apply", {"job_id": job_id, "accept_terms": True})
    ok(bool(prep) and any("typed, not GPS" in s for s in prep["summary"]) and bool(prep.get("warnings")),
       "joined with location off: distance from the typed address, and the farm will be told", (prep or {}).get("summary"))
    src = dict(sql("SELECT u.full_name, a.location_source FROM job_applications a JOIN users u ON u.id = a.applicant_id "
                   "WHERE a.posting_id = :j", j=job_id))
    ok(src.get("Typed Worker") == "typed" and src.get("Sita Devi") == "live", "each application records where its distance came from", src)
    ok("from a typed address" in owner.c.get(f"/farms/{farm_id}?tab=hiring").text, "the farm's hiring tab labels it", "")

    print("6. payroll: salary read from screenshot by OCR + Gemma")
    worker_email = f"sita{RUN}@kisansetu.in"
    sql("UPDATE users SET email = :e FROM farm_members m WHERE m.id = :m AND users.id = m.user_id", e=worker_email, m=member_id)
    owner.refresh(f"/farms/{farm_id}?tab=payroll")
    shot = text_image(["Payment Successful", "Rs 500.00", "Paid to Sita Devi", "UPI Ref No: 4321" + str(uuid.uuid4().int)[:8]], size=(900, 560), fs=48)
    u = owner.post("/api/uploads", data={"kind": "payment"}, files={"file": ("pay.png", shot, "image/png")})
    prep, g = owner.action("payroll.pay", {"member_id": member_id, "upload_id": u.json()["upload_id"], "method": "upi"}, expect_ok=False)
    if prep:
        print("     summary:", prep["summary"])
        ok(g.status_code == 200, "salary payment recorded from OCR amount", g.text[:300])
    else:
        print("     (payment screenshot could not be verified:", g.text[:200], ")")
    rows = sql("SELECT amount FROM salary_payments WHERE member_id = :m", m=member_id)
    ok(bool(rows) and float(rows[0][0]) == 500.0, "payment amount equals the screenshot amount (₹500)", rows)
    print("6b. the screenshot reaches the worker on every channel they have")
    msgs = {r[0]: r for r in sql("SELECT o.channel, o.body, o.attachment_path, o.media_url FROM outbound_messages o JOIN farm_members m "
                                 "ON m.user_id = o.user_id WHERE m.id = :m AND o.body LIKE '%500%' ORDER BY o.created_at DESC", m=member_id)}
    local = BASE.rstrip("/").endswith(":8088")
    ok("email" in msgs and bool(msgs["email"][2]) and (Path(msgs["email"][2]).exists() if local else msgs["email"][2].endswith((".png", ".jpg", ".webp"))),
       "e-mail carries the screenshot as an attachment", msgs.get("email"))
    ok("whatsapp" in msgs and (msgs["whatsapp"][3] or "").endswith("/image"), "WhatsApp carries the screenshot as a picture", msgs.get("whatsapp"))
    link = re.search(r"https?://\S+/proof/[\w.\-]+", (msgs.get("sms") or ("", ""))[1])
    ok(bool(link), "SMS carries a private link to the proof (no sign-in needed)", msgs.get("sms"))
    if link:
        path = "/" + link.group(0).split("/", 3)[3]
        anon = httpx.Client(base_url=BASE, timeout=30)
        page, img = anon.get(path), anon.get(path + "/image")
        ok(page.status_code == 200 and "500" in page.text and "/image" in page.text, "proof page opens from the link and shows the screenshot")
        ok(img.status_code == 200 and img.content[:4] in (b"\x89PNG", b"RIFF") or img.content[:3] == b"\xff\xd8\xff", "the screenshot itself is served", img.headers.get("content-type"))
        ok("no-store" in page.headers.get("cache-control", "") and "noindex" in page.headers.get("x-robots-tag", ""), "private: never cached or indexed")
        ok(anon.get(path[:-6] + "AAAAAA").status_code == 404 and anon.get("/proof/not-a-token").status_code == 404, "tampered / fake links refused")
        pay_upload = sql("SELECT upload_id FROM salary_payments WHERE member_id = :m", m=member_id)[0][0]
        ok(anon.get(f"/media/{pay_upload}").status_code == 404, "the screenshot stays private outside the signed link")
    u2 = owner.post("/api/uploads", data={"kind": "payment"}, files={"file": ("pay.png", shot, "image/png")})
    _, r = owner.action("payroll.pay", {"member_id": member_id, "upload_id": u2.json()["upload_id"], "method": "upi"}, expect_ok=False)
    ok(r is not None and r.status_code in (400, 409), "same screenshot cannot be reused", r.text[:160] if r is not None else "")

    print("7. shop: idempotent COD order lifecycle")
    _, g = owner.action("shop.create", {"farm_id": farm_id, "name": f"Green Valley Shop {RUN}"})
    shop_id = g.json()["shop_id"]
    _, g = owner.action("product.create", {"shop_id": shop_id, "branch_id": branch_id, "name": "Fresh grapes", "category": "fruits",
                                            "unit": "kg", "price": 80, "stock": 50, "min_order_qty": 1,
                                            "description": "Seedless Thompson grapes, harvested this week, no pesticide residue tested."})
    product_id = g.json()["product_id"]
    idem = uuid.uuid4().hex
    order = {"shop_id": shop_id, "items": [{"product_id": product_id, "qty": 5}], "fulfilment": "pickup", "idempotency_key": idem}
    farmer.action("order.place", order)
    farmer.action("order.place", order, expect_ok=False)
    n = sql("SELECT count(*) FROM orders WHERE idempotency_key = :k", k=idem)[0][0]
    ok(n == 1, "replaying the same checkout does not create a second order", n)
    stock = float(sql("SELECT stock FROM products WHERE id = :p", p=product_id)[0][0])
    ok(stock == 45.0, "stock decremented exactly once (50 → 45)", stock)
    typed.refresh("/cart")      # delivery with location off: the distance comes from the saved (typed) location
    prep, g = typed.action("order.place", {"shop_id": shop_id, "items": [{"product_id": product_id, "qty": 2}], "fulfilment": "delivery",
                                           "address": "House 4, Nashik", "idempotency_key": uuid.uuid4().hex})
    drow = sql("SELECT buyer_lat, buyer_lon FROM orders WHERE buyer_id = (SELECT id FROM users WHERE phone = :p)", p="+91" + p_typed)
    ok(bool(prep) and any("saved location" in s for s in prep["summary"]) and bool(drow) and drow[0][0] is not None,
       "delivery order with location off uses the saved location", (prep or {}).get("summary"))
    oid = str(sql("SELECT id FROM orders WHERE idempotency_key = :k", k=idem)[0][0])
    for step in ("confirmed", "ready_for_pickup", "picked_up"):
        owner.action("order.update", {"order_id": oid, "to": step})
    led = sql("SELECT entry_type FROM ledger_entries WHERE farm_id = :f", f=farm_id)
    ok({"sale", "salary_payment"} <= {x[0] for x in led}, "ledger has the sale and the salary payment", led)
    r = owner.post(f"/api/shop/products/{product_id}/ask", json={"question": "Are these grapes seedless?"})
    ok(r.status_code == 200, "Ask-AI about product answered or forwarded", r.text[:200])
    print("     ask-ai:", {k: r.json().get(k) for k in ("answer", "forwarded_to_owner", "confidence")} if r.status_code == 200 else r.text[:120])

    print("8. AI assistant proposes actions (never executes)")
    r = owner.post("/api/ai/chat", json={"message": "I have planted tomato in my Main farm branch"})
    acts = [a.get("type") for a in r.json().get("actions", [])] if r.status_code == 200 else []
    ok(r.status_code == 200, "owner chat answered", r.text[:200])
    print("     reply:", (r.json().get("reply") or "")[:140], "| actions:", acts)
    ok(sql("SELECT count(*) FROM branch_crops WHERE branch_id = :b AND name ILIKE 'tomato%'", b=branch_id)[0][0] == 0,
       "AI did not change anything by itself (tomato not added without GO)")
    r = farmer.post("/api/ai/chat", json={"message": "Today the owner did not give us drinking water and made us work 11 hours"})
    print("     farmer reply:", (r.json().get("reply") or "")[:140], "| actions:", [a.get("type") for a in r.json().get("actions", [])] if r.status_code == 200 else r.text[:100])

    print("9. wellbeing survey → admin alert, anonymous owner warning")
    sql("UPDATE branches SET work_end = '00:00:01' WHERE id = :b", b=branch_id)
    asyncio.run(_run_survey_tick())
    sid = sql("SELECT id FROM wellbeing_surveys WHERE member_id = :m", m=member_id)
    ok(bool(sid), "survey created after working hours")
    if sid:
        farmer.refresh("/survey")
        r = farmer.post(f"/api/welfare/surveys/{sid[0][0]}", json={"rating": "bad", "answers": {"water": False, "overworked": True}, "comment": "No water"})
        ok(r.status_code == 200 and r.json().get("reported"), "bad day reported", r.text[:200])
    sql("UPDATE branches SET work_end = '16:00' WHERE id = :b", b=branch_id)
    ok(bool(sql("SELECT 1 FROM owner_warnings w JOIN farms f ON f.id = w.farm_id WHERE f.id = :f AND w.kind = 'anonymous_report'", f=farm_id)),
       "owner gets an anonymous warning (queued for the digest)")

    print("10. superadmin with TOTP")
    import pyotp
    admin.refresh("/login")
    a_phone, a_password, a_stored = admin_login_details()
    r = admin.post("/api/auth/login", json={"phone": a_phone, "password": a_password})
    ok(r.status_code == 200, "admin password login", r.text[:200])
    if r.status_code == 200 and r.json().get("totp_required"):
        print("     (TOTP already enabled from an earlier run — skipping admin pages)")
    elif r.status_code == 200:
        admin.refresh("/admin/2fa")
        s = admin.post("/api/auth/totp/setup", json={})
        ok(s.status_code == 200, "TOTP secret issued", s.text[:120])
        v = admin.post("/api/auth/totp/verify", json={"code": pyotp.TOTP(s.json()["secret"]).now()})
        ok(v.status_code == 200, "TOTP verified", v.text[:120])
        for tab in ("overview", "farms", "users", "reports", "cases", "appeals", "security", "settings", "outbox", "audit"):
            r = admin.c.get(f"/admin?tab={tab}")
            ok(r.status_code == 200, f"admin tab {tab}", r.status_code)
        sql("UPDATE users SET totp_enabled = false, totp_secret_enc = NULL WHERE phone = :p", p=a_stored)

    print("11. pages render for each role")
    for path in ["/dashboard", f"/farms/{farm_id}", *[f"/farms/{farm_id}?tab={t}" for t in ("branches", "team", "hiring", "payroll", "ledger", "shop", "insurance", "weather", "proposals", "settings")],
                 f"/f/{farm_id}", f"/shop/s/{shop_id}", f"/shop/p/{product_id}", "/account", "/notifications", "/assistant", "/orders", "/cart", f"/farms/{farm_id}/ledger.csv"]:
        r = owner.c.get(path)
        ok(r.status_code == 200, f"owner GET {path}", r.status_code)
    for path in ["/dashboard", "/survey", "/report", "/account", "/orders", "/jobs", f"/jobs/{job_id}", "/account/statement.csv"]:
        r = farmer.c.get(path)
        ok(r.status_code == 200, f"farmer GET {path}", r.status_code)
    r = farmer.c.get(f"/farms/{farm_id}?tab=ledger")
    ok(r.status_code in (303, 403), "farmer cannot open the owner's ledger", r.status_code)

    print("12. protections")
    _, r = owner.action("account.delete", {"confirm": True}, expect_ok=False)
    ok(r.status_code == 400, "owner cannot delete account while farms are open", r.text[:150])
    r = owner.c.post("/api/actions/prepare", json={"type": "crop.add", "payload": {}})
    ok(r.status_code == 403, "request without CSRF token is rejected", r.status_code)

    print("13. a worker becomes a farm owner too (and keeps the job)")
    farmer.refresh("/account")
    new_pw = "Sita" + uuid.uuid4().hex[:8] + "7x"
    up = {"email": f"sita.owner{RUN}@kisansetu.in", "current_password": "582914", "new_password": new_pw, "new_password_again": new_pw}
    _, r = farmer.action("account.become_owner", dict(up, current_password="000000"), expect_ok=False)
    ok(r.status_code == 400 and "wrong_password" in r.text, "wrong PIN refused", r.text[:120])
    _, r = farmer.action("account.become_owner", dict(up, new_password="582914abc", new_password_again="582914abc"), expect_ok=False)
    ok(r.status_code in (400, 422), "a weak owner password is refused", r.text[:120])
    _, r = farmer.action("account.become_owner", dict(up, new_password_again=new_pw + "!"), expect_ok=False)
    ok(r.status_code == 400 and "passwords_differ" in r.text, "mismatched new passwords refused", r.text[:120])
    _, r = farmer.action("account.become_owner", dict(up, email=f"ravi{RUN}@kisansetu.in"), expect_ok=False)
    ok(r.status_code == 409, "another account's email is refused", r.text[:120])
    prep, g = farmer.action("account.become_owner", up)
    ok(bool(prep) and any("Green Valley" in s for s in prep["summary"]), "the confirm step says the job continues", (prep or {}).get("summary"))
    row = sql("SELECT role, email FROM users WHERE phone = :p", p="+91" + p_farmer)[0]
    ok(tuple(row) == ("owner", up["email"]), "account is now a farm owner", row)
    still = sql("SELECT status FROM farm_members WHERE id = :m", m=member_id)[0][0]
    ok(still == "active", "the existing job is untouched", still)
    farmer.refresh("/report")
    _, r = farmer.action("grievance.report", {"categories": ["no_water"]}, expect_ok=False)
    ok(r is not None and r.status_code == 200, "an owner who still works somewhere can still report problems", r.text[:150] if r is not None else "")
    r = farmer.post("/api/gstin/scan", files={"file": ("g.png", text_image(["GSTIN"]), "image/png")})
    ok(r.status_code != 403, "owner tools are open (GST certificate scan)", r.status_code)
    for label, pw, want in (("old PIN", "582914", 401), ("new password", new_pw, 200)):
        c = Client(f"login-{label}")
        r = c.post("/api/auth/login", json={"phone": p_farmer, "password": pw})
        ok(r.status_code == want or (want == 401 and r.status_code in (400, 401)), f"login with the {label}: {r.status_code}", r.text[:120])
    uid = sql("SELECT id FROM users WHERE phone = :p", p="+91" + p_farmer)[0][0]
    leaked = sql("SELECT (SELECT count(*) FROM pending_actions WHERE user_id = :u AND (payload::text LIKE :a OR payload::text LIKE :b)) + "
                 "(SELECT count(*) FROM audit_logs WHERE actor_id = :u AND (details::text LIKE :a OR details::text LIKE :b))",
                 u=uid, a=f"%{new_pw}%", b="%582914%")[0][0]
    ok(leaked == 0, "no password or PIN stored in plain text (pending actions, audit log)", leaked)

    print("14. terms that take money from workers are refused and stop hiring")
    bad_terms = "You need to pay 1 lakh to join"
    owner.refresh(f"/farms/{farm_id}?tab=settings")
    r = owner.post("/api/terms/check", json={"text": bad_terms})
    j = r.json() if r.status_code == 200 else {}
    ok(j.get("ok") is False and any(i.get("code") == "fee_from_worker" and i.get("problem") for i in j.get("issues", [])),
       "'pay 1 lakh to join' is flagged with a reason, whatever the AI says", r.text[:200])
    _, r = owner.action("farm.update_terms", {"farm_id": farm_id, "terms_mode": "custom", "terms_text": bad_terms}, expect_ok=False)
    ok(r is not None and r.status_code == 400 and "terms_unlawful" in r.text, "such terms cannot be adopted", r.text[:160] if r is not None else "")
    sql("UPDATE farms SET terms_mode = 'custom', terms_text = :t WHERE id = :f", t=bad_terms, f=farm_id)   # adopted before the rule
    typed.refresh(f"/jobs/{job_id}")
    _, r = typed.action("job.apply", {"job_id": job_id, "accept_terms": True}, expect_ok=False)
    ok(r is not None and r.status_code == 409 and "farm_terms_blocked" in r.text, "nobody can join while such terms are live",
       r.text[:160] if r is not None else "")
    ok("Hiring is stopped" in owner.c.get(f"/farms/{farm_id}?tab=overview").text, "the owner sees why hiring stopped")
    sql("UPDATE farms SET terms_mode = 'boilerplate', terms_text = NULL WHERE id = :f", f=farm_id)

    print("15. closing the shop: a vote, open orders cancelled in one step, courier orders delivered first")
    typed.refresh("/cart")
    typed.action("order.place", {"shop_id": shop_id, "items": [{"product_id": product_id, "qty": 1}], "fulfilment": "pickup",
                                 "idempotency_key": uuid.uuid4().hex})
    typed.action("order.place", {"shop_id": shop_id, "items": [{"product_id": product_id, "qty": 3}], "fulfilment": "delivery",
                                 "address": "House 4, Nashik", "idempotency_key": uuid.uuid4().hex})
    courier_order = str(sql("SELECT id FROM orders WHERE shop_id = :s AND fulfilment = 'delivery' AND status = 'placed' "
                            "ORDER BY created_at DESC LIMIT 1", s=shop_id)[0][0])
    owner.refresh(f"/farms/{farm_id}?tab=shop")
    for step in ("confirmed", "dispatched"):
        owner.action("order.update", {"order_id": courier_order, "to": step})
    _, r = owner.action("shop.close", {"shop_id": shop_id}, expect_ok=False)
    ok(r is not None and r.status_code == 400 and "shop_open_orders" in r.text, "closing is refused while orders are open",
       r.text[:160] if r is not None else "")
    prep, _ = owner.action("shop.close", {"shop_id": shop_id, "cancel_open_orders": True})
    ok(bool(prep) and any("courier" in s for s in prep["summary"]), "the confirm step says courier orders must still be delivered",
       (prep or {}).get("summary"))
    left = dict(sql("SELECT status, count(*) FROM orders WHERE shop_id = :s AND status IN ('placed', 'confirmed', 'ready_for_pickup', "
                    "'dispatched') GROUP BY status", s=shop_id))
    shop_status = sql("SELECT status FROM shops WHERE id = :s", s=shop_id)[0][0]
    ok(left == {"dispatched": 1} and shop_status == "closing", "one click cancelled the rest; the courier order goes on; shop closing",
       (left, shop_status))
    _, r = typed.action("order.place", {"shop_id": shop_id, "items": [{"product_id": product_id, "qty": 2}], "fulfilment": "pickup",
                                        "idempotency_key": uuid.uuid4().hex}, expect_ok=False)
    ok(r is not None and r.status_code in (404, 409), "a closing shop takes no new orders", r.status_code if r is not None else "")
    owner.action("order.update", {"order_id": courier_order, "to": "delivered"})
    ok(sql("SELECT status FROM shops WHERE id = :s", s=shop_id)[0][0] == "closed", "the shop closed by itself after the last delivery")
    owner.action("shop.update", {"shop_id": shop_id, "status": "open"})
    ok(sql("SELECT status FROM shops WHERE id = :s", s=shop_id)[0][0] == "open", "the owner can reopen it")

    print("16. statements in the app: one's own, and the farm's for its owner (account + farm transaction history)")
    from app.utils import format_inr

    sita = Client("sita")              # step 13 signed her old session out (new password, one login per account)
    sita.post("/api/auth/login", json={"phone": p_farmer, "password": new_pw})
    r = sita.c.get("/account/statement?month=all")
    ok(r.status_code == 200 and "Salary received" in r.text and "Closing balance" in r.text, "a worker's own statement shows the salary",
       r.status_code)
    farm_net = sql("SELECT coalesce(sum(CASE direction WHEN 'in' THEN amount WHEN 'out' THEN -amount ELSE 0 END), 0) "
                   "FROM ledger_entries WHERE farm_id = :f", f=farm_id)[0][0]
    r = owner.c.get(f"/account/statement?of={farm_id}&month=all")
    ok(r.status_code == 200 and "Farm statement" in r.text and format_inr(farm_net) in r.text,
       f"the owner sees the farm statement in the account; closing balance = database total ({format_inr(farm_net)})", r.status_code)
    ok(f"/account/statement?of={farm_id}" in owner.c.get("/account").text, "the account page links the farm statement")
    r = typed.c.get(f"/account/statement?of={farm_id}")
    ok(r.status_code == 404, "someone who is not owner / co-owner cannot open the farm statement", r.status_code)
    r = owner.c.get(f"/farms/{farm_id}?tab=ledger&month=all")
    ok(r.status_code == 200 and "Closing balance" in r.text and format_inr(farm_net) in r.text,
       "the farm's transaction history shows the same statement", r.status_code)
    return finish(t0)


def finish(t0: float) -> int:
    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'} in {time.time() - t0:.0f}s")
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
