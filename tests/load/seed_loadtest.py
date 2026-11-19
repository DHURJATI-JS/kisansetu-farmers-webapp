"""Seed realistic data for a Locust run directly in PostgreSQL (fast bulk inserts) and mint Redis sessions.

    python tests/load/seed_loadtest.py --owners 40 --workers 300

Creates approved farms near Bengaluru (branch, crop, insurance, shop with scarce + plentiful products, open jobs,
5 employees each with today's wellbeing survey) and job-seeking workers. Every stock change is an InventoryMovement
so the post-run invariant "stock == Σ movements" is meaningful. Writes tests/load/.seed.json (git-ignored).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import insert  # noqa: E402

from app.config import settings  # noqa: E402

OUT = Path(__file__).resolve().parent / ".seed.json"
LANGS = ["en"] * 12 + ["hi"] * 5 + ["ta", "bn", "kn"]
CROPS = ["Ragi", "Paddy", "Maize", "Tomato", "Groundnut", "Sugarcane", "Banana", "Coconut"]
PRODUCTS = [("Ragi flour", "grains", "kg", 60), ("Organic jaggery", "processed", "kg", 120), ("Tomatoes", "vegetables", "kg", 30),
            ("Groundnut oil", "processed", "litre", 210), ("Banana (robusta)", "fruits", "dozen", 55), ("Coconut", "fruits", "piece", 35),
            ("Paddy seed", "seeds", "kg", 90), ("Vermicompost", "inputs", "kg", 18)]


def phone() -> str:
    return "+916" + "".join(random.choices("0123456789", k=9))


def gstin() -> str:
    from app.services.gstin import checksum_char

    first14 = "29" + "".join(random.choices("ABCDEFGHJKMNPRTUVWXY", k=5)) + f"{random.randint(1000, 9999)}" \
        + random.choice("ABCDEFGHJKLMNP") + "1Z"
    return first14 + checksum_char(first14)


def near(lat: float, lon: float, km: float) -> tuple[float, float]:
    return lat + random.uniform(-km, km) / 111.0, lon + random.uniform(-km, km) / 108.0


RISKY_TERMS = ("1. Workers get Rs 520 per day for 8 hours.\n2. A security deposit of Rs 2,000 is taken on joining.\n"
               "3. Salary is paid at the end of the season after harvest.\n4. Food and room charges will be deducted from wages.\n"
               "5. Sunday is the weekly day off.")


async def main(n_owners: int, n_workers: int, employees_per_farm: int, team_farms: int = 8) -> None:
    from app.database import SessionLocal, engine
    from app.services.terms import terms_version_for
    from app.models import (
        Branch, BranchCrop, Farm, FarmMember, GstinVerification, InsurancePolicy, InventoryMovement, JobPosting, LedgerEntry,
        Notification, PersonalLedgerEntry, Product, Shop, TermsAcceptance, User, WellbeingSurvey,
    )
    from app.security.passwords import hash_password
    from app.security.sessions import create_session, new_login_key
    from app.services.redis_client import init_redis
    from app.services.terms import BOILERPLATE_VERSION
    from app.utils import now_utc, today_ist

    await init_redis()
    run = datetime.now().strftime("%m%d%H%M")
    tag = f"LT-{run}"
    t0 = time.time()
    pw = await hash_password("LoadTest2026x")
    now, today = now_utc(), today_ist()
    base_lat, base_lon = 12.97, 77.59

    owners, workers, gst, farms, branches, crops, policies, shops, products, moves, jobs, members, surveys, terms = \
        [], [], [], [], [], [], [], [], [], [], [], [], [], []
    for i in range(n_owners):
        lat, lon = near(base_lat, base_lon, 25)
        owners.append(dict(id=uuid.uuid4(), full_name=f"Lo{i:03d} Rao", phone=phone(), email=f"lt{run}o{i}@kisansetu.in",
                           password_hash=pw, role="owner", language=random.choice(LANGS), status="active", phone_verified=True,
                           email_verified=True, photo_verified=True, dob=date(1975 + i % 20, 1 + i % 12, 1 + i % 27), lat=lat, lon=lon,
                           state="Karnataka", district=tag, city="Bengaluru", terms_version="platform-v1", terms_accepted_at=now,
                           privacy_accepted_at=now, token_version=0, landholding_hectares=Decimal("4.5"), has_bank_account=True,
                           is_income_tax_payer=False, is_govt_employee=False, has_epf_esic_nps=False, is_rural=True))
    for i in range(n_workers):
        lat, lon = near(base_lat, base_lon, 20)
        workers.append(dict(id=uuid.uuid4(), full_name=f"Lw{i:04d} Kumar", phone=phone(), email=None, password_hash=pw, role="farmer",
                            language=random.choice(LANGS), status="active", phone_verified=True, email_verified=False, photo_verified=True,
                            dob=date(1985 + i % 18, 1 + i % 12, 1 + i % 27), lat=lat, lon=lon, state="Karnataka", district=tag,
                            city="Bengaluru", terms_version="platform-v1", terms_accepted_at=now, privacy_accepted_at=now, token_version=0,
                            landholding_hectares=Decimal(random.choice(["0", "0.5", "1.2"])), has_bank_account=True,
                            is_income_tax_payer=False, is_govt_employee=False, has_epf_esic_nps=False, is_rural=True))

    notes, pledger = [], []
    for i, u in enumerate(owners + workers):
        tag_ = f"O{i:03d}" if u["role"] == "owner" else f"W{i - len(owners):04d}"
        u["address"] = f"LTA-{tag_} Village Road"          # private: shown only on this person's account page
        u["_note"] = f"LTN-{tag_}"                           # private: only in this person's notifications
        notes.append(dict(id=uuid.uuid4(), user_id=u["id"], kind="welcome", title=f"Welcome {u['_note']}",
                          body="Your load-test account is ready.", severity="info"))
        u["_stmt"] = f"LTS-{tag_}"                           # private: only in this person's own statement
        pledger.append(dict(id=uuid.uuid4(), user_id=u["id"], entry_type="salary_credit", direction="in", amount=Decimal("100"),
                            description=f"{u['_stmt']} opening credit", reference_type="seed", reference_id=tag_))
    for u in owners + workers:
        u["_lk"], u["session_key_hash"] = new_login_key()
        u["session_started_at"], u["session_device"], u["session_ip"] = now, "Locust on load test", "127.0.0.1"
    pool = list(workers)
    random.shuffle(pool)
    for i, o in enumerate(owners):
        gid, fid, bid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        g = gstin()
        gst.append(dict(id=gid, user_id=o["id"], gstin=g, legal_name=f"Load Farms {i}", state_code="29", status="verified", method="test",
                        verified_at=now))
        farms.append(dict(id=fid, owner_id=o["id"], gstin_verification_id=gid, gstin=g, name=f"LT Farm {run}-{i}", farming_type="organic",
                          status="active", hiring_limit=60, pay_day=7, payment_method="upi", terms_mode="boilerplate",
                          terms_version=BOILERPLATE_VERSION, approval_requested_at=now, approved_at=now))
        blat, blon = near(o["lat"], o["lon"], 3)
        branches.append(dict(id=bid, farm_id=fid, name="Main field", is_main=True, city="Bengaluru", district="Bengaluru Rural",
                             state="Karnataka", lat=blat, lon=blon, area_acres=Decimal("12"), work_start=dtime(7, 0), work_end=dtime(15, 0),
                             weekly_offs=[6], hiring_open=True, status="active"))
        crops.append(dict(id=uuid.uuid4(), farm_id=fid, branch_id=bid, crop_key=None, name=random.choice(CROPS), status="growing",
                          added_by_id=o["id"]))
        for ptype, covers in (("comprehensive", True), ("health", False)):
            policies.append(dict(id=uuid.uuid4(), farm_id=fid, policy_type=ptype, insurer="Kisan Mutual", policy_number=f"{ptype[:3].upper()}-{i}",
                                 sum_insured=Decimal("900000"), covers_salaries=covers, valid_from=today - timedelta(days=30),
                                 valid_to=today + timedelta(days=335), status="verified", added_by_id=o["id"]))
        shops.append(dict(id=sid, farm_id=fid, name=f"LT Shop {i}", status="open", courier_base_fee=Decimal("40"),
                          courier_per_km=Decimal("6"), delivery_radius_km=Decimal("40"), accepts_delivery=True, accepts_pickup=True))
        prods = []
        for j, (name, cat, unit, price) in enumerate(random.sample(PRODUCTS, 5)):
            pid = uuid.uuid4()
            stock = Decimal(25 if j < 3 else 500)  # scarce stock makes buyers race for the last units
            products.append(dict(id=pid, shop_id=sid, farm_id=fid, branch_id=bid, name=name, category=cat, unit=unit, price=Decimal(price),
                                 stock=stock, min_order_qty=Decimal("1"), status="active", version=1))
            moves.append(dict(id=uuid.uuid4(), product_id=pid, delta=stock, stock_after=stock, reason="initial", actor_id=o["id"],
                              idempotency_key=f"init:{pid}"))
            prods.append({"id": str(pid), "scarce": j < 3, "price": price})
        o["_farm"] = {"farm_id": str(fid), "branch_id": str(bid), "shop_id": str(sid), "products": prods, "jobs": [], "members": [],
                      "lat": blat, "lon": blon}
        for k in range(2):
            jid = uuid.uuid4()
            jobs.append(dict(id=jid, farm_id=fid, branch_id=bid, title=["Field hand", "Harvest helper"][k], role="employee", salary_type="daily",
                             salary_amount=Decimal("520"), positions=40, description="Seasonal farm work, 8 hours, water + lunch provided.",
                             status="open", created_by_id=o["id"]))
            o["_farm"]["jobs"].append(str(jid))
        for _ in range(employees_per_farm):
            if not pool:
                break
            w = pool.pop()
            mid = uuid.uuid4()
            members.append(dict(id=mid, farm_id=fid, branch_id=bid, user_id=w["id"], role="employee", salary_type="daily",
                                salary_amount=Decimal("520"), joined_on=today - timedelta(days=10), status="active",
                                terms_version=BOILERPLATE_VERSION, terms_accepted_at=now, source="invite"))
            terms.append(dict(id=uuid.uuid4(), user_id=w["id"], farm_id=fid, version=BOILERPLATE_VERSION, ip="127.0.0.1"))
            sv = uuid.uuid4()
            surveys.append(dict(id=sv, user_id=w["id"], member_id=mid, farm_id=fid, branch_id=bid, survey_date=today, status="pending"))
            w["_job"] = {"farm_id": str(fid), "member_id": str(mid), "survey_id": str(sv)}
            o["_farm"]["members"].append(str(mid))
        if i < team_farms:   # team farms: two managers + an accountant, so roles can be changed and raced under load
            o["_farm"]["team"], o["_farm"]["leaders"] = True, []
            for role in ("manager", "manager", "accountant"):
                if not pool:
                    break
                w = pool.pop()
                mid = uuid.uuid4()
                members.append(dict(id=mid, farm_id=fid, branch_id=None, user_id=w["id"], role=role, salary_type="monthly",
                                    salary_amount=Decimal("26000"), joined_on=today - timedelta(days=20), status="active",
                                    terms_version=BOILERPLATE_VERSION, terms_accepted_at=now, source="invite"))
                w["_lead"] = {"farm_id": str(fid), "member_id": str(mid), "role": role, "branch_id": str(bid)}
                o["_farm"]["leaders"].append(str(mid))
            if i < 2:          # risky (legal) custom terms → the explainer has something to warn about
                farms[-1].update(terms_mode="custom", terms_text=RISKY_TERMS, terms_version=terms_version_for("custom", RISKY_TERMS))

    fledger = []
    for o in owners:                                         # private: only in this farm's statement (owner / co-owners)
        o["_farm"]["farm_stmt"] = f"LTF-{o['_note'][4:]}"
        fledger.append(dict(id=uuid.uuid4(), farm_id=uuid.UUID(o["_farm"]["farm_id"]), entry_type="income", direction="in",
                            amount=Decimal("250"), category="seed", description=f"{o['_farm']['farm_stmt']} farm opening income",
                            occurred_on=today, created_by_name="seed"))
    async with SessionLocal() as db:
        strip = lambda rows: [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]  # noqa: E731
        for model, rows in ((User, strip(owners) + strip(workers)), (GstinVerification, gst), (Farm, farms), (Branch, branches),
                            (BranchCrop, crops), (InsurancePolicy, policies), (Shop, shops), (Product, products),
                            (InventoryMovement, moves), (JobPosting, jobs), (FarmMember, members), (TermsAcceptance, terms),
                            (WellbeingSurvey, surveys), (Notification, notes), (PersonalLedgerEntry, pledger),
                            (LedgerEntry, fledger)):
            for k in range(0, len(rows), 500):
                await db.execute(insert(model), rows[k:k + 500])
        await db.commit()

    async def sess(u: dict) -> str:
        return await create_session(u["id"], 0, "127.0.0.1", "locust", extra={"lk": u["_lk"]})

    out = {"run": run, "tag": tag, "created": now.isoformat(), "owners": [], "workers": []}
    for o in owners:
        out["owners"].append({"sid": await sess(o), "user_id": str(o["id"]), "phone": o["phone"], "lang": o["language"],
                              "name": o["full_name"], "addr": o["address"].split()[0], "note": o["_note"], "stmt": o["_stmt"],
                              **o["_farm"]})
    for w in workers:
        out["workers"].append({"sid": await sess(w), "user_id": str(w["id"]), "phone": w["phone"], "lang": w["language"],
                               "name": w["full_name"], "addr": w["address"].split()[0], "note": w["_note"], "stmt": w["_stmt"],
                               "lat": w["lat"], "lon": w["lon"], **({"job": w["_job"]} if "_job" in w else {}),
                               **({"lead": w["_lead"]} if "_lead" in w else {})})
    OUT.write_text(json.dumps(out, indent=1), "utf-8")
    await engine.dispose()
    print(f"seeded {tag}: {len(owners)} owners/farms/shops, {len(products)} products, {len(jobs)} jobs, {len(members)} employees, "
          f"{len(workers)} workers in {time.time() - t0:.1f}s → {OUT.name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--owners", type=int, default=40)
    ap.add_argument("--workers", type=int, default=300)
    ap.add_argument("--employees", type=int, default=5)
    ap.add_argument("--team-farms", type=int, default=8, help="farms that also get two managers and an accountant")
    a = ap.parse_args()
    if not settings.TEST_MODE:
        sys.exit("refusing to seed load-test data unless TEST_MODE=true")
    asyncio.run(main(a.owners, a.workers, a.employees, a.team_farms))
