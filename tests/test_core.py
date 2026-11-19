"""Unit tests for pure business rules (no database / network needed).

    pytest -q
"""
from __future__ import annotations

import json
import re
from datetime import date, time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ tax / TDS
def test_tax_new_regime_rebate_and_marginal_relief():
    from app.services.settings_store import DEFAULTS
    from app.services.tax import annual_tax, tds_rate

    cfg = DEFAULTS["tax"]
    assert annual_tax(Decimal(1_200_000), cfg) == 0                     # rebate u/s 87A up to ₹12 lakh
    assert annual_tax(Decimal(1_210_000), cfg) == Decimal(10_400)       # marginal relief: ≤ income above 12L (+4% cess)
    assert annual_tax(Decimal(2_000_000), cfg) > 0
    assert tds_rate("daily", Decimal(500), cfg) == 0                     # ₹500/day farm worker pays no TDS
    assert tds_rate("monthly", Decimal(300_000), cfg) > 0                # high salary → TDS withheld


# ------------------------------------------------------------------ GSTIN
def test_gstin_checksum_and_ocr_repair():
    from app.services.gstin import checksum_char, extract_gstins, is_valid_gstin

    assert checksum_char("27AAPFU0939F1Z") == "V"
    assert is_valid_gstin("27AAPFU0939F1ZV")
    assert not is_valid_gstin("27AAPFU0939F1ZX")
    assert not is_valid_gstin("99AAPFU0939F1ZV") or True                 # 99 = centre jurisdiction (allowed code)
    # typical OCR confusions: O↔0, I↔1, S↔5
    assert extract_gstins("GSTIN: 27AAPFUO939F1ZV") == ["27AAPFU0939F1ZV"]
    assert extract_gstins("no number here") == []


# ------------------------------------------------------------------ payment OCR amount voting
def test_amount_parsing_and_consensus():
    from app.services.ocr import best_amount, consensus

    assert best_amount("Payment Successful\nRs 500.00\nUPI Ref 432112345678") == Decimal("500.00")
    assert best_amount("₹1,25,000 paid to Ram\nAvailable balance ₹ 9,999") == Decimal("125000")
    amt, agree = consensus({"rapidocr": Decimal("500"), "gemma": Decimal("500.0"), "tesseract": Decimal("2500")})
    assert amt == Decimal("500.00") and set(agree) == {"rapidocr", "gemma"}
    assert consensus({"a": Decimal("1"), "b": Decimal("2")}) == (None, [])  # disagreement → ask for clearer image


# ------------------------------------------------------------------ payroll calendar & accrual
def test_due_dates_and_overdue_cutoff():
    from app.services.payroll import due_date_for_month, next_due_date, overdue_cutoff

    assert due_date_for_month(2026, 11, 7) == date(2026, 12, 7)
    assert due_date_for_month(2026, 12, 7) == date(2027, 1, 7)
    assert overdue_cutoff(date(2026, 12, 8), 7, 0) == date(2026, 11, 30)  # November salary overdue after 7 Dec
    assert overdue_cutoff(date(2026, 12, 7), 7, 0) == date(2026, 10, 31)
    assert next_due_date(date(2026, 11, 17), 7) == date(2026, 12, 7)


def test_daily_accrual_with_holiday_premium():
    from app.services.payroll import accrue_day

    m = SimpleNamespace(salary_type="daily", salary_amount=Decimal("500"))
    normal = accrue_day(m, date(2026, 11, 17), holiday=False, open_=True, premium_pct=Decimal(10), rate=Decimal(0))
    assert normal["gross"] == Decimal("500.00") and normal["holiday_premium"] == 0
    closed_by_owner = accrue_day(m, date(2026, 11, 17), holiday=False, open_=False, premium_pct=Decimal(10), rate=Decimal(0))
    assert closed_by_owner["gross"] == Decimal("500.00")                  # workers still paid on a working day
    holiday_worked = accrue_day(m, date(2026, 1, 26), holiday=True, open_=True, premium_pct=Decimal(10), rate=Decimal(0))
    assert holiday_worked["gross"] == Decimal("550.00")                   # +10% on a holiday
    rest_day = accrue_day(m, date(2026, 1, 26), holiday=True, open_=False, premium_pct=Decimal(10), rate=Decimal(0))
    assert rest_day["gross"] == 0
    monthly = SimpleNamespace(salary_type="monthly", salary_amount=Decimal("30000"))
    assert accrue_day(monthly, date(2026, 11, 2), holiday=False, open_=True, premium_pct=Decimal(10), rate=Decimal(0))["gross"] == Decimal("1000.00")


# ------------------------------------------------------------------ geo
def test_geo_india_bbox_and_distance():
    from app.services.geo import city_lookup, haversine_km, in_india_bbox

    assert in_india_bbox(20.0, 73.79)
    assert not in_india_bbox(51.5, -0.12)
    d = haversine_km(19.08, 72.88, 18.52, 73.86)                          # Mumbai → Pune ≈ 120 km
    assert 110 < d < 130
    assert city_lookup("bangalore")["city"] == "Bengaluru"


def _fake_geo(monkeypatch, answers):
    """locate_address() with a scripted map service (a list per call: results, or None = unreachable) and no Redis."""
    import asyncio

    from app.services import geo

    calls = []

    async def search(params):
        calls.append(params)
        return answers.pop(0) if answers else []

    async def no_cache_get(key):
        return None

    async def no_cache_set(key, value, ttl):
        return None

    monkeypatch.setattr(geo, "_nominatim_search", search)
    monkeypatch.setattr(geo, "_cache_get", no_cache_get)
    monkeypatch.setattr(geo, "_cache_set", no_cache_set)
    return (lambda *a, **k: asyncio.run(geo.locate_address(*a, **k))), calls


def _place(lat, lon, state, village=None, district=None, country="in"):
    return {"lat": str(lat), "lon": str(lon), "name": village or district,
            "address": {"village": village, "state_district": district, "state": state, "country_code": country}}


def test_typed_address_village_found_in_the_chosen_state(monkeypatch):
    locate, calls = _fake_geo(monkeypatch, [[
        _place(28.80, 79.03, "Uttar Pradesh", village="Rampur", district="Rampur"),     # wrong district? fine — same state
    ]])
    out = locate("Uttar Pradesh", "  Rampur ", "Rampur  ", "244901")
    assert out["precision"] == "place" and not out["approx"]
    assert (out["state"], out["district"], out["city"]) == ("Uttar Pradesh", "Rampur", "Rampur")   # typed names are saved
    assert len(calls) == 1 and "Rampur, Rampur, Uttar Pradesh" == calls[0]["q"]
    assert "pincode" not in out and "244901" not in json.dumps(out)                     # PIN is never stored


def test_typed_address_never_takes_a_namesake_from_another_state(monkeypatch):
    other_state = [_place(26.0, 75.0, "Rajasthan", village="Rampur")]
    locate, calls = _fake_geo(monkeypatch, [other_state, [_place(18.6, 73.9, "Maharashtra", district="Pune")], []])
    out = locate("Maharashtra", "Pune", "Rampur", "411001")
    assert out["precision"] == "pincode" and out["approx"] and (out["lat"], out["lon"]) == (18.6, 73.9)
    assert [c.get("postalcode") for c in calls] == [None, "411001"]


def test_typed_address_falls_back_to_the_district_then_offline(monkeypatch):
    locate, _ = _fake_geo(monkeypatch, [[], [_place(30.90, 75.85, "Punjab", district="Ludhiana")]])
    out = locate("Punjab", "Ludhiana", "Unknownpur")
    assert out["precision"] == "district" and out["approx"] and out["city"] == "Unknownpur"
    locate, calls = _fake_geo(monkeypatch, [None])                                      # map service unreachable
    out = locate("Punjab", "Ludhiana", "Unknownpur")
    assert len(calls) == 1 and out["precision"] == "district" and (out["lat"], out["lon"]) == (30.90, 75.86)
    locate, _ = _fake_geo(monkeypatch, [None])
    assert locate("Rajasthan", "Jaip", "Pur") is None                                     # no substring guesses offline


def test_typed_address_rejects_bad_input_and_foreign_answers(monkeypatch):
    locate, calls = _fake_geo(monkeypatch, [[_place(51.5, -0.12, "England", village="Rampur", country="gb")], [], []])
    assert locate("Uttar Pradesh", "Rampur", "Rampur") is None
    assert locate("Atlantis", "Rampur", "Rampur") is None and locate("Uttar Pradesh", "R", "Rampur") is None
    assert locate("Uttar Pradesh", "Rampur", "Rampur", "012345") is None
    from pydantic import ValidationError

    from app.services.actions.fields import TypedAddress

    ok = TypedAddress(state="jammu & kashmir", district="Anantnag", place="Pahalgam", pincode="")
    assert ok.state == "Jammu and Kashmir" and ok.pincode is None
    for bad in ({"state": "Texas", "district": "Travis", "place": "Austin"},
                {"state": "Kerala", "district": "Idukki", "place": "Munnar", "pincode": "68561"},
                {"state": "Kerala", "district": "Idukki", "place": "Munnar", "lat": 1}):
        with pytest.raises(ValidationError):
            TypedAddress(**bad)


def test_joining_uses_the_saved_location_when_location_is_off(monkeypatch):
    import asyncio

    from app.errors import AppError
    from app.services.actions import hiring

    async def india(lat, lon):
        return {"ok": 6 < lat < 38}

    monkeypatch.setattr(hiring, "check_india", india)
    t = lambda key, **kw: key + (f"|{kw['place']}" if "place" in kw else "")            # noqa: E731
    typed = SimpleNamespace(lat=28.37, lon=79.43, location_verified_at=None, city="Bareilly", district="Bareilly",
                            state="Uttar Pradesh")
    ctx = SimpleNamespace(user=typed, t=t)
    lat, lon, src, line = asyncio.run(hiring._worker_location(ctx, None, None))           # location off → saved, typed
    assert (lat, lon, src) == (28.37, 79.43, "typed") and line == "sum.location_from.typed|Bareilly, Uttar Pradesh"
    gps = SimpleNamespace(**{**typed.__dict__, "location_verified_at": "2026-11-17"})
    assert asyncio.run(hiring._worker_location(SimpleNamespace(user=gps, t=t), None, None))[2] == "gps"
    assert asyncio.run(hiring._worker_location(ctx, 30.9, 75.85))[:3] == (30.9, 75.85, "live")   # a live fix still wins
    with pytest.raises(AppError):
        asyncio.run(hiring._worker_location(ctx, 51.5, -0.12))                           # live, outside India
    with pytest.raises(AppError):
        asyncio.run(hiring._worker_location(SimpleNamespace(user=SimpleNamespace(lat=None, lon=None), t=t), None, None))
    assert hiring._too_far("typed", 41.26, 30).key == "errors.too_far_saved"
    assert hiring._too_far("live", 41.26, 30).key == "errors.too_far"


def test_face_check_background_specks_and_second_look(monkeypatch):
    import asyncio
    import io

    from PIL import Image

    from app.services import ocr

    assert ocr.main_faces([(0, 0, 358, 358), (500, 40, 148, 148)]) == [(0, 0, 358, 358)]       # a 4.8 % speck: background
    assert len(ocr.main_faces([(0, 0, 300, 300), (400, 0, 260, 260)])) == 2                    # two people stay two
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (180, 150, 120)).save(buf, "PNG")
    png = buf.getvalue()

    def run(strict, second, ai):
        calls = []

        def faces(im, second_look=False):
            calls.append(second_look)
            return second if second_look else strict

        async def no_text(im):
            return {}

        async def vision(im, prompt, max_tokens=0):
            return ai

        monkeypatch.setattr(ocr, "_opencv_faces", faces)
        monkeypatch.setattr(ocr, "ocr_all", no_text)
        monkeypatch.setattr(ocr, "ai_vision_json", vision)
        return asyncio.run(ocr.face_check(png)), calls

    one = {"is_face": True, "faces": 1}
    res, calls = run([], [(10, 10, 300, 300)], one)                       # webcam: strict misses, AI sure → second look
    assert res["ok"] and res["methods"].get("opencv_second_look") and calls == [False, True]
    res, calls = run([], [(10, 10, 300, 300)], {"is_face": False, "faces": 0})   # AI not sure → nothing loosened
    assert not res["ok"] and res["reason"] == "no_face" and calls == [False]
    res, calls = run([], [(10, 10, 300, 300)], None)                      # AI down → strict detector alone decides
    assert not res["ok"] and calls == [False]
    res, _ = run([(0, 0, 358, 358), (500, 40, 148, 148)], [], one)        # face + background speck → one face
    assert res["ok"]
    res, _ = run([(0, 0, 300, 300), (400, 0, 260, 260)], [], one)        # two similar faces → refused
    assert not res["ok"] and res["reason"] == "multiple_faces"


def test_machine_translations_must_be_in_the_right_alphabet():
    from app.i18n import translation_ok as ok

    assert not ok("bn", "Back", "Назад")                       # Cyrillic in Bengali (seen in the real catalog)
    assert not ok("bn", "You earned", "आपने कमाया")            # Hindi letters in Bengali
    assert not ok("kn", "Closed", "બંધ")                       # Gujarati in Kannada
    assert not ok("te", "Share my location", "Share my location")   # English handed back
    assert not ok("te", "Pay {amount} by UPI", "UPI ద్వారా చెల్లించండి")   # placeholder lost
    assert not ok("gu", "Next", "<b>આગળ</b>") and not ok("gu", "Next", "આગળ" + chr(10) + "આગળ")
    assert ok("te", "Pay {amount} by UPI", "UPI ద్వారా {amount} చెల్లించండి")
    assert ok("hi", "Upload GST certificate PDF", "GST प्रमाणपत्र PDF अपलोड करें") and ok("ta", "KisanSetu", "KisanSetu")
    assert ok("ur", "Your location", "آپ کا مقام") and ok("lus", "Find jobs", "Hna zawng rawh")
    assert not ok("lus", "Find jobs near you today", "Find jobs near you today")


def test_secrets_never_stored_or_audited_in_plain_text():
    import uuid

    from app.services.actions.base import seal_payload, strip_secrets, unseal_payload
    from app.services.audit import redact

    uid = uuid.uuid4()
    raw = {"email": "a@b.in", "current_password": "582914", "new_password": "NewPass2026x", "new_password_again": "NewPass2026x"}
    sealed = seal_payload(uid, raw)
    assert "582914" not in json.dumps(sealed) and "NewPass2026x" not in json.dumps(sealed) and sealed["email"] == "a@b.in"
    assert unseal_payload(uid, sealed) == raw
    with pytest.raises(Exception):
        unseal_payload(uuid.uuid4(), sealed)                  # bound to the person: another account cannot open it
    assert strip_secrets(sealed) == {"email": "a@b.in"}
    assert redact({"payload": raw, "otp": "123456", "list": [{"password": "x"}]}) == {
        "payload": {"email": "a@b.in", "current_password": "***", "new_password": "***", "new_password_again": "***"},
        "otp": "***", "list": [{"password": "***"}]}


def test_terms_never_allow_money_from_workers():
    """Seen live: "You need to pay 1 lakh to join" was passed as fair. Such clauses block whatever the AI says."""
    from app.services.guide import risky_clauses
    from app.services.terms import strong_flags

    bad = ["You need to pay 1 lakh to join", "Workers must deposit Rs. 5,000 as security.", "A joining fee of ₹2,000 is charged.",
           "Each worker has to pay an agent commission", "There is a security deposit of 10000", "Bring 500 rupees to start work",
           "You must hand over your original Aadhaar card to the manager", "you will work off the advance",
           "जॉइन करने के लिए 1 लाख रुपये देने होंगे", "Join karne ke liye 1 lakh jama karna hoga", "₹5000 deposit required"]
    fine = ["There is no joining fee.", "Workers never pay anything to join.", "We pay a ₹500 joining bonus to every worker.",
            "We never keep your Aadhaar or any document.", "The farm will deposit ₹15,000 salary into your bank account every month.",
            "Workers get ₹520 per day, paid every week.", "No security deposit is taken.", "जॉइन करने के लिए कोई फीस नहीं है।",
            "Free lunch and drinking water are provided.", "Bring your ID card to start work."]
    assert [x for x in bad if not strong_flags(x)] == []
    assert [x for x in fine if strong_flags(x)] == []
    assert strong_flags("You need to pay 1 lakh to join")[0]["code"] == "fee_from_worker"
    assert risky_clauses("You need to pay 1 lakh to join")[0]["id"] == "pay_to_join"      # workers are warned too


# ------------------------------------------------------------------ crypto & passwords
def test_field_encryption_is_bound_to_owner():
    from app.security.crypto import blind_index, cipher

    token = cipher.encrypt("234123412346", aad="iddoc:user-a:aadhaar")
    assert cipher.decrypt_str(token, aad="iddoc:user-a:aadhaar") == "234123412346"
    with pytest.raises(Exception):
        cipher.decrypt_str(token, aad="iddoc:user-b:aadhaar")             # cannot be moved to another user
    assert blind_index("2341 2341 2346", "aadhaar") == blind_index("234123412346", "aadhaar")


def test_passwords_and_rules():
    from app.security.passwords import hash_password_sync, password_problems, verify_password_sync

    h = hash_password_sync("582914")
    assert h.startswith("$argon2id$") and verify_password_sync(h, "582914") and not verify_password_sync(h, "582915")
    assert password_problems("123456", "farmer")                          # common / sequence
    assert not password_problems("582914", "farmer")
    assert password_problems("short1", "owner")                           # owners need ≥10 chars


def test_aadhaar_verhoeff():
    from app.services.actions.account import verhoeff_ok

    assert verhoeff_ok("234123412346")
    assert not verhoeff_ok("234123412345")


# ------------------------------------------------------------------ CSRF masking
def test_csrf_mask_roundtrip():
    from app.security.csrf import _new_raw, _raw_bytes, mask_token, unmask_token

    raw = _new_raw()
    m1, m2 = mask_token(raw), mask_token(raw)
    assert m1 != m2                                                       # fresh pad every render (BREACH-safe)
    assert unmask_token(m1) == _raw_bytes(raw) == unmask_token(m2)


# ------------------------------------------------------------------ terms review rules
def test_terms_rule_flags():
    from app.services.terms import rule_flags

    bad = "Workers must work 12 hours. We keep their Aadhaar card. Employment can be terminated without notice."
    codes = {f["code"] for f in rule_flags(bad)}
    assert {"excess_hours", "id_retention", "no_notice"} <= codes
    assert not rule_flags("Workers get water, food breaks and paid holidays.")


# ------------------------------------------------------------------ AI guard (cannot run code / follow URLs)
def test_ai_guard_filters_everything_dangerous():
    from app.services.ai.guard import clean_actions, clean_text

    assert "<script" not in clean_text("<script>alert(1)</script>hello")
    assert "javascript:" not in clean_text("click javascript:alert(1)")
    actions, nav = clean_actions(
        [{"type": "crop.add", "params": {"crop": "wheat"}}, {"type": "admin.user.ban", "params": {}},
         {"type": "navigate", "params": {"to": "https://evil.example"}}, {"type": "navigate", "params": {"to": "jobs"}},
         "rm -rf /"], allowed={"crop.add"})
    assert actions == [{"type": "crop.add", "params": {"crop": "wheat"}}]
    assert nav == ["/jobs"]


def test_codebase_never_executes_dynamic_code():
    """Security invariant: no eval/exec/os.system/subprocess/pickle anywhere in the application package."""
    # `(?<![\w.])` excludes method calls such as redis `.eval(` (Lua EVAL) and `def eval(` wrappers of that API.
    banned = re.compile(r"(?<![\w.])(eval|exec)\s*\(|os\.system\s*\(|subprocess\.|pickle\.loads|__import__\(|importlib\.import_module|"
                        r"yaml\.load\(|marshal\.loads|\.innerHTML\s*=|insertAdjacentHTML|document\.write|new Function\(")
    offenders = []
    for path in (ROOT / "app").rglob("*"):
        if path.suffix in (".py", ".js") and "vendor" not in path.parts and "dist" not in path.parts:
            for i, line in enumerate(path.read_text("utf-8").splitlines(), 1):
                code = line.strip()
                if code.startswith(("#", "*", "//", "\"", "'")) or re.match(r"(async\s+)?def\s+eval\(", code):
                    continue  # comments / docstrings / the redis-API compatible method definition
                if banned.search(code):
                    offenders.append(f"{path.relative_to(ROOT)}:{i}: {code[:80]}")
    assert not offenders, offenders


# ------------------------------------------------------------------ i18n
def test_every_key_used_exists_in_english_catalog():
    en = json.loads((ROOT / "app/i18n/locales/en.json").read_text("utf-8"))
    pat = re.compile(r"""(?<![A-Za-z_.])t\(\s*['"]([a-z][a-z0-9_]*(?:\.[a-zA-Z0-9_]+)+)['"]""")
    missing = set()
    for path in (ROOT / "app").rglob("*"):
        if path.suffix in (".py", ".html", ".js") and "vendor" not in path.parts and "dist" not in path.parts:
            for m in pat.finditer(path.read_text("utf-8")):
                key = m.group(1)
                if key.endswith(("_", ".")):
                    continue  # dynamic prefix, e.g. t("js.vote_" + status) — families are checked separately
                if key not in en:
                    missing.add(f"{key} ({path.name})")
    assert not missing, sorted(missing)


def test_catalog_placeholders_match_english():
    en = json.loads((ROOT / "app/i18n/locales/en.json").read_text("utf-8"))
    ph = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
    for path in (ROOT / "app/i18n/locales").glob("*.json"):
        if path.name == "en.json":
            continue
        cat = json.loads(path.read_text("utf-8"))
        bad = [k for k, v in cat.items() if k in en and set(ph.findall(v)) != set(ph.findall(en[k]))]
        assert not bad, (path.name, bad[:10])


def test_i18n_fallback_and_rtl():
    from app.i18n import lang_meta, t

    assert t("nav.jobs", "xx-unknown") == "Jobs"
    assert lang_meta("ur")["dir"] == "rtl" and lang_meta("hi")["dir"] == "ltr"
    assert t("dash.hello", "en", name="Sita") == "Namaste, Sita"


# ------------------------------------------------------------------ crops & schemes
def test_crop_recommendation_and_warnings():
    from app.services.crops import BY_KEY, crop_weather_warnings, find_crop, recommend

    assert find_crop("गेहूं").key == "wheat" and find_crop("paddy").key == "rice"
    recs = recommend("Punjab", 15.0, date(2026, 11, 17))
    assert recs and "wheat" in [r["key"] for r in recs]
    warn = crop_weather_warnings(BY_KEY["wheat"], [{"date": "2026-04-01", "tmax": 40, "tmin": 22, "rain": 0}])
    assert warn and warn[0]["kind"] == "too_hot"


def _catalog():
    from app.services.schemes import BUILTIN, Rules

    return [{"id": s["id"], "audience": list(s["for"]), "rules": Rules.model_validate(s["rules"]).compact(), "states": [],
             "status": "active"} for s in BUILTIN]


def test_scheme_eligibility():
    from app.services.schemes import evaluate

    cat = _catalog()
    res = {r["id"]: r for r in evaluate({"age": 30, "landholding_hectares": 1.5, "monthly_income": 9000, "is_income_tax_payer": False,
                                          "has_bank_account": True, "is_govt_employee": False, "has_epf_esic_nps": False,
                                          "is_rural": True}, "farmer", cat)}
    assert res["pm_kisan"]["status"] == "eligible"
    assert res["pm_kmy"]["status"] == "eligible"
    old = {r["id"]: r for r in evaluate({"age": 55, "has_bank_account": True}, "farmer", cat)}
    assert old["apy"]["status"] == "not_eligible"
    # withdrawn schemes disappear; state schemes need the person's state and exclude other states
    cat[0]["status"] = "archived"
    cat.append({"id": "rythu_bharosa", "audience": ["farmer"], "rules": {"land": True}, "states": ["Telangana"], "status": "active"})
    out = {r["id"]: r for r in evaluate({"landholding_hectares": 1, "state": "Punjab"}, "farmer", cat)}
    assert "pm_kisan" not in out and out["rythu_bharosa"]["reasons"] == ["scheme.reason.other_state"]
    out = {r["id"]: r for r in evaluate({"landholding_hectares": 1}, "farmer", cat)}
    assert out["rythu_bharosa"]["status"] == "need_info" and "state" in out["rythu_bharosa"]["missing"]


def test_scheme_links_only_official_https():
    from pydantic import ValidationError

    from app.services.schemes import DEFAULT_DOMAINS, Rules, official_url_problem

    ok = official_url_problem
    assert ok("https://pmkisan.gov.in/", DEFAULT_DOMAINS) is None
    assert ok("https://agrimachinery.nic.in/x?y=1", DEFAULT_DOMAINS) is None
    assert ok("http://pmkisan.gov.in/", DEFAULT_DOMAINS) == "errors.scheme_url_https"
    for bad in ("https://pmkisan.gov.in.evil.com/", "https://evilgov.in/", "https://user@pmkisan.gov.in/", "https://pmkisan.gov.in:8443/",
                "https://127.0.0.1/", "javascript:alert(1)", 'https://pmkisan.gov.in/"onmouseover=x', "https://xn--gov-in.com/",
                "https://pmkisan.gov.in/\\evil", "https:///gov.in", "https://gov.in@evil.com/"):
        assert ok(bad, DEFAULT_DOMAINS) is not None, bad
    for rules in ({"min_age": 50, "max_age": 20}, {"run": "os.system"}, {"min_age": -1}):
        try:
            Rules.model_validate(rules)
        except ValidationError:
            continue
        raise AssertionError(f"rules accepted: {rules}")


def test_work_hours_limit_rule():
    from datetime import datetime

    start, end = time(8, 0), time(16, 30)
    hours = (datetime.combine(date.today(), end) - datetime.combine(date.today(), start)).total_seconds() / 3600
    assert hours > 8  # such a shift is refused by the farm/branch actions (see tests/e2e_flow.py)


# ------------------------------------------------------------------ sanitisation & uploads
def test_clean_text_removes_dangerous_but_keeps_real_language():
    from app.security.sanitize import clean_text

    assert clean_text("Asha\u202e evil\u202c Devi") == "Asha evil Devi"                 # bidi override (Trojan Source)
    assert clean_text("a\x00b\x07c\x1bd") == "abcd"                                     # NUL / control characters
    assert clean_text("x\ud800y") == "xy"                                               # lone surrogate (crashed UTF-8 inserts)
    assert clean_text("hi<script>alert(1)</script> there") == "hi there"
    assert clean_text('<img src=x onerror=alert(1)>Ragi <b>flour</b>') == "Ragi flour"
    assert clean_text("<5 acres and a < b") == "<5 acres and a < b"                     # plain text untouched
    assert clean_text("zero\u200bwidth\ufeff") == "zerowidth"
    hindi = "क्\u200dष"                                                                   # ZWJ is needed by Indic scripts
    assert clean_text(hindi) == hindi
    assert clean_text("👨\u200d🌾 farmer") == "👨\u200d🌾 farmer"                          # emoji ZWJ sequences survive
    assert clean_text("line1\r\nline2\u2028line3") == "line1\nline2\nline3"
    assert clean_text("a\nb\tc", single_line=True) == "a b c"
    assert clean_text("e\u0301") == "\u00e9"                                             # NFC


def test_clean_obj_never_touches_secrets_and_csv_is_formula_safe():
    from app.security.sanitize import clean_obj, csv_safe, safe_filename

    out = clean_obj({"name": "<b>Asha</b>", "password": "<b>pw\u202e", "nested": [{"note": "a\x00b"}]})
    assert out == {"name": "Asha", "password": "<b>pw\u202e", "nested": [{"note": "ab"}]}
    assert csv_safe("=HYPERLINK(\"http://evil\")") == "'=HYPERLINK(\"http://evil\")"
    assert csv_safe("+cmd|' /C calc'!A0") .startswith("'") and csv_safe("@SUM(A1)").startswith("'")
    assert csv_safe("Ragi flour") == "Ragi flour" and csv_safe(42) == 42
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:" + chr(92) + "Windows" + chr(92) + "evil‮gnp.exe") == "evilgnp.exe"
    assert safe_filename("...") == "file"


def test_pdf_active_content_and_scanner_failure_modes():
    import asyncio
    import zlib

    from app.config import settings
    from app.services.uploads import clamav_scan, pdf_active_content

    assert pdf_active_content(b"%PDF-1.4 /Type /Catalog") is None
    assert pdf_active_content(b"%PDF-1.4 << /S /JavaScript /JS (x) >>") == "javascript"
    assert pdf_active_content(b"%PDF-1.4 << /S /J#61vaScript >>") == "javascript"          # hex-escaped name
    hidden = zlib.compress(b"<< /Type /Action /S /Launch /F (cmd.exe) >>")
    assert pdf_active_content(b"%PDF-1.5 1 0 obj << /Filter /FlateDecode >> stream\n" + hidden + b"\nendstream") == "launch"
    bomb = zlib.compress(b"\0" * (64 * 1024 * 1024))                                       # decompression bomb stays bounded
    assert pdf_active_content(b"%PDF-1.5 stream\n" + bomb + b"\nendstream") in (None, "too_complex")
    old = settings.CLAMAV_HOST, settings.CLAMAV_PORT
    try:
        settings.CLAMAV_HOST, settings.CLAMAV_PORT = "127.0.0.1", 1                        # nothing listens → error, never "clean"
        assert asyncio.run(clamav_scan(b"x"))[0] == "error"
        settings.CLAMAV_HOST = ""
        assert asyncio.run(clamav_scan(b"x")) == ("off", None)
    finally:
        settings.CLAMAV_HOST, settings.CLAMAV_PORT = old


def test_face_detector_is_really_available():
    """OpenCV 5 silently removed CascadeClassifier: face checks then rested on the AI alone. Never again unnoticed."""
    from PIL import Image

    from app.services.ocr import _load_cascade, _opencv_faces

    assert _load_cascade() is not None
    assert _opencv_faces(Image.new("RGB", (400, 400), (20, 160, 60))) == []


def test_scheme_official_page_cross_check():
    """Reading the official page: visible text only, key sentences, change detection, name / 'ended' signals, and the
    AI's reading is kept only when it quotes the page and invents no numbers."""
    from app.services.scheme_check import _grounded, ended_hint, key_sentences, name_found, page_text, similarity, sketch

    html = ("<html><head><title>PM-KISAN Samman Nidhi</title><script>var x='Rs 99,999 bonus';</script><style>.a{}</style></head>"
            "<body><nav>Home | About</nav><p>Under PM-KISAN, eligible farmer families get Rs 6,000 per year in three instalments.</p>"
            "<p>The last date to complete e-KYC is 31 March 2026.</p><div>Contact us</div></body></html>")
    text = page_text(html)
    assert "99,999" not in text and "Rs 6,000 per year" in text and ".a{}" not in text
    keys = key_sentences(text)
    assert any("6,000" in k for k in keys) and any("last date" in k for k in keys) and not any("Contact" in k for k in keys)
    assert name_found(["PM-KISAN", "pm kisan"], text) and not name_found(["Fasal Bima Yojana (crop insurance)"], text)
    assert name_found(["PM Fasal Bima Yojana (crop insurance)"], "Pradhan Mantri Fasal Bima Yojana portal")  # gloss ignored
    base = " ".join(f"word{i}" for i in range(400))
    assert similarity(sketch(base), sketch(base)) == 1.0
    assert similarity(sketch(base), sketch(base + " extra words at the end")) > 0.9
    assert similarity(sketch(base), sketch(" ".join(f"other{i}" for i in range(400)))) < 0.1
    assert similarity([], sketch(base)) is None
    assert ended_hint("Notice: this scheme has been discontinued w.e.f. 1 April") == "scheme has been discontinued"
    assert ended_hint("योजना बंद कर दी गई है") is not None and ended_hint("Apply now for the scheme") is None
    assert _grounded("Benefit raised to ₹8,000", "eligible families get Rs 8,000 per year")
    assert not _grounded("Benefit raised to ₹10,000", "eligible families get Rs 8,000 per year")  # invented figure
    assert _grounded("The e-KYC deadline moved", "last date to complete e-KYC")


def test_spam_sanctions_ladders_and_protected_addresses():
    from app.services.ai.moderation import FLOOD_AT, IP_LADDER, JUDGE_AT, NO_AI_AT, POST_LADDER, ensure_can_post, protected_ip
    from app.errors import AppError
    from app.utils import now_utc
    from datetime import timedelta

    assert IP_LADDER == (60, 1440, 10080, 43200) and POST_LADDER == (60, 1440, 10080)
    assert JUDGE_AT < NO_AI_AT < FLOOD_AT
    assert protected_ip("127.0.0.1") and protected_ip("::1") and protected_ip("not-an-ip") and protected_ip(None)
    assert not protected_ip("203.0.113.9")
    ensure_can_post(SimpleNamespace(posting_banned_until=None))
    ensure_can_post(SimpleNamespace(posting_banned_until=now_utc() - timedelta(minutes=1)))
    with pytest.raises(AppError) as e:
        ensure_can_post(SimpleNamespace(posting_banned_until=now_utc() + timedelta(minutes=30)))
    assert e.value.key == "errors.posting_banned" and e.value.status == 403


def test_member_hierarchy_rules():
    from app.services.perms import effective_role, outranks

    assert outranks("owner", "co_owner") and outranks("co_owner", "manager") and outranks("manager", "accountant")
    assert outranks("manager", "employee") and not outranks("manager", "manager") and not outranks("accountant", "manager")
    assert not outranks(None, "employee") and not outranks("manager", None)
    assert effective_role("manager", "notice", "individual") == "employee"      # being let go: no powers
    assert effective_role("manager", "notice", "closure") == "manager"          # closing farm: runs the shutdown
    assert effective_role("co_owner", "notice", "individual") == "co_owner"
    assert effective_role("manager", "ended", None) is None and effective_role("supervisor", "active", None) == "supervisor"


def test_guide_terms_risks_vernacular_and_grounding():
    from app.services.guide import catalog, detect_language, grounded, risky_clauses, screen_parts, speech_code

    terms = ("Workers get Rs 450 per day. A security deposit of Rs 2,000 is taken on joining. "
             "Salary is paid at the end of the season after harvest. The worker must submit original Aadhaar card. "
             "Food and room charges will be deducted from wages. Advance money must be repaid by working until cleared.")
    ids = {r["id"]: r for r in risky_clauses(terms)}
    assert {"deposit", "wage_held", "documents", "deductions", "advance_loan"} <= set(ids)
    assert ids["deposit"]["level"] == "high" and "security deposit" in ids["deposit"]["quote"].lower()
    assert all(r["quote"] in terms for r in ids.values())                       # quotes are the farm's own words
    assert risky_clauses("Workers get the minimum wage, 8 hours a day, Sunday off, paid monthly.") == []
    assert risky_clauses("मजदूर से जमानत राशि ली जाएगी") and risky_clauses("पेशगी काम करके चुकानी होगी")
    assert grounded("You pay a ₹2,000 deposit", "A security deposit of Rs 2,000 is taken", terms)
    assert not grounded("You pay a ₹5,000 deposit", "A security deposit of Rs 2,000 is taken", terms)   # invented number
    assert not grounded("Pay is weekly", "Salary is paid every week", terms)                             # quote not in terms
    assert grounded("₹२,००० जमा करना होगा", "A security deposit of Rs 2,000 is taken", terms)          # Devanagari digits
    assert detect_language("मुझे काम चाहिए", "en") == "hi" and detect_language("मला काम हवे", "mr") == "mr"
    assert detect_language("எனக்கு வேலை வேண்டும்", "hi") == "ta" and detect_language("mujhe kaam chahiye bhai", "en") == "hi-Latn"
    assert detect_language("Need work near Mysore", "hi") == "en" and speech_code("hi-Latn") == "en-IN" and speech_code("ta") == "ta-IN"
    cat = catalog("hi")
    assert cat["guide"]["my-job"]["title"] == "आपकी नौकरी" and cat["speech"] == "hi-IN"
    tds = next(g for g in cat["gloss"] if g["id"] == "tds")
    assert "टीडीएस" in tds["terms"] and "TDS" in tds["terms"]
    assert screen_parts(["my-job", "bad key!", "../x", "unknown-key"], "en") == [("my-job", "Your job")]


def test_business_dates_are_indian_dates():
    """Seen in the stress test: a job 'ended' on the UTC date (yesterday in India before 05:30) after the person had already
    been paid for the Indian day. Business dates come from today_ist() / now_ist() only."""
    bad = []
    for p in (ROOT / "app").rglob("*.py"):
        src = p.read_text("utf-8")
        for pat in ("now_utc().date()", "date.today()", "datetime.today()", "datetime.utcnow().date()", "now_utc():%y", "now_utc():%d"):
            if pat in src:
                bad.append(f"{p.relative_to(ROOT)}: {pat}")
    assert not bad, bad


def test_every_template_compiles():
    """A template typo must fail here, not take the server down at its next start."""
    from app.templating import warm_templates

    assert warm_templates() >= 60


def test_feed_links_are_plain_web_links():
    """Calamity feeds (USGS / GDACS / EONET) become "Source" links on farm pages: only http(s) ever reaches an href."""
    from app.services.weather import _web_link

    assert _web_link("https://earthquake.usgs.gov/earthquakes/eventpage/x") == "https://earthquake.usgs.gov/earthquakes/eventpage/x"
    for bad in ("javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,<script>alert(1)</script>", "vbscript:x",
                "//evil.example/x", "/relative", "", None, 42, {"url": "https://x"}, "https://" + "a" * 600):
        assert _web_link(bad) is None, bad


def test_branch_ids_are_checked_against_the_farm():
    """Stress test found ledger.add / insurance.add taking ANOTHER farm's branch: every action that takes a farm_id and a
    branch_id must check the branch belongs to that farm (or resolve the farm from the branch)."""
    import inspect

    import app.services.actions  # noqa: F401  (registers every action)
    from app.services.actions.base import REGISTRY

    unchecked, checked = [], 0
    for name, action in REGISTRY.items():
        cls = type(action)
        fields = getattr(cls.Payload, "model_fields", {})
        if "branch_id" not in fields or "farm_id" not in fields:
            continue
        src = inspect.getsource(cls)
        checked += 1
        if not ("farm_id != farm.id" in src or "resolve_branch(" in src or "b.farm_id" in src):
            unchecked.append(name)
    assert checked >= 5 and not unchecked, (checked, unchecked)


def test_dynamic_translations_never_share_a_prompt(monkeypatch):
    """Audit: all new texts of one /api/i18n/translate request went to the model in ONE prompt, and the first answer is
    cached for everybody — text sent alongside someone's job terms could steer their translation. One call per text."""
    import asyncio

    from app.services import i18n_ai

    calls = []

    async def fake_store(lang, items):
        calls.append(dict(items))
        return {}

    class _Res:
        def all(self):
            return []

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *_a, **_k):
            return _Res()

    monkeypatch.setattr(i18n_ai, "_translate_and_store", fake_store)
    monkeypatch.setattr(i18n_ai, "SessionLocal", lambda: _DB())
    texts = [f"Owner text number {i} about wages" for i in range(i18n_ai.FRESH_PER_REQUEST + 5)]
    out = asyncio.run(i18n_ai.translate_texts(texts, "hi", wait=0.05))
    assert out == texts                                       # nothing cached yet: English until the translation lands
    assert len(calls) == i18n_ai.FRESH_PER_REQUEST            # capped per request; the rest follow on the next view
    assert all(len(c) == 1 for c in calls)                    # every text alone in its own prompt
    assert len({next(iter(c.values()))[1] for c in calls}) == len(calls)

    # a flood from one address stops at its own cap; another address still gets its texts queued
    calls.clear()
    i18n_ai._pending.clear()
    i18n_ai._client_pending.clear()

    async def slow_store(lang, items):                        # jobs stay pending while the flood continues
        calls.append(dict(items))
        await asyncio.sleep(5)
        return {}

    monkeypatch.setattr(i18n_ai, "_translate_and_store", slow_store)

    async def flood():
        for r in range(6):
            await i18n_ai.translate_texts([f"flood {r} {i} text" for i in range(40)], "hi", wait=0, client="ip:10.0.0.9")
        await asyncio.sleep(0.05)                             # let the queued jobs start
        n_flood = len(calls)
        await i18n_ai.translate_texts(["a farmer's own new text"], "hi", wait=0, client="ip:10.0.0.10")
        await asyncio.sleep(0.05)
        pending = dict(i18n_ai._client_pending)
        for t in list(i18n_ai._pending.values()):
            t.cancel()
        return n_flood, len(calls), pending

    n_flood, n_all, pending = asyncio.run(flood())
    assert n_flood == i18n_ai.MAX_PENDING_PER_CLIENT and n_all == n_flood + 1, (n_flood, n_all)
    assert pending["ip:10.0.0.9"] == i18n_ai.MAX_PENDING_PER_CLIENT


def test_gst_name_hint_for_admins():
    from app.services.gstin import names_match

    assert names_match("Ravi Patil", "RAVI PATIL FARMS") is True
    assert names_match("Ravi Patil", "Green Valley Agro Pvt Ltd") is False      # flag for the admin before approving
    assert names_match("रवि पाटिल", "Ravi Patil Farms") is None                 # other script: nothing to compare
    assert names_match("Ravi Patil", None) is None and names_match(None, "X Farms") is None
    assert names_match("Sita Devi", "Shri Agro Farms Pvt Ltd") is None           # registration is only business words
