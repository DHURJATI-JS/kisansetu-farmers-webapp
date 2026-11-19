"""Spam strikes → escalating IP bans / posting bans, shared-IP protection and superadmin lifts — end-to-end against a
RUNNING server (TEST_MODE=true). The AI judge is switched to its rule-based path for the run (ks:test:moderation_ai_off)
so every assertion is deterministic; the AI path itself is exercised by the soak test.

    python tests/e2e_bans.py
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import redis

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from e2e_flow import Client, finish, ok, reset_limits, signup, sql  # noqa: E402
from e2e_schemes import admin_login  # noqa: E402

from app.config import settings  # noqa: E402

R = redis.Redis.from_url(settings.REDIS_URL)
SPAM = "Send OTP now to claim lottery prize <script>alert(1)</script> http://bit.ly/x http://a.b http://c.d"
RUN = uuid.uuid4().hex[:6]


def phone(prefix: str) -> str:
    return prefix + str(uuid.uuid4().int % 10**9).zfill(9)


def anon(ip: str) -> Client:
    c = Client(f"anon {ip}")
    c.c.headers["X-Forwarded-For"] = ip
    c.refresh("/")
    return c


def chat(c: Client, msg: str):
    return c.post("/api/ai/chat", json={"message": f"{msg} {uuid.uuid4().hex[:4]}" if "lottery" not in msg else msg, "session": "t"})


def clean_ip(ip: str) -> None:
    sql("UPDATE ip_bans SET lifted_at = now() WHERE ip = :ip AND lifted_at IS NULL", ip=ip)
    for k in (f"ks:ipban:{ip}", f"ks:strikes:ip:{ip}", f"ks:judged:ip:{ip}", f"ks:rl429:{ip}", f"ks:ipusers:{ip}", f"ks:msgs:ip:{ip}"):
        R.delete(k)
    R.set(f"ks:iplift:{ip}", "1", ex=120)


def wait_status(c: Client, want: int, secs: float, path: str = "/") -> int:
    end = time.time() + secs
    code = 0
    while time.time() < end:
        code = c.c.get(path).status_code
        if code == want:
            return code
        time.sleep(0.5)
    return code


def main() -> int:
    t0 = time.time()
    reset_limits()
    R.set("ks:test:moderation_ai_off", "1", ex=3600)
    ips = {k: f"10.84.{i}.{int(RUN[:2], 16) % 200 + 1}" for i, k in enumerate(("anon", "shared", "poster", "guess", "other"))}
    for ip in ips.values():
        clean_ip(ip)
    try:
        print("1. anonymous spammer: watched at 3 points, banned (rule) at 6, everyone else unaffected")
        a = anon(ips["anon"])
        r = chat(a, SPAM)
        ok(r.status_code in (400, 429) and r.json().get("error") == "errors.content_blocked", "spam message blocked", r.text[:120])
        time.sleep(1.0)
        ok(a.c.get("/").status_code == 200, "one offence (3 points): only watched, not banned")
        chat(a, SPAM)
        code = wait_status(a, 403, 6)
        ok(code == 403, "second offence (6 points): IP banned", code)
        ban = sql("SELECT level, source, shared, expires_at > now() + interval '50 minutes', expires_at < now() + interval '70 minutes', "
                  "evidence FROM ip_bans WHERE ip = :ip AND lifted_at IS NULL", ip=ips["anon"])
        ok(bool(ban) and ban[0][0] == 0 and ban[0][1] == "auto" and ban[0][3] and ban[0][4], "first ban: ladder step 1 = 1 hour", ban and ban[0][:5])
        ev = ban[0][5] if ban else {}
        ok(ev.get("points", 0) >= 6 and len(ev.get("offences", [])) >= 2 and "lottery" in str(ev), "ban carries its evidence", ev)
        ok(anon(ips["other"]).c.get("/").status_code == 200, "other IPs unaffected")
        r = a.c.get("/api/notifications")
        ok(r.status_code == 403 and r.json().get("error") == "errors.ip_banned" and "blocked" in r.json().get("message", ""),
           "API refused: same error shape as every API error, with a readable message", r.text[:120])
        h = a.c.get("/dashboard", headers={"Accept": "text/html"})
        ok(h.status_code == 403 and h.headers.get("content-type", "").startswith("text/html") and "<script" not in h.text,
           "a browser gets a small readable page (no scripts), not raw JSON")

        print("2. repeat offender: the next ban is one step up the ladder (24 h)")
        sql("UPDATE ip_bans SET expires_at = now() - interval '1 second' WHERE ip = :ip AND lifted_at IS NULL", ip=ips["anon"])
        R.delete(f"ks:ipban:{ips['anon']}", f"ks:strikes:ip:{ips['anon']}", f"ks:judged:ip:{ips['anon']}")
        code = wait_status(a, 200, 40)
        ok(code == 200, "the expired ban stops applying (ban table refresh)", code)
        chat(a, SPAM)
        time.sleep(0.5)
        chat(a, SPAM)
        code = wait_status(a, 403, 6)
        lv = sql("SELECT level, expires_at > now() + interval '23 hours' FROM ip_bans WHERE ip = :ip AND lifted_at IS NULL "
                 "ORDER BY created_at DESC LIMIT 1", ip=ips["anon"])
        ok(code == 403 and bool(lv) and lv[0][0] == 1 and lv[0][1], "second ban: step 2 = 24 hours", (code, lv))

        print("3. superadmin lifts it: every ban of that IP ends, strikes reset, works again within seconds")
        adm = admin_login()
        bid = str(sql("SELECT id FROM ip_bans WHERE ip = :ip AND lifted_at IS NULL", ip=ips["anon"])[0][0])
        adm.refresh("/admin?tab=security")
        ok(ips["anon"] in adm.c.get("/admin?tab=security").text, "the ban is listed on the security tab")
        bid = str(sql("SELECT id FROM ip_bans WHERE ip = :ip AND lifted_at IS NULL ORDER BY created_at DESC LIMIT 1", ip=ips["anon"])[0][0])
        _, g = adm.action("admin.ip", {"op": "lift", "ban_id": bid, "reason": "false positive test"})
        ok(sql("SELECT count(*) FROM ip_bans WHERE ip = :ip AND lifted_at IS NULL", ip=ips["anon"])[0][0] == 1,
           "the expired first ban is NOT lifted (it still counts on the ladder)")
        code = wait_status(a, 200, 8)
        ok(code == 200, "unbanned within seconds", code)
        row = sql("SELECT lifted_by_id IS NOT NULL, lift_note FROM ip_bans WHERE id = :b", b=bid)[0]
        ok(row[0] and row[1] == "false positive test", "lift recorded with who and why", row)
        ok(R.zcard(f"ks:strikes:ip:{ips['anon']}") == 0, "strike points cleared")

        print("4. shared address (CGNAT): ban ≤ 1 h and signed-in farmers still pass")
        R.pfadd(f"ks:ipusers:{ips['shared']}", *[str(uuid.uuid4()) for _ in range(5)])
        farmer = Client("shared farmer")
        farmer.c.headers["X-Forwarded-For"] = ips["shared"]
        signup(farmer, "farmer", phone("8"), f"Shared Farmer {RUN}", "582913", 13.0, 77.6, dob="1995-01-01")
        sp = anon(ips["shared"])
        chat(sp, SPAM)
        time.sleep(0.5)
        chat(sp, SPAM)
        code = wait_status(sp, 403, 6)
        sh = sql("SELECT shared, expires_at < now() + interval '61 minutes' FROM ip_bans WHERE ip = :ip AND lifted_at IS NULL", ip=ips["shared"])
        ok(code == 403 and bool(sh) and sh[0][0] and sh[0][1], "anonymous traffic banned; marked shared, at most 1 hour", (code, sh))
        r = farmer.c.get("/dashboard")
        ok(r.status_code == 200, "signed-in farmer on the same address still works", r.status_code)

        print("5. signed-in spammer: posting ban on the ACCOUNT (job, pay and complaints untouched); admin lifts it")
        s = Client("poster")
        s.c.headers["X-Forwarded-For"] = ips["poster"]
        ps = phone("8")
        signup(s, "farmer", ps, f"Spam Poster {RUN}", "582914", 13.0, 77.6, dob="1994-01-01")
        chat(s, SPAM)
        time.sleep(0.5)
        chat(s, SPAM)
        time.sleep(2.0)
        until = sql("SELECT posting_banned_until > now() + interval '50 minutes' FROM users WHERE phone = :p", p="+91" + ps)[0][0]
        ok(bool(until), "account posting ban (1 hour)", until)
        r = chat(s, "What fertiliser for ragi?")
        ok(r.status_code == 403 and r.json().get("error") == "errors.posting_banned", "chat refused with the reason and end time", r.text[:160])
        ok(s.c.get("/dashboard").status_code == 200, "the account itself keeps working")
        _, g = s.action("grievance.report", {"categories": ["unpaid_salary"], "description": "Salary not paid for 2 months"},
                        expect_ok=False)
        ok(g.json().get("error") != "errors.posting_banned", "a grievance report is NEVER refused for spam (here: no job yet)",
           g.text[:120])
        ok(not sql("SELECT 1 FROM ip_bans WHERE ip = :ip AND lifted_at IS NULL", ip=ips["poster"]), "the poster's IP is not banned (account judged)")
        sid = str(sql("SELECT s.id FROM user_sanctions s JOIN users u ON u.id = s.user_id WHERE u.phone = :p", p="+91" + ps)[0][0])
        adm.refresh("/admin?tab=security")
        adm.action("admin.sanction.lift", {"sanction_id": sid, "note": "warned by phone"})
        ok(sql("SELECT posting_banned_until IS NULL FROM users WHERE phone = :p", p="+91" + ps)[0][0], "posting ban lifted")
        r = s.post("/api/ai/chat", json={"message": "hello", "session": "t"})
        ok(not (r.status_code == 403 and r.json().get("error") == "errors.posting_banned"), "the account can post again", r.status_code)

        print("6. password guessing across accounts from one IP → strikes → ban")
        g1 = anon(ips["guess"])
        victims = []
        for i in range(3):
            v = Client(f"victim{i}")
            v.c.headers["X-Forwarded-For"] = f"10.85.{i}.{int(RUN[:2], 16) % 200 + 1}"
            pv = phone("7")
            signup(v, "buyer", pv, f"Victim {i} {RUN}", f"VictimPass{RUN}x{i}", 13.0, 77.6)
            victims.append(pv)
        for pv in victims:
            for k in R.scan_iter("ks:rl:*"):    # ONLY the login rate limit (8/min per IP) — strike points must accumulate
                R.delete(k)
            for _ in range(5):
                g1.post("/api/auth/login", json={"phone": pv, "password": "wrong-password-123"})
        code = wait_status(g1, 403, 6, "/login")
        ok(code == 403, "the guessing IP is banned after locking out 3 accounts", code)
        ok(all(sql("SELECT status = 'active' FROM users WHERE phone = :p", p="+91" + pv)[0][0] for pv in victims),
           "the guessed accounts are not punished")

        print("7. a busy / shared network that only hits rate limits is throttled, never banned (3-hour soak finding)")
        busy = anon(ips["other"])
        codes = [busy.post("/api/auth/login", json={"phone": "9999999999", "password": "x"}).status_code for _ in range(45)]
        ok(codes.count(429) >= 30, "the rate limit answered 429 (≥ 30 times in a row)", codes.count(429))
        time.sleep(3)
        ok(busy.c.get("/").status_code == 200 and not sql("SELECT 1 FROM ip_bans WHERE ip = :ip AND lifted_at IS NULL", ip=ips["other"]),
           "…and the address is NOT banned (rate limits alone are not abuse)")
    finally:
        R.delete("ks:test:moderation_ai_off")
        for ip in ips.values():
            clean_ip(ip)
    return finish(t0)


if __name__ == "__main__":
    sys.exit(main())
