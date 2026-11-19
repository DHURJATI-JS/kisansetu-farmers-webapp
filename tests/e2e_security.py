"""Security + account-leak hunt against a RUNNING server (TEST_MODE=true), using the load-test seed.

    python tests/load/seed_loadtest.py      # fresh seed: live sessions + a private marker line per person and per farm
    python tests/e2e_security.py            # BASE=http://127.0.0.1:<PORT> when the server is not on 8088

Every seeded person has one statement line "LTS-<tag>" that only THEY may ever see, every seeded farm one ledger line
"LTF-<tag>" that only its owner / co-owners (account page) and its managers / accountants (farm transaction history) see.

1. Statements: own entries only — page, CSV and the farm's transaction history; strangers, employees, managers and
   ex-co-owners never get a farm's statement in their account; 300 concurrent requests from 16 accounts never mix.
2. Hostile links (month / page / of) — never a 500, never echoed back as live markup.
3. Stored markup (profile, typed address, ledger text) is stripped or escaped; CSV exports neutralise formulas.
4. Cross-account ids: another farm's branch on ledger.add / insurance.add, a stranger's shop / ledger / farm, a forged
   role on the owner upgrade, privilege fields on profile.update, GO on somebody else's prepared action.
5. Headers: personal pages and CSV exports are private (no-store, nosniff, attachment); the session cookie is HttpOnly.
6. Abuse limits: typed-address lookups are rate limited.
7. Login redirect (?next=) only ever lands on this site — in a real browser.
"""
from __future__ import annotations

import csv
import io
import json
import random
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from e2e_flow import BASE, Client, finish, ok, reset_limits, signup, sql  # noqa: E402

SEED = Path(__file__).resolve().parent / "load" / ".seed.json"
LTS_RE = re.compile(r"\bLTS-[OW]\d{3,4}\b")
LTF_RE = re.compile(r"\bLTF-O\d{3}\b")
MARKUP = ("<script>alert", "onerror=alert", "onmouseover=alert", "<img src=x", "<svg/onload")


class Seeded(Client):
    """A seeded account's own browser: its session cookie, its own address."""

    def __init__(self, name: str, ident: dict, ip: str):  # noqa: D107 — no super(): the guest GET would mint a session
        self.name, self.ident = name, ident
        self.c = httpx.Client(base_url=BASE, timeout=120, follow_redirects=False, cookies={"ks_session": ident["sid"]},
                              headers={"X-Forwarded-For": ip})
        self.csrf = ""
        self.refresh("/dashboard")

    def live(self) -> bool:
        r = self.c.get("/api/auth/me")
        return r.status_code == 200 and ((r.json() or {}).get("user") or {}).get("id") == self.ident["user_id"]


def run_async(fn, *args):
    """Run one service-level check in-process against the same database (fresh engine per call)."""
    import asyncio

    async def go():
        from app.database import engine
        from app.services.redis_client import init_redis

        await init_redis()                  # notify() / caches use Redis
        try:
            return await fn(*args)
        finally:
            await engine.dispose()
    return asyncio.run(go())


async def _ai_context(uid: str) -> str:
    from app.database import SessionLocal
    from app.models import User
    from app.services.ai.assistant import build_context

    async with SessionLocal() as db:
        return await build_context(db, await db.get(User, uuid.UUID(uid)))


async def _powers(uid: str, farm_id: str):
    from app.database import SessionLocal
    from app.models import Farm, User
    from app.services.notify import farm_leader_ids
    from app.services.perms import farm_role, roles_by_farm

    async with SessionLocal() as db:
        u, f = await db.get(User, uuid.UUID(uid)), await db.get(Farm, uuid.UUID(farm_id))
        return await farm_role(db, f, u), await roles_by_farm(db, u), u.id in await farm_leader_ids(db, f.id)


async def _admin_reject(farm_id: str) -> None:
    from types import SimpleNamespace

    from app.database import SessionLocal
    from app.services.actions.admin import AdminFarmReject

    act = AdminFarmReject()
    async with SessionLocal() as db:
        ctx = SimpleNamespace(db=db, user=None, ip=None, action_id=None, t=lambda k, **kw: k)
        await act.execute(ctx, act.Payload(farm_id=uuid.UUID(farm_id), reason="GSTIN belongs to another business"), None)
        await db.commit()


def markers(rx: re.Pattern, body: str) -> set[str]:
    return set(rx.findall(body or ""))


def no_markup(body: str) -> bool:
    low = (body or "").lower()
    return not any(m in low for m in MARKUP)


def main() -> int:
    t0 = time.time()
    if not SEED.exists():
        print("needs the load-test seed: python tests/load/seed_loadtest.py")
        return 1
    reset_limits()
    seed = json.loads(SEED.read_text("utf-8"))
    if not seed["owners"] or "farm_stmt" not in seed["owners"][0]:
        print("the seed predates the statement markers — re-run python tests/load/seed_loadtest.py")
        return 1
    ip_n = iter(range(1, 250))

    def seeded(ident: dict, label: str) -> Seeded | None:
        c = Seeded(label, ident, f"10.79.0.{next(ip_n)}")
        return c if c.live() else None

    owners = [c for c in (seeded(o, f"owner {o['name']}") for o in seed["owners"][:14]) if c][:8]
    workers = [c for c in (seeded(w, f"worker {w['name']}") for w in seed["workers"][-40:] if "lead" not in w) if c][:14]
    # a load test promotes / demotes / pauses the seeded leaders: only those still holding their role, unpaused
    holding = {str(r[0]) for r in sql("SELECT id FROM farm_members WHERE role IN ('manager', 'accountant') AND status = 'active'")}
    leaders = [c for c in (seeded(w, f"{w['lead']['role']} {w['name']}") for w in seed["workers"]
                           if "lead" in w and w["lead"]["member_id"] in holding) if c]
    if len(owners) < 3 or len(workers) < 6:
        print(f"only {len(owners)} owners / {len(workers)} workers still signed in — re-run seed_loadtest.py")
        return 1
    A, B = owners[0], owners[1]
    fa, fb = A.ident, B.ident
    print(f"(accounts: {len(owners)} owners, {len(workers)} workers, {len(leaders)} managers/accountants — all live)")

    # ------------------------------------------------------------------ 1. statements
    print("1. statements show only one's own entries")
    r = A.c.get("/account/statement?month=all")
    ok(r.status_code == 200 and markers(LTS_RE, r.text) == {fa["stmt"]}, "owner's statement page: only their own line",
       (r.status_code, sorted(markers(LTS_RE, r.text))))
    r = A.c.get("/account/statement.csv")
    ok(r.status_code == 200 and markers(LTS_RE, r.text) == {fa["stmt"]}, "owner's statement CSV: only their own line",
       (r.status_code, sorted(markers(LTS_RE, r.text))))
    r = A.c.get(f"/account/statement?of={fa['farm_id']}&month=all")
    ok(r.status_code == 200 and markers(LTF_RE, r.text) == {fa["farm_stmt"]} and not markers(LTS_RE, r.text),
       "farm statement in the owner's account: only this farm's lines (no personal lines)", (r.status_code, sorted(markers(LTF_RE, r.text))))
    r = A.c.get(f"/farms/{fa['farm_id']}?tab=ledger&month=all")
    ok(r.status_code == 200 and markers(LTF_RE, r.text) == {fa["farm_stmt"]}, "farm transaction history: only this farm's lines",
       (r.status_code, sorted(markers(LTF_RE, r.text))))
    r = A.c.get(f"/farms/{fa['farm_id']}/ledger.csv")
    ok(r.status_code == 200 and markers(LTF_RE, r.text) == {fa["farm_stmt"]}, "farm ledger CSV: only this farm's lines",
       (r.status_code, sorted(markers(LTF_RE, r.text))))
    acct = A.c.get("/account").text
    ok(f"of={fa['farm_id']}" in acct and f"of={fb['farm_id']}" not in acct, "account page lists the owner's farm statement only")

    r = A.c.get(f"/account/statement?of={fb['farm_id']}&month=all")
    ok(r.status_code == 404 and fb["farm_stmt"] not in r.text, "another owner's farm statement → 404", r.status_code)
    r = A.c.get(f"/farms/{fb['farm_id']}?tab=ledger&month=all")
    ok(r.status_code in (303, 403, 404) and fb["farm_stmt"] not in r.text, "another farm's transaction history refused", r.status_code)
    r = A.c.get(f"/farms/{fb['farm_id']}/ledger.csv")
    ok(r.status_code in (403, 404) and fb["farm_stmt"] not in r.text, "another farm's ledger CSV refused", r.status_code)

    still_employee = {str(r[0]) for r in sql("SELECT id FROM farm_members WHERE role = 'employee' AND status = 'active'")}
    emp = next((w for w in workers if (w.ident.get("job") or {}).get("member_id") in still_employee), None)   # load tests promote
    if emp:
        jf = emp.ident["job"]["farm_id"]
        jstmt = next((o["farm_stmt"] for o in seed["owners"] if o["farm_id"] == jf), None)
        r = emp.c.get(f"/account/statement?of={jf}&month=all")
        ok(r.status_code == 404, "an employee never gets their farm's statement in the account", r.status_code)
        r = emp.c.get(f"/farms/{jf}?tab=ledger&month=all")
        ok(r.status_code in (200, 303, 403, 404) and jstmt not in r.text, "an employee's farm page never shows the farm's ledger", r.status_code)
        r = emp.c.get(f"/farms/{jf}/ledger.csv")
        ok(r.status_code in (403, 404), "an employee cannot download the farm's ledger", r.status_code)
        r = emp.c.get("/account/statement?month=all")
        ok(markers(LTS_RE, r.text) == {emp.ident["stmt"]} and not markers(LTF_RE, r.text), "an employee's own statement: only their line")
    for ld in leaders[:3]:
        lf = ld.ident["lead"]["farm_id"]
        lstmt = next((o["farm_stmt"] for o in seed["owners"] if o["farm_id"] == lf), None)
        role = ld.ident["lead"]["role"]
        r = ld.c.get(f"/account/statement?of={lf}")
        ok(r.status_code == 404, f"a {role} does not get the farm statement in their account (owners / co-owners only)", r.status_code)
        r = ld.c.get(f"/farms/{lf}?tab=ledger&month=all")
        ok(r.status_code == 200 and markers(LTF_RE, r.text) == {lstmt}, f"a {role} sees their farm's transaction history — that farm only",
           (r.status_code, sorted(markers(LTF_RE, r.text))))
        other = next(o for o in seed["owners"] if o["farm_id"] != lf)
        r = ld.c.get(f"/farms/{other['farm_id']}/ledger.csv")
        ok(r.status_code in (403, 404), f"a {role} cannot download another farm's ledger", r.status_code)

    # a co-owner sees the farm statement; once removed, at once no more
    co = next(w for w in workers if (w.ident.get("job") or {}).get("farm_id") != fa["farm_id"])
    mid = str(uuid.uuid4())
    sql("INSERT INTO farm_members (id, farm_id, user_id, role, salary_type, salary_amount, joined_on, status, source) "
        "VALUES (:i, :f, :u, 'co_owner', 'monthly', 0, (now() AT TIME ZONE 'Asia/Kolkata')::date, 'active', 'owner')",
        i=mid, f=fa["farm_id"], u=co.ident["user_id"])
    try:
        r = co.c.get(f"/account/statement?of={fa['farm_id']}&month=all")
        ok(r.status_code == 200 and markers(LTF_RE, r.text) == {fa["farm_stmt"]}, "a co-owner sees that farm's statement in the account",
           (r.status_code, sorted(markers(LTF_RE, r.text))))
        r = co.c.get("/account/statement?month=all")
        ok(markers(LTS_RE, r.text) == {co.ident["stmt"]} and not markers(LTF_RE, r.text), "…while their own statement stays their own")
    finally:
        sql("UPDATE farm_members SET status = 'ended', ended_on = (now() AT TIME ZONE 'Asia/Kolkata')::date, end_reason = 'test' "
            "WHERE id = :i", i=mid)
    r = co.c.get(f"/account/statement?of={fa['farm_id']}&month=all")
    ok(r.status_code == 404 and fa["farm_stmt"] not in r.text, "a removed co-owner loses the farm statement at once", r.status_code)
    ok(f"of={fa['farm_id']}" not in co.c.get("/account").text, "…and it is gone from their account page")

    # a banned owner keeps their personal statement but not the farm's money
    susp = owners[2]
    sql("UPDATE users SET status = 'banned' WHERE id = :u", u=susp.ident["user_id"])
    try:
        r = susp.c.get(f"/account/statement?of={susp.ident['farm_id']}&month=all")
        ok(r.status_code != 200 and susp.ident["farm_stmt"] not in r.text, "a banned owner cannot open the farm statement", r.status_code)
    finally:
        sql("UPDATE users SET status = 'active' WHERE id = :u", u=susp.ident["user_id"])

    guest = httpx.Client(base_url=BASE, timeout=60, headers={"X-Forwarded-For": "10.79.1.1"})
    for path in ("/account/statement", f"/account/statement?of={fa['farm_id']}", "/account/statement.csv", f"/farms/{fa['farm_id']}/ledger.csv"):
        r = guest.get(path)
        ok(r.status_code in (303, 401, 403) and not markers(LTF_RE, r.text) and not markers(LTS_RE, r.text), f"guest refused: {path}",
           r.status_code)

    print("   300 concurrent statement requests from 16 accounts")
    people = owners[:6] + workers[:10]
    jobs = []
    for c in people:
        jobs += [(c, "/account/statement?month=all", LTS_RE, {c.ident["stmt"]}), (c, "/account/statement.csv", LTS_RE, {c.ident["stmt"]})]
        if c.ident.get("farm_stmt"):
            jobs += [(c, f"/account/statement?of={c.ident['farm_id']}&month=all", LTF_RE, {c.ident["farm_stmt"]}),
                     (c, f"/farms/{c.ident['farm_id']}?tab=ledger&month=all", LTF_RE, {c.ident["farm_stmt"]})]
    jobs = (jobs * 8)[:300]
    random.shuffle(jobs)
    bad, errors, lock = [], [], threading.Lock()

    def hit(job) -> None:
        c, path, rx, want = job
        r = c.c.get(path)
        if r.status_code == 429:            # the CSV limit (10 a minute) — a refusal, not a leak
            return
        seen = markers(rx, r.text)
        with lock:
            if r.status_code >= 500:
                errors.append((c.name, path, r.status_code))
            elif seen != want:
                bad.append((c.name, path, r.status_code, sorted(seen), sorted(want)))

    with ThreadPoolExecutor(max_workers=48) as ex:
        list(ex.map(hit, jobs))
    ok(not bad, "no statement ever carried another account's or another farm's lines", bad[:4])
    ok(not errors, "no server errors under concurrency", errors[:4])

    # ------------------------------------------------------------------ 2. hostile links
    print("2. hostile links")
    fails = []
    for month in ("2026-11' OR '1'='1", "9999-99", "<script>alert(1)</script>", "-1", "2026-13", "0001-01", "all; DROP TABLE users",
                  "%00", "2026-11\r\nSet-Cookie: x=1", "../../etc/passwd", "1" * 400):
        for path in ("/account/statement", f"/account/statement?of={fa['farm_id']}", f"/farms/{fa['farm_id']}?tab=ledger"):
            r = A.c.get(path, params={"month": month})
            if r.status_code >= 500 or (r.status_code == 200 and not no_markup(r.text)) or "x=1" in r.headers.get("set-cookie", ""):
                fails.append((path, month[:30], r.status_code))
        r = A.c.get("/account/statement.csv", params={"month": month})
        if r.status_code >= 500:
            fails.append(("csv", month[:30], r.status_code))
    ok(not fails, "hostile month values: never a 500, never echoed as markup, no header injection", fails[:4])
    fails = []
    for page in ("-1", "0", "99999999999999999999", "1e9", "abc", "NaN", "2147483648", "-99999999999999999999"):
        for path in ("/account/statement", f"/account/statement?of={fa['farm_id']}", f"/farms/{fa['farm_id']}?tab=ledger",
                     "/community", "/policies", "/jobs", "/shop", "/notifications", "/orders"):
            r = A.c.get(path, params={"page": page})
            if r.status_code >= 500:
                fails.append((path, page, r.status_code))
    ok(not fails, "hostile page numbers on every paged list: never a 500", fails[:4])
    fails = []
    for of in ("' OR 1=1--", "../../etc/passwd", "00000000-0000-0000-0000-000000000000", "%00", "me' UNION SELECT 1--", fb["farm_id"],
               fb["farm_id"].upper(), "{" + fb["farm_id"] + "}", fb["farm_id"].replace("-", ""), f" {fb['farm_id']}", "ME", "<svg/onload=alert(1)>"):
        r = A.c.get("/account/statement", params={"of": of})
        if r.status_code not in (404, 422) or fb["farm_stmt"] in r.text or not no_markup(r.text):
            fails.append((of[:40], r.status_code))
    ok(not fails, "hostile / foreign statement ids (any spelling of a stranger's farm id): always 404", fails[:4])

    # ------------------------------------------------------------------ 3. stored markup + CSV formulas
    print("3. stored markup is stripped or escaped; CSV formulas are neutralised")
    from app.utils import today_ist

    evil_desc = '=HYPERLINK("http://evil.example/x","refund") <script>alert(4)</script><img src=x onerror=alert(5)>'
    _, g = A.action("ledger.add", {"farm_id": fa["farm_id"], "entry_type": "expense", "amount": 11, "category": "=cmd|' /C calc'!A0",
                                    "description": evil_desc, "occurred_on": str(today_ist())})
    row = sql("SELECT description, category FROM ledger_entries WHERE farm_id = :f AND amount = 11 ORDER BY created_at DESC LIMIT 1",
              f=fa["farm_id"])
    ok(bool(row) and "<" not in row[0][0], "markup stripped before the ledger line is stored", row[:1])
    page_html = A.c.get(f"/account/statement?of={fa['farm_id']}&month=all").text + A.c.get(f"/farms/{fa['farm_id']}?tab=ledger&month=all").text
    ok(no_markup(page_html), "statement pages show the line as text, never as markup")
    cells = [c for line in csv.reader(io.StringIO(A.c.get(f"/farms/{fa['farm_id']}/ledger.csv").text.lstrip("﻿"))) for c in line]
    risky = [c for c in cells if c[:1] in ("=", "+", "-", "@", "\t", "\r") and not re.fullmatch(r"-?\d+(\.\d+)?", c)]
    ok(not risky and any(c.startswith("'=HYPERLINK") for c in cells) and any(c.startswith("'=cmd") for c in cells),
       "farm ledger CSV: formula cells start with an apostrophe (spreadsheet won't run them)", risky[:3])

    fresh = Client("fresh-farmer")
    fresh.c.headers["X-Forwarded-For"] = "10.79.1.2"
    ph, pw = "8" + str(uuid.uuid4().int % 10**9).zfill(9), "736251"
    signup(fresh, "farmer", ph, "Meera Secure", pw, 18.52, 73.86, dob="1994-05-06")
    fresh.action("profile.update", {"skills": "Tractor <img src=x onerror=alert(1)><script>alert(2)</script>",
                                    "address": "<svg/onload=alert(3)>Ward 4"})
    db_row = sql("SELECT skills, address FROM users WHERE phone = :p", p="+91" + ph)[0]
    ok("<" not in (db_row[0] or "") + (db_row[1] or ""), "profile text: markup stripped before it is stored", db_row)
    ok(no_markup(fresh.c.get("/account").text), "account page shows profile text as text")
    prep = fresh.post("/api/actions/prepare", json={"type": "location.update", "payload": {
        "address": {"state": "Maharashtra", "district": "<b onmouseover=alert(6)>Pune</b>", "place": "Pune<script>alert(7)</script>"}}})
    ok(prep.status_code in (200, 400) and no_markup(prep.text), "typed address with markup: summary never echoes markup", prep.text[:160])
    if prep.status_code == 200:
        g = fresh.post(f"/api/actions/{prep.json()['action_id']}/go", json={})
        ok(g.status_code == 200, "typed address saved once the markup is gone", g.text[:160])
        loc = sql("SELECT city, district, state FROM users WHERE phone = :p", p="+91" + ph)[0]
        ok(all("<" not in (x or "") for x in loc), "saved place has no markup", loc)
    ok(no_markup(fresh.c.get("/account").text) and no_markup(fresh.c.get("/dashboard").text), "account + dashboard clean after a hostile address")
    r = fresh.post("/api/auth/location/typed", json={"state": "Punjab", "district": "<img src=x onerror=alert(8)>", "place": "Ludhiana"})
    ok(r.status_code in (200, 422, 429) and no_markup(r.text), "typed-address preview never echoes markup", r.text[:160])

    # ------------------------------------------------------------------ 4. cross-account ids
    print("4. cross-account ids and forged fields")
    b_branch = fb["branch_id"]
    _, r = A.action("ledger.add", {"farm_id": fa["farm_id"], "branch_id": b_branch, "entry_type": "income", "amount": 12, "category": "test",
                                    "description": "tagged onto another farm's branch", "occurred_on": str(today_ist())}, expect_ok=False)
    ok(r is not None and r.status_code == 404, "ledger.add with ANOTHER farm's branch → 404", getattr(r, "text", "")[:160])
    ok(not sql("SELECT 1 FROM ledger_entries WHERE branch_id = :b AND farm_id <> :f", b=b_branch, f=fb["farm_id"]),
       "no ledger line anywhere points at a branch of a different farm")
    _, g = A.action("ledger.add", {"farm_id": fa["farm_id"], "branch_id": fa["branch_id"], "entry_type": "income", "amount": 13,
                                    "category": "own", "description": "own branch is fine", "occurred_on": str(today_ist())})
    ok(g is not None and g.status_code == 200, "ledger.add with the farm's OWN branch still works")
    pol = {"policy_type": "health", "insurer": "Test Insurer", "policy_number": "XB-1", "sum_insured": 100000,
           "valid_from": str(today_ist()), "valid_to": str(today_ist().replace(year=today_ist().year + 1)), "upload_id": str(uuid.uuid4())}
    _, r = A.action("insurance.add", {"farm_id": fa["farm_id"], "branch_id": b_branch, "policy": pol}, expect_ok=False)
    ok(r is not None and r.status_code == 404, "insurance.add with ANOTHER farm's branch → 404", getattr(r, "text", "")[:160])
    ok(not sql("SELECT 1 FROM insurance_policies p JOIN branches b ON b.id = p.branch_id WHERE b.farm_id <> p.farm_id"),
       "no insurance policy anywhere points at a branch of a different farm")
    _, r = A.action("ledger.add", {"farm_id": fb["farm_id"], "entry_type": "income", "amount": 14, "category": "test",
                                    "description": "write into a stranger's ledger", "occurred_on": str(today_ist())}, expect_ok=False)
    ok(r is not None and r.status_code in (403, 404), "ledger.add on a stranger's farm refused", getattr(r, "status_code", None))
    _, r = A.action("shop.close", {"shop_id": fb["shop_id"], "cancel_open_orders": True}, expect_ok=False)
    ok(r is not None and r.status_code in (403, 404), "closing a stranger's shop refused", getattr(r, "status_code", None))
    mgr = next((ld for ld in leaders if ld.ident["lead"]["role"] == "manager"), None)
    if mgr:
        shop = next(o["shop_id"] for o in seed["owners"] if o["farm_id"] == mgr.ident["lead"]["farm_id"])
        _, r = mgr.action("shop.close", {"shop_id": shop, "cancel_open_orders": True}, expect_ok=False)
        ok(r is not None and r.status_code == 403, "a manager cannot close the farm's shop (owners and co-owners only)", getattr(r, "status_code", None))
    w = workers[-1]
    _, r = w.action("account.become_owner", {"email": f"x{uuid.uuid4().hex[:6]}@example.in", "current_password": "000000",
                                             "new_password": "Abcdef12345", "new_password_again": "Abcdef12345", "role": "superadmin"},
                    expect_ok=False)
    ok(r is not None and r.status_code in (400, 422), "owner upgrade with a forged role field refused", getattr(r, "status_code", None))
    for forged in ({"role": "superadmin"}, {"is_admin": True}, {"status": "active", "skills": "x"}, {"token_version": 0}, {"user_id": fa["user_id"]}):
        _, r = w.action("profile.update", forged, expect_ok=False)
        ok(r is not None and r.status_code in (400, 422), f"profile.update with {list(forged)[0]} refused", getattr(r, "status_code", None))
    ok(sql("SELECT role FROM users WHERE id = :u", u=w.ident["user_id"])[0][0] == "farmer", "the worker is still a plain farmer")
    prep = A.post("/api/actions/prepare", json={"type": "ledger.add", "payload": {
        "farm_id": fa["farm_id"], "entry_type": "income", "amount": 15, "category": "test", "description": "prepared by A, GO by B",
        "occurred_on": str(today_ist())}})
    ok(prep.status_code == 200, "A prepared a ledger line")
    if prep.status_code == 200:
        g = B.post(f"/api/actions/{prep.json()['action_id']}/go", json={})
        ok(g.status_code in (403, 404, 409), "B cannot confirm A's prepared action", g.status_code)
        ok(not sql("SELECT 1 FROM ledger_entries WHERE description = 'prepared by A, GO by B'"), "…and nothing was written")

    # ------------------------------------------------------------------ 4b. audit findings (fixed) stay fixed
    print("4b. cross-account audit: invites, AI context, banned owners, contacts, GSTIN")
    prep = A.post("/api/actions/prepare", json={"type": "member.invite", "payload": {
        "farm_id": fa["farm_id"], "branch_id": b_branch, "phone": "9" + str(uuid.uuid4().int % 10**9).zfill(9), "role": "manager",
        "salary_type": "monthly", "salary_amount": 30000}})
    ok(prep.status_code == 404, "inviting a MANAGER onto another farm's branch → 404 (was unchecked for leader roles)", prep.text[:160])
    prep = A.post("/api/actions/prepare", json={"type": "member.invite", "payload": {
        "farm_id": fa["farm_id"], "branch_id": fa["branch_id"], "phone": "9" + str(uuid.uuid4().int % 10**9).zfill(9), "role": "manager",
        "salary_type": "monthly", "salary_amount": 30000}})
    ok(prep.status_code == 200, "…the farm's own branch still works", prep.text[:160])

    working = {str(r[0]) for r in sql("SELECT user_id FROM farm_members WHERE status <> 'ended' "
                                      "AND role IN ('employee', 'supervisor', 'manager', 'accountant')")}
    sup = next(w for w in workers[6:] if not w.ident.get("job") and w.ident["user_id"] not in working)   # one working farm
    sid_ = str(uuid.uuid4())
    sql("INSERT INTO farm_members (id, farm_id, branch_id, user_id, role, salary_type, salary_amount, joined_on, status, source) "
        "VALUES (:i, :f, :b, :u, 'supervisor', 'daily', 520, (now() AT TIME ZONE 'Asia/Kolkata')::date, 'active', 'owner')",
        i=sid_, f=fa["farm_id"], b=fa["branch_id"], u=sup.ident["user_id"])
    try:
        sup_ctx, own_ctx = run_async(_ai_context, sup.ident["user_id"]), run_async(_ai_context, fa["user_id"])
        # their OWN pay ("unpaid … (overdue …)") is theirs to know; the farm-wide line "employees N, salary due …" is not
        ok("salary due" not in sup_ctx and not re.search(r"employees \d+", sup_ctx), "a supervisor's AI context has no farm salary totals",
           sup_ctx[:300])
        ok("salary due" in own_ctx, "…the owner's still has them")
    finally:
        sql("UPDATE farm_members SET status = 'ended', ended_on = (now() AT TIME ZONE 'Asia/Kolkata')::date, end_reason = 'test' WHERE id = :i",
            i=sid_)

    sql("UPDATE users SET status = 'banned' WHERE id = :u", u=susp.ident["user_id"])
    try:
        role, roles, alerted = run_async(_powers, susp.ident["user_id"], susp.ident["farm_id"])
        ok(role is None and not roles and not alerted, "a banned owner holds no farm role anywhere and gets no farm alerts", (role, roles, alerted))
    finally:
        sql("UPDATE users SET status = 'active' WHERE id = :u", u=susp.ident["user_id"])
    role, roles, alerted = run_async(_powers, susp.ident["user_id"], susp.ident["farm_id"])
    ok(role == "owner" and alerted, "…and gets them back once active again", (role, alerted))

    ok(sql("SELECT contact_visible FROM users WHERE phone = :p", p="+91" + ph)[0][0] is False, "a new account's phone / email start hidden")
    listed = sql("SELECT id, phone FROM users WHERE role = 'farmer' AND status = 'active' AND photo_verified ORDER BY created_at DESC LIMIT 1")
    if listed:
        lid, lphone = listed[0]
        sql("UPDATE users SET contact_visible = true WHERE id = :u", u=lid)
        try:
            buyer = Client("buyer")
            buyer.c.headers["X-Forwarded-For"] = "10.79.1.5"
            signup(buyer, "buyer", "7" + str(uuid.uuid4().int % 10**9).zfill(9), "Kavya Buyer", "481516", 19.07, 72.88)
            ok(f"tel:{lphone}" not in buyer.c.get("/community").text, "a buyer never sees farmers' phone numbers in the community")
            r = workers[0].c.get("/community")
            ok(r.status_code == 200 and f"tel:{lphone}" in r.text, "a farmer sees a fellow farmer who opted in",
               (r.status_code, workers[0].name, lphone, r.text[:160] if r.status_code != 200 else "listed but no Call button"))
        finally:
            sql("UPDATE users SET contact_visible = false WHERE id = :u", u=lid)
        ok(f"tel:{lphone}" not in workers[0].c.get("/community").text, "…and no longer once they opt out")
    else:
        print("     (community check skipped — no photo-verified farmer yet)")

    sq = seed["owners"][-1]                 # not one of the live accounts above
    sql("UPDATE farms SET status = 'pending_approval' WHERE id = :f", f=sq["farm_id"])
    run_async(_admin_reject, sq["farm_id"])
    v = sql("SELECT v.status, v.gstin FROM gstin_verifications v JOIN farms f ON f.gstin_verification_id = v.id WHERE f.id = :f",
            f=sq["farm_id"])[0]
    ok(v[0] == "rejected" and not sql("SELECT 1 FROM gstin_verifications WHERE gstin = :g AND status = 'verified'", g=v[1]),
       "rejecting a farm frees its GSTIN for the real business", v)

    # ------------------------------------------------------------------ 5. headers
    print("5. private pages, private downloads, HttpOnly session")
    r = A.c.get("/account/statement")
    h = {k.lower(): v for k, v in r.headers.items()}
    ok("no-store" in h.get("cache-control", "") and "nonce-" in h.get("content-security-policy", "") and h.get("x-frame-options") == "DENY"
       and h.get("x-content-type-options") == "nosniff", "statement page: no-store + CSP nonce + no framing + nosniff",
       {k: h.get(k) for k in ("cache-control", "x-frame-options", "x-content-type-options")})
    quiet = workers[-2]                 # A has used up its CSV allowance (10 a minute) in the concurrency run
    for who, path in ((quiet, "/account/statement.csv"), (A, f"/farms/{fa['farm_id']}/ledger.csv")):
        r = who.c.get(path)
        ok(r.status_code == 200, f"{path}: downloaded", r.status_code)
        h = {k.lower(): v for k, v in r.headers.items()}
        ok("no-store" in h.get("cache-control", "") and h.get("x-content-type-options") == "nosniff" and "attachment" in h.get("content-disposition", ""),
           f"{path}: private download (no-store, nosniff, attachment)", {k: h.get(k) for k in ("cache-control", "content-disposition")})
    probe = Client("cookie-probe")
    probe.c.headers["X-Forwarded-For"] = "10.79.1.3"
    probe.refresh("/login")
    r = probe.post("/api/auth/login", json={"phone": ph, "password": pw})
    sc = ";".join(v for k, v in r.headers.multi_items() if k.lower() == "set-cookie" and v.startswith("ks_session=")).lower()
    ok(r.status_code == 200 and "httponly" in sc and "samesite=lax" in sc, "session cookie is HttpOnly + SameSite", sc[:120])

    # ------------------------------------------------------------------ 6. abuse limits
    print("6. typed-address lookups are rate limited")
    spam = Client("lookup-spam")
    spam.c.headers["X-Forwarded-For"] = "10.79.1.4"
    spam.refresh("/signup")
    codes = [spam.post("/api/auth/location/typed", json={"state": "Maharashtra", "district": "Pune", "place": "Pune"}).status_code
             for _ in range(13)]
    ok(429 in codes or 403 in codes, "13 lookups in a minute from one address → limited", codes)

    # ------------------------------------------------------------------ 7. login redirect in a browser
    print("7. after login, ?next= only ever lands on this site (real browser)")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("     (skipped — Playwright not installed)")
        return finish(t0)
    host = urlparse(BASE).netloc
    cases = [("//evil.example/x", None), ("/\\evil.example/x", None), ("/%5Cevil.example/x", None), ("/\t/evil.example/x", None),
             ("/\n/evil.example/x", None), ("/.//evil.example/x", None), ("https://evil.example/x", None),
             ("javascript:alert(document.domain)", None), ("/account", "/account")]
    with sync_playwright() as pw_:
        browser = pw_.chromium.launch()
        for i, (nxt, want) in enumerate(cases):
            ctx = browser.new_context(extra_http_headers={"X-Forwarded-For": f"10.79.2.{i + 1}"})
            off_site, docs = [], []
            # navigations only (fonts / CDN scripts are not jumps): a page load on any other host = an open redirect
            ctx.on("request", lambda rq: None if rq.resource_type != "document" else
                   (off_site.append(rq.url) if urlparse(rq.url).netloc != host else docs.append(urlparse(rq.url).path)))
            page = ctx.new_page()
            dialogs = []
            page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
            page.goto(BASE + "/login?" + urlencode({"next": nxt}))
            page.fill("#login-form [name=phone]", ph)
            page.fill("#login-form [name=password]", pw)
            page.click("#login-form button[type=submit]")
            try:
                page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
                page.wait_for_load_state("load")
            except Exception:  # noqa: BLE001 — an off-site jump shows up below
                pass
            landed = urlparse(page.url)
            good = not off_site and not dialogs and landed.netloc == host and (want is None or want in docs)
            ok(good, f"next={nxt!r} → stayed on this site ({landed.path})", (off_site[:1], dialogs[:1], page.url))
            ctx.close()
        browser.close()
    return finish(t0)


if __name__ == "__main__":
    sys.exit(main())
