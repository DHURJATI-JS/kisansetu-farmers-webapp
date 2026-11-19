"""KisanSetu Locust suite — realistic journeys + adversarial "chaos" traffic for bug hunting.

    python tests/load/seed_loadtest.py                       # once: farms, shops, workers + Redis sessions
    KS_MODE=bughunt locust -f tests/load/locustfile.py --headless -u 120 -r 10 -t 5m
    KS_MODE=stress  locust -f tests/load/locustfile.py --headless          # 10-min step load (shape below)
    python scripts/run_loadtest.py --mode stress                          # all of the above + invariants + import

Rules of the game
* Every simulated person has their own IP (X-Forwarded-For from the trusted local proxy) and their own session, so
  per-IP / per-user rate limits behave as in production. Chaos users use 10.66.x.x so bans can be told apart.
* Any 5xx is a failure. Unexpected 4xx are failures too, except where the task expects them (e.g. 409 "out of stock"
  when buyers race for the last units, 403 when chaos omits the CSRF token).
* The AI is only ever asked; AI-proposed actions are never confirmed (post-run invariant: none executed).
"""
from __future__ import annotations

import itertools
import json
import os
import random
import re
import threading
import time
import uuid
from pathlib import Path

import gevent
from locust import HttpUser, LoadTestShape, between, constant, events, task
from locust.exception import StopUser

HERE = Path(__file__).resolve().parent
MODE = os.environ.get("KS_MODE", "stress")
RESULTS = Path(os.environ.get("KS_RESULTS", HERE / "results"))
STAMP = os.environ.get("KS_STAMP", time.strftime("%Y%m%d-%H%M%S"))
SEED = json.loads((HERE / ".seed.json").read_text("utf-8"))
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')
UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ORDER_ACT_RE = re.compile(r'"order_id": "(' + UUID_RE + r')", "to": "(\w+)"')
CANCEL_RE = re.compile(r'data-action="order.cancel" data-payload=\'\{"order_id": "(' + UUID_RE + r')"')

_ids = itertools.count(1)
_owner_ix = itertools.count()
_employed_ix, _seeker_ix, _buyer_ix, _chaos_ix, _switch_ix = (itertools.count() for _ in range(5))
_lock = threading.Lock()
CHECKS: dict[str, dict[str, int]] = {}   # custom correctness checks → results/<stamp>_checks.json
EMPLOYED = [w for w in SEED["workers"] if "job" in w]
LEADERS = [w for w in SEED["workers"] if "lead" in w]                 # seeded managers / accountants of team farms
TEAM_OWNERS = [o for o in SEED["owners"] if o.get("team")]
_SEEK = [w for w in SEED["workers"] if "job" not in w and "lead" not in w]
_team_ix, _lead_ix = itertools.count(), itertools.count()
ROLE_RE = re.compile(r'"expected_role": "(\w+)", "member_id": "(' + UUID_RE + r')"')
NOTICE_RE = re.compile(r'data-action="member\.(give_notice|cancel_notice)" data-payload=\'\{"member_id": "(' + UUID_RE + r')"\}\'')
SPAM = ["Send OTP now to claim lottery prize http://bit.ly/x http://a.b http://c.d",
        "WIN crypto bitcoin today <script>alert(1)</script> http://t.co/a http://e.f http://g.h",
        "you have won a lottery!! pay registration fee now http://cutt.ly/q http://i.j http://k.l"]
VERNACULAR = [("मुझे इस महीने का वेतन कब मिलेगा?", "hi"), ("எனக்கு அருகில் வேலை எப்படி கண்டுபிடிப்பது?", "ta"),
              ("mujhe paas mein kaam kaise milega bhai?", "hi-Latn"), ("ನನಗೆ ಹತ್ತಿರ ಕೆಲಸ ಬೇಕು", "kn"),
              ("আমার বেতন কবে পাব?", "bn"), ("When will I get my salary?", "en")]
SCREEN = ["my-job", "pay-stats", "contacts", "report"]
# disjoint identities per behaviour, so one user's rate limit / ban never leaks into another class
_n = len(_SEEK)
SEEKERS, BUYERS = _SEEK[: _n * 30 // 100], _SEEK[_n * 30 // 100: _n * 75 // 100]
CHAOS_IDS, SWITCH_IDS = _SEEK[_n * 75 // 100: _n * 90 // 100], _SEEK[_n * 90 // 100:]   # switchers sign in/out with passwords
UA_LAPTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
UA_PHONE = "Mozilla/5.0 (Android 15; Mobile; rv:144.0) Gecko/144.0 Firefox/144.0"
GREET_RE = re.compile(r'<h1 class="ks-title ks-h1 m-0">(.*?)</h1>', re.S)
ADDR_RE = re.compile(r"LTA-[WO]\d{3,4}")
NOTE_RE = re.compile(r"LTN-[WO]\d{3,4}")
EICAR = "".join(["X5O!P%@AP[4", chr(92), "PZX54(P^)7CC)7}", chr(36), "EICAR-STANDARD-", "ANTIVIRUS-TEST-FILE!", chr(36), "H+H*"]).encode()


def tiny_png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (90, 160, 60)).save(buf, "PNG")
    return buf.getvalue()


PNG = tiny_png()


def private_data_check(user, ident: dict) -> None:
    """Pages that show personal data must show ONLY this person's markers (address, notifications, identity)."""
    r = user.page("/account", "/account")
    if r.status_code == 200 and ident.get("addr"):
        check("no_foreign_account_data", set(ADDR_RE.findall(r.text)) == {ident["addr"]})
    r = user.page("/notifications", "/notifications")
    if r.status_code == 200 and ident.get("note"):
        check("no_foreign_account_data", set(NOTE_RE.findall(r.text)) <= {ident["note"]})
    r = user.req("GET", "/api/auth/me", "/api/auth/me")
    if r.status_code == 200:
        check("no_foreign_account_data", ((r.json() or {}).get("user") or {}).get("id") == ident["user_id"])
    if ident.get("stmt"):                     # the statement page and its CSV hold only this person's marker line
        r = user.page("/account/statement?of=me&month=all", "/account/statement")
        if r.status_code == 200:
            check("statement_shows_only_own_entries", set(STMT_RE.findall(r.text)) == {ident["stmt"]})
        r = user.req("GET", "/account/statement.csv", "/account/statement.csv", (200, 429))   # 10 a minute: a refusal, not a leak
        if r.status_code == 200:
            check("statement_shows_only_own_entries", set(STMT_RE.findall(r.text)) == {ident["stmt"]})
    if ident.get("farm_id") and ident.get("farm_stmt"):
        r = user.page(f"/account/statement?of={ident['farm_id']}&month=all", "/account/statement?of=[farm]")
        if r.status_code == 200:              # the owner's farm statement: this farm's marker, nobody else's
            check("farm_statement_shows_only_own_farm", set(FARM_STMT_RE.findall(r.text)) == {ident["farm_stmt"]})
        r = user.page(f"/farms/{ident['farm_id']}?tab=ledger&month=all", "/farms/[id]?tab=ledger")
        if r.status_code == 200:              # the same statement inside the farm's transaction history
            check("farm_statement_shows_only_own_farm", set(FARM_STMT_RE.findall(r.text)) == {ident["farm_stmt"]})
STMT_RE = re.compile(r"\bLTS-[OW]\d{3,4}\b")
FARM_STMT_RE = re.compile(r"\bLTF-O\d{3}\b")
TOKEN_RE = re.compile(r"\bL[wo]\d{3,4}\b")


def shows_only(html: str, name: str | None) -> bool:
    """The dashboard greeting is rendered from the request's own user: it must name this user and nobody else
    (seeded first names are unique tokens like Lw0007, so a cross-request leak would show a different token)."""
    if not name:
        return True
    m = GREET_RE.search(html or "")
    if not m:
        return False
    tokens = set(TOKEN_RE.findall(m.group(1)))
    return tokens == {name.split()[0]}
SHOPS = [{"shop_id": o["shop_id"], "farm_id": o["farm_id"], "products": o["products"], "jobs": o["jobs"], "lat": o.get("lat"),
          "lon": o.get("lon")} for o in SEED["owners"]]
HOT_SHOPS = SHOPS[:5]                      # most buyers race on a few shops' scarce stock
JOBS = [j for s in SHOPS for j in s["jobs"]]
QUESTIONS = ["Which crops grow well in November in Karnataka?", "मेरी टमाटर की पत्तियाँ पीली हो रही हैं, क्या करूँ?",
             "How do I report unpaid salary?", "What is PM-KISAN and am I eligible?", "Tips to save water for ragi?"]
# typed addresses already in the Redis lookup cache: the load test must never flood the public OpenStreetMap service
TYPED_PLACES = [{"state": "Maharashtra", "district": "Pune", "place": "Pune"},
                {"state": "Maharashtra", "district": "Nashik", "place": "Nashik"},
                {"state": "Punjab", "district": "Ludhiana", "place": "Ludhiana"},
                {"state": "Uttar Pradesh", "district": "Bareilly", "place": "Bareilly"}]
TRANSLATE = ["Field hand", "Harvest helper", "Seasonal farm work, 8 hours, water + lunch provided.", "Ragi flour", "Organic jaggery",
             "Tomatoes", "Groundnut oil", "Vermicompost", "Main field", "Banana (robusta)"]
NASTY = ["' OR 1=1 --", "\"; DROP TABLE users; --", "<script>alert(1)</script>", "%00%0d%0a", "../../etc/passwd", "😀🌾" * 40,
         "‮evil‬", "ठीक' UNION SELECT password_hash FROM users --", "${jndi:ldap://x}", "{{7*7}}", "a" * 600]


def check(name: str, ok: bool) -> None:
    with _lock:
        c = CHECKS.setdefault(name, {"runs": 0, "violations": 0})
        c["runs"] += 1
        c["violations"] += 0 if ok else 1


def take(seq: list, counter: itertools.count) -> dict:
    return seq[next(counter) % len(seq)]


class KSUser(HttpUser):
    abstract = True
    host = os.environ.get("KS_HOST", "http://127.0.0.1:8088")  # not "localhost": IPv6-first lookup costs 2 s per connect on Windows
    wait_time = between(1, 4)
    ip_prefix = "10.20"

    def on_start(self) -> None:
        n = next(_ids)
        self.ip = f"{self.ip_prefix}.{(n // 250) % 250}.{n % 250 + 1}"
        self.client.headers.update({"X-Forwarded-For": self.ip, "User-Agent": "Mozilla/5.0 (KisanSetu Locust load test)",
                                    "Accept-Language": "en-IN,en;q=0.8"})
        self.csrf = ""

    # ------------------------------------------------------------------ helpers
    def login_as(self, ident: dict) -> None:
        self.me = ident
        self.client.cookies.set("ks_session", ident["sid"])
        if ident.get("lang") and ident["lang"] != "en":
            self.client.cookies.set("ks_lang", ident["lang"])

    def req(self, method: str, path: str, name: str, expect: tuple[int, ...] = (200,), **kw):
        kw.setdefault("allow_redirects", False)
        with self.client.request(method, path, name=name, catch_response=True, **kw) as r:
            if r.status_code in expect:
                r.success()
            elif r.status_code >= 500 or r.status_code == 0:
                r.failure(f"HTTP {r.status_code}")
            else:
                r.failure(f"unexpected {r.status_code}: {(r.text or '')[:100]}")
            return r

    def page(self, path: str, name: str | None = None, expect: tuple[int, ...] = (200,)):
        r = self.req("GET", path, name or path, expect)
        m = CSRF_RE.search(r.text or "") if r.status_code == 200 else None
        if m:
            self.csrf = m.group(1)
        return r

    def post(self, path: str, name: str, payload: dict | None = None, expect: tuple[int, ...] = (200,), csrf: bool = True, **kw):
        headers = {"X-CSRF-Token": self.csrf} if csrf else {}
        return self.req("POST", path, name, expect, json=payload if payload is not None else {}, headers=headers, **kw)

    def action(self, type_: str, payload: dict, name: str | None = None, prep_ok: tuple[int, ...] = (200,),
               go_ok: tuple[int, ...] = (200,)):
        name = name or type_
        r = self.post("/api/actions/prepare", f"action prepare {name}", {"type": type_, "payload": payload}, prep_ok)
        if r.status_code != 200:
            return r, None
        g = self.post(f"/api/actions/{r.json()['action_id']}/go", f"action GO {name}", {}, go_ok)
        return r, g


# ====================================================================== anonymous visitors
class Visitor(KSUser):
    weight = 3
    ip_prefix = "10.10"

    def on_start(self) -> None:
        super().on_start()
        self.page("/")

    @task(4)
    def home(self):
        self.page("/", "/ (home)")

    @task(3)
    def schemes(self):
        self.page("/schemes")
        self.post("/api/schemes/check", "/api/schemes/check", {"age": random.randint(18, 70), "landholding_hectares": random.choice([0, 0.8, 3]),
                                                                "has_bank_account": True, "is_rural": True, "role": "farmer",
                                                                "state": random.choice(["Karnataka", "Punjab", None])})

    @task(2)
    def scheme_link(self):
        self.page(f"/schemes/go/{random.choice(['pm_kisan', 'pmfby', 'kcc', 'eshram', 'pmjay'])}", "/schemes/go/[slug]")

    @task(3)
    def jobs(self):
        self.page("/jobs")
        self.req("GET", f"/api/jobs/search?lat={12.9 + random.random() / 5:.4f}&lon={77.5 + random.random() / 5:.4f}&source=internal",
                 "/api/jobs/search")

    @task(3)
    def shop(self):
        self.page("/shop")
        s = random.choice(SHOPS)
        self.page(f"/shop/s/{s['shop_id']}", "/shop/s/[id]")
        self.page(f"/shop/p/{random.choice(s['products'])['id']}", "/shop/p/[id]")
        self.req("GET", f"/api/shop/products?q={random.choice(['ragi', 'oil', 'tomato', ''])}", "/api/shop/products")

    @task(1)
    def static_pages(self):
        for p in random.sample(["/login", "/signup", "/policies", "/legal/terms", "/legal/privacy", "/offline"], 2):
            self.page(p, p)
        self.req("GET", "/sw.js", "/sw.js")
        self.req("GET", "/healthz", "/healthz")


# ====================================================================== farm workers
class Worker(KSUser):
    weight = 5
    ip_prefix = "10.20"

    def on_start(self) -> None:
        super().on_start()
        self.login_as(take(EMPLOYED, _employed_ix) if random.random() < 0.6 else take(SEEKERS, _seeker_ix))
        self.surveyed = False
        self.page("/dashboard")

    @task(5)
    def dashboard(self):
        r = self.page("/dashboard")
        if r.status_code == 200:
            check("pages_only_show_own_account", shows_only(r.text, self.me.get("name")))

    @task(3)
    def jobs(self):
        self.page("/jobs")
        self.req("GET", f"/api/jobs/search?lat={self.me['lat']:.4f}&lon={self.me['lon']:.4f}", "/api/jobs/search")
        self.page(f"/jobs/{random.choice(JOBS)}", "/jobs/[id]")

    @task(2)
    def notifications(self):
        self.req("GET", "/api/notifications", "/api/notifications")
        self.page("/notifications")

    @task(2)
    def schemes(self):
        self.page("/schemes")
        self.post("/api/schemes/check", "/api/schemes/check", {"role": "farmer"})

    @task(1)
    def survey(self):
        job = self.me.get("job")
        if not job or self.surveyed:
            self.page("/account")
            return
        self.page("/survey")
        rating = random.choice(["good"] * 9 + ["bad"])
        answers = {"worked_today": True, **({"no_water": True} if rating == "bad" else {})}
        r = self.post(f"/api/welfare/surveys/{job['survey_id']}", "/api/welfare/surveys/[id]", {"rating": rating, "answers": answers},
                      expect=(200, 409))
        self.surveyed = r.status_code in (200, 409)

    @task(1)
    def apply_with_location_off(self):
        """Job seekers apply WITHOUT sharing a live position: the distance rule uses the saved (GPS or typed) location.
        Far-away jobs are refused with 400; near ones go through the GO step."""
        if self.me.get("job"):
            return
        job_id = random.choice(JOBS)
        self.page(f"/jobs/{job_id}", "/jobs/[id]")
        r = self.post("/api/actions/prepare", "action: job.apply (saved location)",
                      {"type": "job.apply", "payload": {"job_id": job_id, "accept_terms": True}}, expect=(200, 400, 403, 404, 409))
        if r.status_code == 200:
            self.post(f"/api/actions/{r.json()['action_id']}/go", "action GO: job.apply (saved location)", {}, expect=(200, 400, 409, 429))

    @task(1)
    def typed_location(self):
        """A typed address instead of GPS: confirm step only (cancelled), so seeded locations stay as they are."""
        r = self.post("/api/actions/prepare", "action: location.update (typed)",
                      {"type": "location.update", "payload": {"address": random.choice(TYPED_PLACES)}}, expect=(200, 400, 429))
        if r.status_code == 200:
            self.post(f"/api/actions/{r.json()['action_id']}/cancel", "/api/actions/[id]/cancel", {}, expect=(200, 204))

    @task(1)
    def translate(self):
        if self.me.get("lang", "en") != "en":
            self.post("/api/i18n/translate", "/api/i18n/translate", {"texts": random.sample(TRANSLATE, 4)})

    @task(1)
    def misc(self):
        self.page(random.choice(["/community", "/policies", "/report", "/orders"]), "/(community|policies|report|orders)")

    @task(2)
    def my_private_data(self):
        private_data_check(self, self.me)


# ====================================================================== farm owners
class Owner(KSUser):
    weight = 2
    ip_prefix = "10.30"

    def on_start(self) -> None:
        super().on_start()
        # team-farm owners belong to TeamChurn (one person, one simulated user: rate limits are per account)
        self.login_as(take([o for o in SEED["owners"] if not o.get("team")] or SEED["owners"], _owner_ix))
        self.page("/dashboard")

    @task(3)
    def dashboard(self):
        r = self.page("/dashboard")
        if r.status_code == 200:
            check("pages_only_show_own_account", shows_only(r.text, self.me.get("name")))

    @task(6)
    def farm_tabs(self):
        tab = random.choice(["overview", "team", "payroll", "ledger", "shop", "hiring", "insurance", "weather", "branches", "proposals"])
        r = self.page(f"/farms/{self.me['farm_id']}?tab={tab}", f"/farms/[id]?tab={tab}")
        if tab == "shop" and r.status_code == 200:
            self.process_orders(r.text)

    def process_orders(self, html: str) -> None:
        nxt = {"placed": "confirmed", "confirmed": "ready_for_pickup", "ready_for_pickup": "picked_up"}
        for oid, to in ORDER_ACT_RE.findall(html)[:2]:
            if to in nxt.values() or to == "dispatched":
                self.action("order.update", {"order_id": oid, "to": to}, "order.update", prep_ok=(200, 409), go_ok=(200, 409))

    @task(1)
    def my_private_data(self):
        private_data_check(self, self.me)

    @task(1)
    def ledger_csv(self):
        self.req("GET", f"/farms/{self.me['farm_id']}/ledger.csv", "/farms/[id]/ledger.csv")

    @task(2)
    def restock(self):
        """Optimistic concurrency: buyers keep changing the version, so a stale version → 409 is expected, then retry."""
        p = random.choice([x for x in self.me["products"] if not x["scarce"]])
        for _ in range(3):
            items = self.req("GET", f"/api/shop/products?shop={self.me['shop_id']}", "/api/shop/products").json().get("items", [])
            ver = next((i["version"] for i in items if i["id"] == p["id"]), None)
            if ver is None:
                return
            pr, g = self.action("inventory.adjust", {"product_id": p["id"], "delta": 5, "reason": "restock", "expected_version": ver},
                                "inventory.adjust", prep_ok=(200, 409, 429), go_ok=(200, 409, 429))
            if g is not None and g.status_code == 200:
                return
            if 429 in (pr.status_code, g.status_code if g is not None else 0):
                return   # rate-limited: a person would wait — stop retrying (the limiter is doing its job)

    @task(1)
    def crop_cycle(self):
        name = random.choice(["Ragi", "Maize", "Tomato", "Groundnut"])
        # 429 = this owner's 30 GO/minute limit (double-GO and restock tasks share it): graceful back-pressure, not a bug
        _, g = self.action("crop.add", {"farm_id": self.me["farm_id"], "branch_id": self.me["branch_id"], "crop": name}, "crop.add",
                           prep_ok=(200, 400, 409, 429), go_ok=(200, 429))
        if g is not None and g.status_code == 200:
            self.action("crop.remove", {"farm_id": self.me["farm_id"], "branch_id": self.me["branch_id"], "crop": name, "mode": "harvested"},
                        "crop.remove", prep_ok=(200, 400, 404, 409), go_ok=(200, 404, 409))

    @task(1)
    def double_go(self):
        """Two GO clicks at the same instant must execute the action exactly once."""
        r = self.post("/api/actions/prepare", "action prepare crop.add (double GO)",
                      {"type": "crop.add", "payload": {"farm_id": self.me["farm_id"], "branch_id": self.me["branch_id"], "crop": "Sorghum"}},
                      expect=(200, 400, 409))
        if r.status_code != 200:
            return
        aid = r.json()["action_id"]
        jobs = [gevent.spawn(self.post, f"/api/actions/{aid}/go", "action GO (double click)", {}, (200, 409)) for _ in range(2)]
        gevent.joinall(jobs, timeout=60)
        codes = sorted(j.value.status_code for j in jobs if j.value is not None)
        if 429 in codes:   # rate-limited click: inconclusive (the database invariant still proves nothing ran twice)
            return
        check("double_go_executes_once", codes == [200, 409])
        if codes and codes[0] == 200:
            self.action("crop.remove", {"farm_id": self.me["farm_id"], "branch_id": self.me["branch_id"], "crop": "Sorghum", "mode": "removed"},
                        "crop.remove", prep_ok=(200, 400, 404, 409), go_ok=(200, 404, 409))


# ====================================================================== buyers racing for scarce stock
class Buyer(KSUser):
    weight = 3
    ip_prefix = "10.40"

    def on_start(self) -> None:
        super().on_start()
        self.login_as(take(BUYERS, _buyer_ix))
        self.page("/shop")

    @task(3)
    def browse(self):
        s = random.choice(HOT_SHOPS + SHOPS[:10])
        self.page(f"/shop/s/{s['shop_id']}", "/shop/s/[id]")
        self.page(f"/shop/p/{random.choice(s['products'])['id']}", "/shop/p/[id]")

    def _order(self, shop: dict, items: list[dict], key: str | None = None):
        import math

        near = shop.get("lat") is not None and math.dist((shop["lat"], shop["lon"]), (self.me["lat"], self.me["lon"])) * 111 < 35
        return self.action("order.place", {"shop_id": shop["shop_id"], "items": items,
                                           "fulfilment": random.choice(["pickup", "delivery"]) if near else "pickup",
                                           "address": "Near the panchayat office, Hoskote", "lat": self.me["lat"], "lon": self.me["lon"],
                                           "idempotency_key": key or uuid.uuid4().hex},
                           "order.place", prep_ok=(200, 409), go_ok=(200, 409))

    @task(4)
    def race_for_last_units(self):
        """Multi-item orders on scarce products in RANDOM line order — the classic deadlock / oversell trap."""
        shop = random.choice(HOT_SHOPS)
        scarce = [p for p in shop["products"] if p["scarce"]]
        picks = random.sample(scarce, random.randint(1, len(scarce)))
        random.shuffle(picks)
        self._order(shop, [{"product_id": p["id"], "qty": random.randint(1, 3)} for p in picks])

    @task(1)
    def idempotent_replay(self):
        """A flaky network resends the same order: same idempotency key → one order, stock taken once."""
        shop = random.choice(SHOPS[5:])
        p = random.choice([x for x in shop["products"] if not x["scarce"]])
        key = uuid.uuid4().hex
        _, g1 = self._order(shop, [{"product_id": p["id"], "qty": 1}], key)
        _, g2 = self._order(shop, [{"product_id": p["id"], "qty": 1}], key)
        if g1 is not None and g2 is not None and g1.status_code == 200 and g2.status_code == 200:
            check("idempotent_order_replay", bool(g1.json().get("order_no")) and g1.json().get("order_no") == g2.json().get("order_no"))

    @task(1)
    def my_orders(self):
        r = self.page("/orders")
        ids = CANCEL_RE.findall(r.text or "")
        if ids and random.random() < 0.5:
            self.action("order.cancel", {"order_id": random.choice(ids)}, "order.cancel", prep_ok=(200, 409), go_ok=(200, 409))

    @task(1)
    def cart(self):
        self.page("/cart")


# ====================================================================== adversarial traffic
class Chaos(KSUser):
    weight = 1
    ip_prefix = "10.66"
    wait_time = between(0.5, 2)

    def req(self, method: str, path: str, name: str, expect: tuple[int, ...] = (200,), **kw):
        """Once the perimeter guard has banned this attacker's IP, every request gets 403 ip_banned — that is the
        defence working, so count it as expected and retire the attacker."""
        kw.setdefault("allow_redirects", False)
        with self.client.request(method, path, name=name, catch_response=True, **kw) as r:
            banned = r.status_code == 403 and "ip_banned" in (r.text or "")
            if r.status_code in expect or banned:
                r.success()
            elif r.status_code >= 500 or r.status_code == 0:
                r.failure(f"HTTP {r.status_code}")
            else:
                r.failure(f"unexpected {r.status_code}: {(r.text or '')[:100]}")
        if banned:
            check("attacker_ip_banned", True)
            raise StopUser()
        return r

    def on_start(self) -> None:
        super().on_start()
        self.login_as(take(CHAOS_IDS, _chaos_ix))
        self.page("/dashboard")

    @task(2)
    def missing_csrf(self):
        self.post("/api/actions/prepare", "chaos: no CSRF token", {"type": "crop.add", "payload": {}}, expect=(403,), csrf=False)

    @task(2)
    def malformed_json(self):
        self.req("POST", "/api/actions/prepare", "chaos: malformed JSON", (400, 422), data=b'{"type": "crop.add", "payload": {',
                 headers={"X-CSRF-Token": self.csrf, "Content-Type": "application/json"})

    @task(1)
    def oversized_body(self):
        self.req("POST", "/api/actions/prepare", "chaos: 13 MB body", (413,), data=b'{"x":"' + b"A" * (13 * 1024 * 1024) + b'"}',
                 headers={"X-CSRF-Token": self.csrf, "Content-Type": "application/json"})

    @task(2)
    def unknown_and_forbidden_actions(self):
        self.post("/api/actions/prepare", "chaos: unknown action", {"type": "system.shell", "payload": {"cmd": "rm -rf /"}}, expect=(404,))
        self.post("/api/actions/prepare", "chaos: admin action as worker", {"type": "admin.user.ban", "payload": {"user_id": str(uuid.uuid4()),
                                                                                                                   "reason": "x"}}, expect=(403,))
        self.post("/api/actions/prepare", "chaos: extra payload fields", {"type": "crop.add", "payload": {"crop": "Ragi", "is_admin": True}},
                  expect=(400,))

    @task(2)
    def go_someone_elses_action(self):
        self.post(f"/api/actions/{uuid.uuid4()}/go", "chaos: GO unknown/foreign action", {}, expect=(409,))

    @task(3)
    def missing_and_bad_ids(self):
        """Random / malformed ids must give 404 (or 422), never 500 — also for signed-in users."""
        for path, name in ((f"/jobs/{uuid.uuid4()}", "chaos: missing job"), (f"/shop/p/{uuid.uuid4()}", "chaos: missing product"),
                           (f"/shop/s/{uuid.uuid4()}", "chaos: missing shop"), (f"/f/{uuid.uuid4()}", "chaos: missing public farm"),
                           ("/jobs/not-a-uuid", "chaos: malformed job id"), ("/schemes/go/NOPE", "chaos: missing scheme"),
                           (f"/payments/{uuid.uuid4()}", "chaos: missing payment"), (f"/media/{uuid.uuid4()}", "chaos: missing media")):
            self.req("GET", path, name, (404, 422))

    @task(2)
    def idor(self):
        s = random.choice(SHOPS)
        self.req("GET", f"/farms/{s['farm_id']}?tab=payroll", "chaos: other farm's payroll", (403, 404))
        self.req("GET", f"/farms/{s['farm_id']}/ledger.csv", "chaos: other farm's ledger", (403, 404))
        self.req("GET", "/admin", "chaos: admin page", (403, 303))
        self.req("GET", "/admin/production-test", "chaos: production-test page", (403, 303))
        self.req("GET", "/internal/metrics", "chaos: internal metrics via proxy", (404,))

    @task(3)
    def injection_and_unicode(self):
        q = random.choice(NASTY)
        self.req("GET", "/api/jobs/search", "chaos: nasty job search", (200, 422), params={"q": q, "lat": "12.9", "lon": "77.5"})
        self.req("GET", "/api/shop/products", "chaos: nasty product search", (200, 422), params={"q": q, "sort": q[:20], "page": "-5"})
        self.req("GET", "/shop", "chaos: nasty shop page query", (200, 422), params={"q": q, "category": q[:30]})
        self.post("/api/schemes/check", "chaos: nasty scheme check", {"age": q, "state": q}, expect=(200, 422))

    @task(2)
    def statement_attacks(self):
        """Other people's statements: a stranger's farm, broken / hostile ids and months — never 200 with foreign data, never 500."""
        s = random.choice(SHOPS)
        self.req("GET", f"/account/statement?of={s['farm_id']}", "chaos: another farm's statement", (404,))
        for of in ("' OR 1=1--", "../../etc/passwd", "00000000-0000-0000-0000-000000000000", "%00", "me' UNION SELECT 1--"):
            self.req("GET", "/account/statement", "chaos: hostile statement id", (404, 422), params={"of": of})
        for month in ("2026-11' OR '1'='1", "9999-99", "<script>alert(1)</script>", "-1", "2026-13", "all; DROP TABLE users"):
            r = self.req("GET", "/account/statement", "chaos: hostile statement month", (200, 422), params={"month": month})
            if r.status_code == 200:
                check("no_reflected_markup", "<script>alert(1)" not in r.text)
        self.req("GET", f"/farms/{s['farm_id']}?tab=ledger", "chaos: another farm's transaction history", (403, 404))

    @task(1)
    def escalation_attempts(self):
        """Become owner with a forged role, close a stranger's shop, typed address carrying markup."""
        self.post("/api/actions/prepare", "chaos: owner upgrade with a forged role",
                  {"type": "account.become_owner", "payload": {"email": "x@example.in", "current_password": "x", "new_password": "Abcdef12345",
                                                                "new_password_again": "Abcdef12345", "role": "superadmin"}}, expect=(400, 403, 422))
        s = random.choice(SHOPS)
        self.post("/api/actions/prepare", "chaos: close a stranger's shop",
                  {"type": "shop.close", "payload": {"shop_id": s["shop_id"], "cancel_open_orders": True}}, expect=(403, 404))
        self.post("/api/auth/location/typed", "chaos: typed address with markup",
                  {"state": "Punjab", "district": "<img src=x onerror=alert(1)>", "place": "Ludhiana\u202e<script>"}, expect=(200, 422, 429))

    @task(1)
    def traversal(self):
        for path in ("/static/../app/config.py", "/static/..%2f..%2f.env", "/media/..%2f..%2fvar%2fdev_secrets.json", "/.env",
                     "/.git/config", "/static/%2e%2e/%2e%2e/.env"):
            self.req("GET", path, "chaos: path traversal", (400, 404))

    @task(1)
    def wrong_methods(self):
        self.req("DELETE", "/api/notifications", "chaos: wrong method", (403, 404, 405), headers={"X-CSRF-Token": self.csrf})
        self.req("PUT", "/api/actions/prepare", "chaos: wrong method", (403, 404, 405), headers={"X-CSRF-Token": self.csrf})

    @task(1)
    def malware_upload(self):
        self.req("POST", "/api/uploads", "chaos: upload malware (EICAR)", (422,), data={"kind": "plant"},
                 files={"file": ("leaf.png", EICAR, "image/png")}, headers={"X-CSRF-Token": self.csrf})

    @task(1)
    def polyglot_upload(self):
        r = self.req("POST", "/api/uploads", "chaos: upload polyglot PNG", (200,), data={"kind": "plant"},
                     files={"file": ("../../x\u202egnp.png", PNG + b"<html><script>alert(1)</script></html>", "image/png")},
                     headers={"X-CSRF-Token": self.csrf})
        if r.status_code == 200:
            m = self.req("GET", f"/media/{r.json().get('upload_id')}", "chaos: fetch own upload")
            check("uploads_are_rebuilt_clean", m.status_code == 200 and b"<script" not in m.content and "sandbox" in m.headers.get("content-security-policy", ""))

    @task(1)
    def scripted_pdf(self):
        body = b"%PDF-1.4\n1 0 obj << /OpenAction << /S /J#61vaScript /JS (app.alert(1)) >> >> endobj\n%%EOF"
        self.req("POST", "/api/uploads", "chaos: upload PDF with script", (400, 422), data={"kind": "insurance"},
                 files={"file": ("p.pdf", body, "application/pdf")}, headers={"X-CSRF-Token": self.csrf})

    @task(2)
    def hostile_text(self):
        raw = ('{"type":"profile.update","payload":{"skills":"Ploughing ' + uuid.uuid4().hex[:6]
               + '\\u202e\\u0000 <script>alert(1)</script>\\ud800<b>x</b>"}}').encode()
        r = self.req("POST", "/api/actions/prepare", "chaos: hostile text (bidi/NUL/surrogate/HTML)", (200,), data=raw,
                     headers={"X-CSRF-Token": self.csrf, "Content-Type": "application/json"})
        if r.status_code == 200:
            self.post(f"/api/actions/{r.json()['action_id']}/go", "chaos: GO hostile text", {}, expect=(200,))

    @task(1)
    def brute_force_login(self):
        """Password guessing from one IP: 401s, then 429 (rate limit), then a 403 IP ban — all expected."""
        for _ in range(4):
            r = self.post("/api/auth/login", "chaos: password guessing", {"phone": "9000000001", "password": uuid.uuid4().hex[:12]},
                          expect=(400, 401, 403, 429))
            if r.status_code == 403:
                check("bruteforce_eventually_banned", True)
                raise StopUser()


# ====================================================================== one login per account
class DeviceSwitch(KSUser):
    """The same account signs in on a laptop, then on a phone. The laptop must be signed out at once: its pending GO is
    refused with 401 (nothing changes - post-run invariant), and the phone keeps working."""
    weight = 1
    ip_prefix = "10.70"
    wait_time = between(12, 16)   # under the 8 logins/minute per-IP limit

    def on_start(self) -> None:
        from locust.clients import HttpSession

        super().on_start()
        self.acct = take(SWITCH_IDS, _switch_ix)
        self.client.headers["User-Agent"] = UA_LAPTOP
        self.phone = HttpSession(self.host, self.environment.events.request, self)
        self.phone.headers.update({"X-Forwarded-For": self.ip.replace("10.70.", "10.71.", 1), "User-Agent": UA_PHONE})

    def _csrf(self, session, label: str) -> str:
        with session.get("/login", name=f"switch: {label} opens /login", catch_response=True, allow_redirects=True) as r:
            if r.status_code == 200:
                r.success()
            else:
                r.failure(f"HTTP {r.status_code}")
            m = CSRF_RE.search(r.text or "")
        return m.group(1) if m else ""

    def _login(self, session, label: str) -> str:
        token = self._csrf(session, label)
        with session.post("/api/auth/login", json={"phone": self.acct["phone"][-10:], "password": "LoadTest2026x"},
                          headers={"X-CSRF-Token": token}, name=f"switch: {label} signs in", catch_response=True) as r:
            if r.status_code == 200:
                r.success()
            else:
                r.failure(f"unexpected {r.status_code}: {(r.text or '')[:80]}")
            signed_in = r.status_code == 200
        if not signed_in:
            return None
        page = session.get("/dashboard", name="switch: dashboard after sign-in")
        m = CSRF_RE.search(page.text or "")
        if page.status_code == 200:
            check("pages_only_show_own_account", shows_only(page.text, self.acct.get("name")))
        return m.group(1) if m else token

    @task
    def sign_in_elsewhere(self):
        try:
            self._cycle()
        finally:   # fresh connections next time (no stale keep-alive reuse)
            self.client.close()
            self.phone.close()

    def _cycle(self):
        # a new network for each switch: real people do not sign in ~260 times an hour from ONE address (the per-IP
        # login limit is 60/hour — the 3-hour soak hit it with a fixed address)
        n = next(_ids)
        self.client.headers["X-Forwarded-For"] = f"10.70.{(n // 250) % 250}.{n % 250 + 1}"
        self.phone.headers["X-Forwarded-For"] = f"10.71.{(n // 250) % 250}.{n % 250 + 1}"
        laptop_csrf = self._login(self.client, "laptop")
        if laptop_csrf is None:
            return
        marker = f"LT-REVOKED-{uuid.uuid4().hex[:10]}"
        action_id = None
        with self.client.post("/api/actions/prepare", json={"type": "profile.update", "payload": {"skills": marker}},
                              headers={"X-CSRF-Token": laptop_csrf}, name="switch: laptop prepares a change", catch_response=True) as r:
            if r.status_code == 200:
                r.success()
                action_id = r.json().get("action_id")
            else:
                r.failure(f"unexpected {r.status_code}")
        if self._login(self.phone, "phone") is None:
            if action_id:   # the laptop is legitimately still signed in: withdraw the change instead of confirming it
                self.client.post(f"/api/actions/{action_id}/cancel", json={}, headers={"X-CSRF-Token": laptop_csrf},
                                 name="switch: laptop cancels (phone sign-in failed)")
            return
        if action_id:
            with self.client.post(f"/api/actions/{action_id}/go", json={}, headers={"X-CSRF-Token": laptop_csrf},
                                  name="switch: signed-out laptop presses GO", catch_response=True) as g:
                body = {}
                try:
                    body = g.json() or {}
                except Exception:
                    pass
                refused = g.status_code == 401 and body.get("reason") == "replaced"
                if refused:
                    g.success()
                else:
                    g.failure(f"expected 401 replaced, got {g.status_code}")
                check("signed_out_device_cannot_change_anything", refused)
        with self.phone.get("/api/auth/me", name="switch: phone still signed in", catch_response=True) as r:
            good = False
            try:
                good = r.status_code == 200 and ((r.json() or {}).get("user") or {}).get("id") == self.acct["user_id"]
            except Exception:
                pass
            if good:
                r.success()
            else:
                r.failure(f"phone lost its session: {r.status_code}")
            check("newest_device_stays_signed_in", good)


# ====================================================================== AI assistant (slow, low rate)
class AIUser(KSUser):
    fixed_count = int(os.environ.get("KS_AI_USERS", "2"))
    wait_time = between(20, 40)
    ip_prefix = "10.50"

    def on_start(self) -> None:
        super().on_start()
        self.login_as(take(EMPLOYED, _employed_ix))
        self.page("/assistant")

    @task
    def ask(self):
        # 429/503 = "AI busy, try later" (graceful back-pressure) — acceptable, but never 5xx crashes / hangs
        msg, want = random.choice(VERNACULAR) if random.random() < .5 else (random.choice(QUESTIONS), None)
        r = self.post("/api/ai/chat", "/api/ai/chat", {"message": msg, "session": uuid.uuid4().hex[:32], "page_keys": SCREEN},
                      expect=(200, 429, 503), timeout=300)
        if r.status_code == 200:
            data = r.json()
            check("ai_reply_has_no_executed_actions", all("action_id" in a or "error" in a for a in data.get("actions", [])))
            check("ai_points_only_at_screen_parts", data.get("highlight") in (None, *SCREEN))
            if want:
                check("ai_answers_in_the_language_asked", data.get("lang") == want)


# ====================================================================== realtime (Engine.IO polling handshake)
class SocketUser(KSUser):
    weight = 1
    ip_prefix = "10.60"
    wait_time = between(3, 8)

    def on_start(self) -> None:
        super().on_start()
        self.login_as(take(EMPLOYED, _employed_ix))

    @task
    def connect_and_leave(self):
        import socketio

        t0, exc, acked = time.perf_counter(), None, False
        for attempt in range(2):   # the browser client reconnects on its own: one dropped handshake is not a failure
            cl = socketio.Client(reconnection=False)
            try:
                cl.connect(self.host, headers={"Cookie": f"ks_session={self.me['sid']}", "X-Forwarded-For": self.ip},
                           transports=["websocket"], wait_timeout=10)
                acked, exc = cl.connected, None
            except Exception as e:  # noqa: BLE001
                exc = e
            finally:
                try:
                    cl.disconnect()
                except Exception:
                    pass
            if acked:
                break
            gevent.sleep(1)
        self.environment.events.request.fire(request_type="WS", name="socket.io connect (websocket)",
                                             response_time=(time.perf_counter() - t0) * 1000, response_length=0, exception=exc,
                                             context={})
        check("socket_connect_ack", acked)


# ====================================================================== people decisions under load
class TeamChurn(KSUser):
    """Owners of team farms promote, demote, give and withdraw notices — racing their own managers, who act too."""
    fixed_count = len(TEAM_OWNERS)       # one simulated person per account (no shared per-account rate limits)
    ip_prefix = "10.35"
    wait_time = between(2, 5)

    def on_start(self) -> None:
        super().on_start()
        if not TEAM_OWNERS:
            raise StopUser()
        self.login_as(take(TEAM_OWNERS, _team_ix))
        self.team()

    def team(self) -> None:
        r = self.page(f"/farms/{self.me['farm_id']}?tab=team", "/farms/[id]?tab=team (owner)")
        html = r.text if r.status_code == 200 else ""
        self.roles = {mid: role for role, mid in ROLE_RE.findall(html)}
        self.notices = NOTICE_RE.findall(html)

    @task(3)
    def change_role(self):
        self.team()
        if not self.roles:
            return
        mid, role = random.choice(list(self.roles.items()))
        new = random.choice([r for r in ("manager", "accountant", "supervisor", "employee") if r != role])
        payload = {"member_id": mid, "role": new, "expected_role": role, "reason": "load test"}
        if new in ("employee", "supervisor"):
            payload["branch_id"] = self.me["branch_id"]
        # 400 = not eligible as a farm worker / hiring limit; 409 = changed meanwhile (a race we expect and must survive)
        self.action("member.change_role", payload, prep_ok=(200, 400, 403, 409), go_ok=(200, 400, 403, 409))

    @task(2)
    def notice_cycle(self):
        self.team()
        if not self.notices:
            return
        kind, mid = random.choice(self.notices)
        if kind == "give_notice":
            self.action("member.give_notice", {"member_id": mid, "reason": "load test"}, "member.give_notice (owner)",
                        prep_ok=(200, 403, 409), go_ok=(200, 403, 409))
        else:
            self.action("member.cancel_notice", {"member_id": mid}, "member.cancel_notice (owner)",
                        prep_ok=(200, 403, 409), go_ok=(200, 403, 409))


class Manager(KSUser):
    """Seeded managers / accountants run their team while the owner may demote them or pause them (notice) at any time."""
    fixed_count = min(len(LEADERS), 16)
    ip_prefix = "10.36"
    wait_time = between(2, 5)

    def on_start(self) -> None:
        super().on_start()
        if not LEADERS:
            raise StopUser()
        self.login_as(take(LEADERS, _lead_ix))
        self.page("/dashboard")

    def team(self):
        r = self.page(f"/farms/{self.me['lead']['farm_id']}?tab=team", "/farms/[id]?tab=team (manager)", expect=(200, 303))
        if r.status_code == 303:   # demoted to a worker role, or paused by an individual notice: no team powers
            check("paused_or_demoted_leader_sent_to_public_page", r.headers.get("location", "").startswith("/f/"))
        return r

    @task(3)
    def team_tab(self):
        self.team()

    @task(2)
    def payroll(self):
        self.page(f"/farms/{self.me['lead']['farm_id']}?tab=payroll", "/farms/[id]?tab=payroll (manager)", expect=(200, 303))

    @task(2)
    def act_on_team(self):
        r = self.team()
        targets = NOTICE_RE.findall(r.text or "") if r.status_code == 200 else []
        if not targets:
            return
        kind, mid = random.choice(targets)
        name = f"member.{kind} (manager)"
        self.action(f"member.{kind}", {"member_id": mid, "reason": "load test"} if kind == "give_notice" else {"member_id": mid},
                    name, prep_ok=(200, 403, 409), go_ok=(200, 403, 409))

    @task(1)
    def notice_a_peer(self):
        """Straight at the API (no button is shown for peers): the server must refuse unless the peer was demoted meanwhile."""
        farm = next((o for o in TEAM_OWNERS if o["farm_id"] == self.me["lead"]["farm_id"]), None)
        peers = [m for m in (farm or {}).get("leaders", []) if m != self.me["lead"]["member_id"]]
        if peers:
            self.action("member.give_notice", {"member_id": random.choice(peers), "reason": "peer"}, "member.give_notice (peer)",
                        prep_ok=(200, 403, 409), go_ok=(200, 403, 409))

    @task(1)
    def change_roles_is_refused(self):
        r, _ = self.action("member.change_role", {"member_id": self.me["lead"]["member_id"], "role": "manager", "expected_role": "accountant"},
                           "member.change_role (by manager)", prep_ok=(403,), go_ok=(403,))
        check("only_owners_change_roles", r.status_code == 403)


# ====================================================================== spam → strikes → bans
class Spammer(KSUser):
    """Anonymous spam from its own IP range (10.67.x.x): blocked at once, judged, banned; the ban then holds."""
    weight = 1
    ip_prefix = "10.67"
    wait_time = between(3, 8)

    def on_start(self) -> None:
        super().on_start()
        self.banned, self.sent = False, {}
        self.page("/", "/ (spammer)", expect=(200, 403))

    @task
    def spam(self):
        if self.banned:
            r = self.page("/", "/ (banned spammer)", expect=(200, 403))
            if r.status_code == 200:      # the 1-hour ban ran out: a fresh start
                self.banned, self.sent = False, {}
            return
        msg = random.choice(SPAM)
        n = self.sent[msg] = self.sent.get(msg, 0) + 1
        # the FIRST copies may be answered (a farmer can paste a scam to ask "is this real?"); from the 3rd identical copy
        # on, the repetition rule must block it whatever the AI thinks of the words
        r = self.post("/api/ai/chat", "spam → /api/ai/chat", {"message": msg, "session": "spam"},
                      expect=(200, 400, 403, 429) if n < 3 else (400, 403, 429))
        if r.status_code == 403 and "ip_banned" in (r.text or ""):
            self.banned = True
            check("spammer_ip_banned", True)
        elif n >= 3 and r.status_code != 403:
            check("repeated_spam_blocked", r.status_code in (400, 429))


# ====================================================================== the guide (catalog, terms explained)
class GuideUser(KSUser):
    weight = 1
    ip_prefix = "10.37"
    wait_time = between(5, 15)

    @task(3)
    def catalog(self):
        lang = random.choice(["en", "hi", "ta", "bn", "kn"])
        r = self.req("GET", f"/api/guide/catalog?lang={lang}", "/api/guide/catalog")
        if r.status_code == 200:
            check("guide_catalog_has_tours_and_words", bool(r.json().get("guide")) and len(r.json().get("gloss", [])) >= 25)

    @task(2)
    def explain_terms(self):
        farm = random.choice(TEAM_OWNERS[:2] or SEED["owners"]) if random.random() < .5 else random.choice(SEED["owners"])
        self.page(f"/f/{farm['farm_id']}", "/f/[id]")
        r = self.post("/api/terms/explain", "/api/terms/explain", {"farm_id": farm["farm_id"]}, expect=(200, 429), timeout=240)
        if r.status_code == 200 and farm in TEAM_OWNERS[:2]:
            check("terms_explainer_never_waits_for_a_busy_ai", r.elapsed.total_seconds() < 60 or bool(r.json().get("points")))
            ids = {x["id"] for x in r.json().get("risks", [])}
            check("risky_terms_always_flagged", {"deposit", "wage_held", "deductions"} <= ids)


# ====================================================================== load shapes
class StepLoad(LoadTestShape):
    """10-minute step load: 25 → 75 → 150 → 200 → 250 concurrent users (AI users on top)."""
    stages = [(60, 25), (150, 75), (270, 150), (390, 200), (600, 250)]
    spawn_rate = 10

    def tick(self):
        if MODE != "stress":
            return None
        t = self.get_run_time()
        for end, users in self.stages:
            if t < end:
                return users, self.spawn_rate
        return None


class SoakLoad(LoadTestShape):
    """Long soak (default 3 h): a steady crowd with a 3-minute spike every 20 minutes (AI users on top)."""
    duration = int(os.environ.get("KS_DURATION", "10800"))
    base, peak = int(os.environ.get("KS_SOAK_USERS", "110")), int(os.environ.get("KS_SOAK_PEAK", "220"))

    def tick(self):
        if MODE != "soak":
            return None
        t = self.get_run_time()
        if t >= self.duration:
            return None
        if t < 120:
            return max(5, int(self.base * t / 120)), 5
        return (self.peak if (t - 120) % 1200 >= 1020 else self.base), 10


if MODE != "stress":
    del StepLoad
if MODE != "soak":
    del SoakLoad
if MODE == "bughunt":
    Chaos.weight = 3        # bug-hunt mode: CLI -u/-r/-t apply; boost the adversarial share


# ====================================================================== server/resource sampler
SAMPLES: list[dict] = []


def _server_proc():
    """The uvicorn supervisor (or single process) serving the port; workers are its children."""
    import psutil

    port = int(Visitor.host.rsplit(":", 1)[-1].split("/")[0]) if Visitor.host.count(":") == 2 else 80
    for c in psutil.net_connections(kind="tcp"):
        if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
            p = psutil.Process(c.pid)
            try:
                parent = p.parent()
                if parent and "run.py" in " ".join(parent.cmdline()):
                    return parent
            except Exception:
                pass
            return p
    return None


def _tree(proc):
    try:
        return [proc] + proc.children(recursive=True)
    except Exception:
        return [proc]


def _sampler(environment) -> None:
    import psutil
    import requests

    proc = None if os.environ.get("KS_REMOTE") == "1" else _server_proc()
    me = psutil.Process()
    seen: dict[int, object] = {}
    if proc:
        for p in _tree(proc):
            p.cpu_percent(None)
            seen[p.pid] = p
    me.cpu_percent(None)
    psutil.cpu_percent(None)
    t0 = time.time()
    while True:
        gevent.sleep(5)
        s = {"t": round(time.time() - t0, 1), "ts": round(time.time(), 1), "users": environment.runner.user_count if environment.runner else 0}
        st = environment.stats.total
        s["rps"] = round(st.current_rps, 2)
        s["fail_s"] = round(st.current_fail_per_sec, 2)
        try:
            if proc:
                procs = []
                for p in _tree(proc):                 # keep the same objects so cpu_percent has a baseline
                    if p.pid not in seen:
                        p.cpu_percent(None)
                        seen[p.pid] = p
                    procs.append(seen[p.pid])
                s["cpu"] = round(sum(p.cpu_percent(None) for p in procs), 1)
                s["rss_mb"] = round(sum(p.memory_info().rss for p in procs) / 2**20, 1)
                s["processes"] = len(procs)
        except Exception:
            proc = _server_proc()
        s["locust_cpu"] = round(me.cpu_percent(None), 1)
        s["host_cpu"] = round(psutil.cpu_percent(None), 1)
        try:
            m = requests.get(Visitor.host + "/internal/metrics", timeout=4).json()
            if not proc and m.get("cpu_s") is not None:              # remote / containerised server
                now_t = time.time()
                if getattr(environment, "ks_last_cpu", None):
                    dt = now_t - environment.ks_last_cpu[1]
                    s["cpu"] = round(max(0.0, (m["cpu_s"] - environment.ks_last_cpu[0]) / dt * 100), 1) if dt > 0 else None
                environment.ks_last_cpu = (m["cpu_s"], now_t)
                s["rss_mb"] = m.get("rss_mb")
                s["processes"] = m.get("workers")
            s.update(db_out=m["db_pool"]["checked_out"], db_overflow=m["db_pool"]["overflow"], ai_queued=m["ai_queue"]["queued"],
                     ai_active=m["ai_queue"]["active"], loop_lag_ms=m["loop_lag_ms"], loop_lag_max_ms=m["loop_lag_max_ms"],
                     workers=m.get("workers", 1))
        except Exception as exc:
            s["metrics_error"] = str(exc)[:80]
        SAMPLES.append(s)


@events.test_start.add_listener
def _start(environment, **_kw):
    RESULTS.mkdir(parents=True, exist_ok=True)
    environment.ks_sampler = gevent.spawn(_sampler, environment)


@events.test_stop.add_listener
def _stop(environment, **_kw):
    if getattr(environment, "ks_sampler", None):
        environment.ks_sampler.kill()
    (RESULTS / f"{STAMP}_server.json").write_text(json.dumps(SAMPLES), "utf-8")
    (RESULTS / f"{STAMP}_checks.json").write_text(json.dumps(CHECKS, indent=1), "utf-8")
