"""One login per account + account-leak tests, against a RUNNING server (TEST_MODE=true).

    python tests/load/seed_loadtest.py      # (once) seeded users are used for the concurrency leak check
    python tests/e2e_sessions.py

1. A second login signs the first device out: its page reload redirects to /login with the reason, its POSTs
   (even a GO prepared earlier) are refused with 401 and change NOTHING, its live socket is told why and cut off
   (it never receives the new device's notifications), and the account gets a "new sign-in" security notice.
2. Password reset signs every device out with its own reason; logout kills the session server-side;
   a pre-set (fixated) session id is never adopted.
3. Leaks: cross-account access to uploads / notifications / farms / orders / sessions is refused; JSON never carries
   secrets; personal pages are not cacheable; under 240 concurrent requests from 12 users nobody sees anyone else.
"""
from __future__ import annotations

import json
import random
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from e2e_flow import BASE, Client, finish, ok, reset_limits, signup, sql, text_image  # noqa: E402

UA_LAPTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
UA_PHONE = "Mozilla/5.0 (Android 15; Mobile; rv:144.0) Gecko/144.0 Firefox/144.0"
SECRET_MARKERS = ("password_hash", "totp_secret", "session_key", "$argon2", "\"lk\"", "token_version", "failed_logins", "pepper")
SEEN_JSON: list[str] = []


def phone(prefix: str) -> str:
    return prefix + str(uuid.uuid4().int % 10**9).zfill(9)


def device(name: str, ua: str, ip: str) -> Client:
    c = Client(name)
    c.c.headers.update({"User-Agent": ua, "X-Forwarded-For": ip})
    c.refresh("/login")
    return c


def jget(c: Client, path: str) -> httpx.Response:
    r = c.c.get(path, headers={"Accept": "application/json"})
    if "json" in r.headers.get("content-type", ""):
        SEEN_JSON.append(r.text)
    return r


def login(c: Client, ph: str, pw: str) -> httpx.Response:
    c.refresh("/login")
    return c.post("/api/auth/login", json={"phone": ph, "password": pw})


def socket_events(cookie: str, ip: str) -> tuple[object, list]:
    import socketio

    events: list = []
    s = socketio.Client(reconnection=False)
    s.on("session:revoked", lambda d: events.append(("session:revoked", d)))
    s.on("notify", lambda d: events.append(("notify", d)))
    s.on("disconnect", lambda *a: events.append(("disconnect", None)))
    s.connect(BASE, headers={"Cookie": f"ks_session={cookie}", "X-Forwarded-For": ip}, transports=["websocket"], wait_timeout=10)
    return s, events


def main() -> int:
    t0 = time.time()
    reset_limits()
    run = uuid.uuid4().hex[:6]
    pa, pw_a = phone("8"), "583920"
    marker = f"E2E-REVOKED-{run}"

    print("1. one login per account")
    laptop = device("A-laptop", UA_LAPTOP, "10.77.0.1")
    signup(laptop, "farmer", pa, "Asha Sessions", pw_a, 17.38, 78.48, dob="1993-04-04")
    me = laptop.c.get("/api/auth/me").json().get("user") or {}
    ok(me.get("name") == "Asha Sessions", "laptop signed in", me)
    prep = laptop.post("/api/actions/prepare", json={"type": "profile.update", "payload": {"skills": marker}})
    ok(prep.status_code == 200, "laptop prepared a change (not yet confirmed)", prep.text[:120])
    action_id = prep.json().get("action_id")
    old_cookie = laptop.c.cookies.get("ks_session")
    sock, events = socket_events(old_cookie, "10.77.0.1")
    ok(sock.connected, "laptop has a live notification socket")

    phone_dev = device("A-phone", UA_PHONE, "10.77.0.2")
    r = login(phone_dev, pa, pw_a)
    ok(r.status_code == 200, "same account signs in on a phone", r.text[:120])
    time.sleep(3)
    revoked = [e for e in events if e[0] == "session:revoked"]
    ok(bool(revoked) and revoked[0][1].get("reason") == "replaced", "laptop socket told why at once (session:revoked → replaced)", events)
    ok(("disconnect", None) in events or not sock.connected, "laptop socket disconnected by the server")
    ok(not any(e[0] == "notify" for e in events), "laptop socket did NOT receive the phone's notifications (no live leak)", events)
    try:
        sock.disconnect()
    except Exception:
        pass

    g = laptop.post(f"/api/actions/{action_id}/go", json={})
    ok(g.status_code == 401 and g.json().get("error") == "session_revoked" and g.json().get("reason") == "replaced",
       "laptop GO refused with 401 session_revoked/replaced", g.text[:200])
    st = sql("SELECT status FROM pending_actions WHERE id = :i", i=action_id)[0][0]
    skills = sql("SELECT skills FROM users WHERE phone = :p", p="+91" + pa)[0][0]
    ok(st == "prepared" and skills != marker, "nothing changed: the action was not executed", (st, skills))
    ok("ks_session" not in laptop.c.cookies or not laptop.c.cookies.get("ks_session"), "revocation response cleared the cookie")

    replay = httpx.Client(base_url=BASE, cookies={"ks_session": old_cookie}, headers={"X-Forwarded-For": "10.77.0.1"}, timeout=30)
    before = sql("SELECT count(*) FROM pending_actions pa JOIN users u ON u.id = pa.user_id WHERE u.phone = :p", p="+91" + pa)[0][0]
    m = re.search(r'name="csrf-token" content="([^"]+)"', replay.get("/login").text)
    r = replay.post("/api/actions/prepare", json={"type": "profile.update", "payload": {"skills": marker}},
                    headers={"X-CSRF-Token": m.group(1) if m else ""})
    after = sql("SELECT count(*) FROM pending_actions pa JOIN users u ON u.id = pa.user_id WHERE u.phone = :p", p="+91" + pa)[0][0]
    ok(r.status_code == 401 and before == after, "replayed old cookie: POST refused, no new action created", (r.status_code, r.text[:120]))
    replay.cookies.set("ks_session", old_cookie)
    r = replay.post("/api/i18n/lang", json={"lang": "ta"}, headers={"X-CSRF-Token": m.group(1) if m else ""})
    lang_now = sql("SELECT language FROM users WHERE phone = :p", p="+91" + pa)[0][0]
    ok(r.status_code == 401 and lang_now != "ta", "replayed old cookie cannot change the language either", (r.status_code, lang_now))
    replay.cookies.set("ks_session", old_cookie)
    r = replay.get("/dashboard", follow_redirects=False)
    loc = r.headers.get("location", "")
    ok(r.status_code == 303 and loc.startswith("/login?reason=replaced"), "page reload with old cookie → /login?reason=replaced", (r.status_code, loc))
    page = replay.get(loc).text
    ok("signed in on another device" in page and "Firefox on Android" in page, "login page tells why + which device signed in")
    r = replay.get("/api/auth/me")
    ok(r.status_code == 200 and r.json().get("user") is None, "afterwards the device is simply signed out (no redirect loop)")
    forged = replay.get("/login?reason=replaced&d=eyJhbGciOiJIUzI1NiJ9.eyJkZXZpY2UiOiJIQUNLRVIifQ.x").text
    ok("HACKER" not in forged, "forged sign-out details are not shown")
    me = phone_dev.c.get("/api/auth/me").json().get("user") or {}
    ok(me.get("name") == "Asha Sessions", "phone stays signed in", me)
    # the old cookie can only open an anonymous (guest) socket: it must never join the account's live channel
    s2, ev2 = socket_events(old_cookie, "10.77.0.1")
    login(phone_dev, pa, pw_a)            # signing in again sends a "new sign-in" notice to the account's channel
    time.sleep(2.5)
    ok(not any(e[0] == "notify" for e in ev2), "a socket opened with the old cookie receives none of the account's notifications", ev2)
    try:
        s2.disconnect()
    except Exception:
        pass
    me = phone_dev.c.get("/api/auth/me").json().get("user") or {}
    ok(me.get("name") == "Asha Sessions", "phone signed in again")
    n = sql("SELECT title_key, params FROM notifications n JOIN users u ON u.id = n.user_id WHERE u.phone = :p AND n.kind = 'security'",
            p="+91" + pa)
    ok(any(x[0] == "notif.new_login.title" and x[1].get("device") == "Firefox on Android" for x in n), "account got a 'new sign-in' notice")
    sessions = jget(phone_dev, "/api/account/sessions").json()["sessions"]
    ok(len(sessions) == 1 and "hash" not in sessions[0], "exactly one live session, session hashes never exposed", sessions)

    print("2. password reset / logout / fixation")
    third = device("A-tablet", UA_LAPTOP, "10.77.0.3")
    r = third.post("/api/auth/otp", json={"phone": pa, "purpose": "reset"})
    code = r.json().get("test_code")
    r = third.post("/api/auth/password/reset", json={"phone": pa, "otp": code, "new_password": "Reset2026xy"})
    ok(r.status_code == 200, "password reset with OTP", r.text[:120])
    r = phone_dev.c.get("/dashboard", follow_redirects=False)
    ok(r.status_code == 303 and "reason=password_reset" in r.headers.get("location", ""), "phone signed out with reason password_reset",
       r.headers.get("location"))
    r = login(phone_dev, pa, pw_a)
    ok(r.status_code == 401, "old password no longer works")
    r = login(phone_dev, pa, "Reset2026xy")
    ok(r.status_code == 200, "new password works")
    phone_dev.post("/api/auth/logout", json={})
    gone = phone_dev.c.cookies.get("ks_session")
    replay2 = httpx.Client(base_url=BASE, cookies={"ks_session": r.cookies.get("ks_session") or "x"}, timeout=30)
    ok(replay2.get("/api/auth/me").json().get("user") is None and not gone, "after logout the old cookie is dead server-side")

    pb = phone("8")
    bdev = device("B", UA_LAPTOP, "10.77.0.4")
    signup(bdev, "farmer", pb, "Bhola Leak", "641937", 17.40, 78.50, dob="1991-05-05")
    fix = device("fixation", UA_LAPTOP, "10.77.0.5")
    fix.c.cookies.set("ks_session", "attacker-chosen-session-id-0123456789")
    r = login(fix, pb, "641937")
    ok(r.status_code == 200 and r.cookies.get("ks_session") not in (None, "attacker-chosen-session-id-0123456789"),
       "login issues a fresh session id (fixation impossible)")
    bdev = fix  # B's latest device
    attacker = device("guesser", UA_LAPTOP, "10.77.0.9")
    codes = [login(attacker, pb, f"wrong{i}x") .status_code for i in range(6)]
    ok(codes[:5] == [401] * 5 and codes[5] == 429, "password guessing from one IP is locked after 5 tries", codes)
    owner_again = device("B-home", UA_PHONE, "10.77.0.10")
    r = login(owner_again, pb, "641937")
    ok(r.status_code == 200, "...but the real owner on another IP is NOT locked out (no lockout DoS)", r.text[:120])
    bdev = owner_again

    print("3. cross-account access (A vs B)")
    adev = device("A-again", UA_LAPTOP, "10.77.0.6")
    login(adev, pa, "Reset2026xy")
    ups = {}
    for kind in ("plant", "idproof"):
        u = adev.post("/api/uploads", data={"kind": kind}, files={"file": ("x.png", text_image(["private", kind]), "image/png")})
        ok(u.status_code == 200, f"A uploaded a private {kind}", u.text[:100])
        ups[kind] = u.json().get("upload_id")
    for kind, uid in ups.items():
        rb = bdev.c.get(f"/media/{uid}")
        ra = httpx.get(f"{BASE}/media/{uid}")
        ok(rb.status_code in (403, 404) and ra.status_code in (401, 403, 404), f"B and strangers cannot fetch A's {kind}", (rb.status_code, ra.status_code))
        ok(adev.c.get(f"/media/{uid}").status_code == 200, f"A can fetch her own {kind}")
    b_notes = [str(x[0]) for x in sql("SELECT n.id FROM notifications n JOIN users u ON u.id = n.user_id WHERE u.phone = :p", p="+91" + pb)]
    if b_notes:
        adev.post("/api/notifications/read", json={"ids": b_notes})
        unread = sql("SELECT count(*) FROM notifications WHERE id = ANY(:ids) AND read_at IS NULL", ids=[uuid.UUID(x) for x in b_notes])[0][0]
        ok(unread == len(b_notes), "A cannot mark B's notifications as read")
    mine = jget(adev, "/api/notifications").json()
    ok(all("Bhola" not in json.dumps(x) for x in mine.get("items", mine if isinstance(mine, list) else [])), "A's notification feed has nothing of B")
    farm = sql("SELECT id FROM farms ORDER BY created_at DESC LIMIT 1")
    if farm:
        fid = farm[0][0]
        ok(adev.c.get(f"/farms/{fid}?tab=payroll").status_code in (403, 404), "A cannot open someone else's farm payroll")
        ok(adev.c.get(f"/farms/{fid}/ledger.csv").status_code in (403, 404), "A cannot download someone else's ledger")
    order = sql("SELECT id FROM orders ORDER BY created_at DESC LIMIT 1")
    if order:
        r = adev.post("/api/actions/prepare", json={"type": "order.cancel", "payload": {"order_id": str(order[0][0])}})
        ok(r.status_code in (403, 404), "A cannot cancel someone else's order", r.status_code)
    pay = sql("SELECT id FROM salary_payments ORDER BY created_at DESC LIMIT 1")
    if pay:
        ok(adev.c.get(f"/payments/{pay[0][0]}").status_code in (403, 404), "A cannot view someone else's salary proof")
    ok(adev.c.get("/account/export/forged-token").status_code in (400, 403, 404), "forged export link refused")
    ok(adev.c.get("/admin").status_code in (303, 403), "A cannot open the admin area")

    print("3b. partial updates through GO change only what was sent (regression: stress test found NULL-ed fields)")
    _, g = adev.action("profile.update", {"skills": "Drip irrigation"})
    row = sql("SELECT full_name, skills, dob FROM users WHERE phone = :p", p="+91" + pa)[0]
    ok(g is not None and g.status_code == 200 and row[0] == "Asha Sessions" and row[1] == "Drip irrigation" and row[2] is not None,
       "profile.update {skills} kept name and birth date", row)
    seed = Path(__file__).resolve().parent / "load" / ".seed.json"
    if seed.exists():
        o = json.loads(seed.read_text("utf-8"))["owners"][-1]
        own = httpx.Client(base_url=BASE, cookies={"ks_session": o["sid"]}, headers={"X-Forwarded-For": "10.77.0.20"}, timeout=60)
        tok = re.search(r'name="csrf-token" content="([^"]+)"', own.get("/dashboard").text).group(1)
        pid = o["products"][-1]["id"]
        before = sql("SELECT name, unit, category, description, version FROM products WHERE id = :i", i=pid)[0]
        new_price = random.randint(20, 900)
        r = own.post("/api/actions/prepare", json={"type": "product.update", "payload": {"product_id": pid, "expected_version": before[4],
                                                                                          "price": new_price}}, headers={"X-CSRF-Token": tok})
        g = own.post(f"/api/actions/{r.json().get('action_id')}/go", json={}, headers={"X-CSRF-Token": tok}) if r.status_code == 200 else r
        after = sql("SELECT name, unit, category, description, price FROM products WHERE id = :i", i=pid)[0]
        ok(g.status_code == 200 and tuple(after[:4]) == tuple(before[:4]) and float(after[4]) == float(new_price),
           "product.update {price} kept name, unit, category and description", (g.status_code, before, after))

    print("4. secrets never leave the server; personal pages are not cacheable")
    for path in ("/api/auth/me", "/api/account/sessions", "/api/notifications", "/api/shop/products", "/api/jobs/search?lat=17.4&lon=78.5"):
        jget(adev, path)
    leaked = [m for m in SECRET_MARKERS for body in SEEN_JSON if m in body]
    ok(not leaked, "no password hashes / TOTP secrets / session keys in any JSON response", leaked[:5])
    for path in ("/dashboard", "/account", "/account/statement", "/notifications", "/orders", "/api/auth/me", "/api/notifications", "/api/account/sessions"):
        cc = adev.c.get(path).headers.get("cache-control", "")
        ok("no-store" in cc, f"{path} is not cacheable ({cc})")
    seed = Path(__file__).resolve().parent / "load" / ".seed.json"
    if seed.exists():
        o = json.loads(seed.read_text("utf-8"))["owners"][0]
        pub = httpx.get(f"{BASE}/f/{o['farm_id']}").text
        ok(o["phone"][-10:] not in pub, "public farm page does not reveal the owner's phone number")

    print("5. 240 concurrent requests from 12 users — everyone only ever sees themselves")
    if not seed.exists():
        print("     (skipped — run tests/load/seed_loadtest.py first)")
    else:
        people = random.sample([w for w in json.loads(seed.read_text("utf-8"))["workers"] if w.get("name")], 12)
        clients = {p["user_id"]: httpx.Client(base_url=BASE, cookies={"ks_session": p["sid"]}, timeout=60,
                                              headers={"X-Forwarded-For": f"10.78.0.{i + 1}"}) for i, p in enumerate(people)}
        names = {p["user_id"]: p["name"] for p in people}
        # a seeded session signed out since (a newer login elsewhere) browses as a guest: NOT a leak — leave it out
        def me(p):
            r = clients[p["user_id"]].get("/api/auth/me")
            return ((r.json() or {}).get("user") or {}).get("id") if r.status_code == 200 else None
        live = [p for p in people if me(p) == p["user_id"]]
        if len(live) < len(people):
            print(f"     ({len(people) - len(live)} seeded session(s) were signed out since seeding — not used)")
        people = live or people
        bad, lock = [], threading.Lock()

        def hit(i: int) -> None:
            """A LEAK = a response that carries a DIFFERENT person's identity (a guest / signed-out answer is not one)."""
            p = people[i % len(people)]
            c = clients[p["user_id"]]
            if i % 2:
                r = c.get("/api/auth/me")
                seen = ((r.json() or {}).get("user") or {}).get("id") if r.status_code == 200 else None
                leak = seen is not None and seen != p["user_id"]
                shown = seen
            else:
                r = c.get("/dashboard")
                m = re.search(r'<h1 class="ks-title ks-h1 m-0">(.*?)</h1>', r.text, re.S) if r.status_code == 200 else None
                tokens = set(re.findall(r"\bL[wo]\d{3,4}\b", m.group(1))) if m else set()
                leak = bool(tokens - {names[p["user_id"]].split()[0]})
                shown = sorted(tokens)
            if leak or r.status_code >= 500:
                with lock:
                    bad.append((p["name"], r.status_code, shown))

        with ThreadPoolExecutor(max_workers=48) as ex:
            list(ex.map(hit, range(240)))
        ok(not bad, "no response ever carried another user's identity", bad[:5])
    print("6. nobody waits behind a slow AI reply (no row lock held across an AI call)")
    if seed.exists():
        data = json.loads(seed.read_text("utf-8"))

        def live(sid: str, uid: str) -> bool:      # seeded sessions end when a load test signs that account in again
            r = httpx.get(f"{BASE}/api/auth/me", cookies={"ks_session": sid}, headers={"X-Forwarded-For": "10.77.1.9"}, timeout=30)
            return r.status_code == 200 and ((r.json() or {}).get("user") or {}).get("id") == uid

        owner, mates = None, []
        for o in data["owners"]:
            crew = [w for w in data["workers"] if (w.get("job") or {}).get("farm_id") == o["farm_id"]][:6]
            crew = [w for w in crew if live(w["sid"], w["user_id"])][:2]
            if len(crew) == 2 and live(o["sid"], o["user_id"]):
                owner, mates = o, crew
                break
        if not owner:
            print("     (skipped — no seeded owner with two seeded workers is still signed in; re-run seed_loadtest.py)")
        if owner and len(mates) == 2:
            sql("DELETE FROM salary_accruals WHERE farm_id = :f AND work_date = (now() AT TIME ZONE 'Asia/Kolkata')::date", f=owner["farm_id"])  # force fresh writes
            asker = httpx.Client(base_url=BASE, cookies={"ks_session": mates[0]["sid"]}, timeout=300, headers={"X-Forwarded-For": "10.77.1.1"})
            tok = re.search(r'name="csrf-token" content="([^"]+)"', asker.get("/assistant").text)
            started = threading.Event()

            def ask():
                started.set()
                asker.post("/api/ai/chat", json={"message": "How much salary do I have due this month?", "session": uuid.uuid4().hex[:32]},
                           headers={"X-CSRF-Token": tok.group(1) if tok else ""})

            th = threading.Thread(target=ask, daemon=True)
            th.start()
            started.wait()
            time.sleep(2.0)                     # the chat has built its context and is waiting for the AI now
            mate = httpx.Client(base_url=BASE, cookies={"ks_session": mates[1]["sid"]}, timeout=60, headers={"X-Forwarded-For": "10.77.1.2"})
            own = httpx.Client(base_url=BASE, cookies={"ks_session": owner["sid"]}, timeout=60, headers={"X-Forwarded-For": "10.77.1.3"})
            for label, cl, path in (("coworker dashboard", mate, "/dashboard"), ("owner's team tab", own, f"/farms/{owner['farm_id']}?tab=team")):
                t1 = time.perf_counter()
                r = cl.get(path)
                d = time.perf_counter() - t1
                ok(r.status_code == 200 and d < 5, f"{label} answered in {d:.1f}s while the AI was still thinking", r.status_code)
            th.join(timeout=1)
    return finish(t0)


if __name__ == "__main__":
    sys.exit(main())
