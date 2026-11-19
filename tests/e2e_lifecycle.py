"""Lifecycle end-to-end against a RUNNING server (TEST_MODE=true):
co-owner consensus, emergency halt + order cancellation, closure blocked while salary is due, closure after payment,
owner account deletion only after the farm is closed.

    python tests/e2e_lifecycle.py
"""
from __future__ import annotations

import asyncio
import random
import sys
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from e2e_flow import FAILS, Client, finish, ok, reset_limits, signup, sql, text_image  # noqa: E402


def phone(prefix: str) -> str:
    return prefix + str(uuid.uuid4().int % 10**9).zfill(9)


def new_gstin() -> str:
    from app.services.gstin import checksum_char

    first14 = "29" + "".join(random.choices("ABCDEFGHJKMNPRTUVWXY", k=5)) + f"{random.randint(1000, 9999)}" + random.choice("ABCDEFGHJKLMNP") + "1Z"
    return first14 + checksum_char(first14)


def create_farm(owner: Client) -> tuple[str, str]:
    gstin = new_gstin()
    r = owner.post("/api/gstin/scan", files={"file": ("gst.png", text_image(["GST REG-06", f"GSTIN: {gstin}", "Legal Name: Test Farms"]), "image/png")})
    ok(r.status_code == 200, f"GSTIN {gstin} scanned", r.text[:200])
    owner.action("gstin.verify", {"verification_id": r.json()["verification_id"]})
    ups = [owner.post("/api/uploads", data={"kind": "insurance"}, files={"file": ("p.png", text_image([k, "Policy"]), "image/png")}).json()["upload_id"]
           for k in ("Salary policy", "Health policy")]
    today = date.today()
    _, g = owner.action("farm.create", {
        "gstin_verification_id": r.json()["verification_id"], "name": f"Lifecycle Farm {uuid.uuid4().hex[:4]}", "farming_type": "organic",
        "hiring_limit": 5, "pay_day": 5, "payment_method": "upi", "terms_mode": "boilerplate", "accept_platform_terms": True,
        "branch": {"name": "North field", "lat": 12.97, "lon": 77.59, "work_start": "07:00", "work_end": "15:00", "weekly_offs": [6],
                   "crops": [{"name": "Ragi"}]},
        "insurance": [{"policy_type": "comprehensive", "insurer": "Kisan Mutual", "policy_number": "CMP-001", "sum_insured": 900000, "covers_salaries": True,
                       "valid_from": str(today.replace(day=1)), "valid_to": str(today + timedelta(days=365)), "upload_id": ups[0]},
                      {"policy_type": "health", "insurer": "Kisan Mutual", "policy_number": "HLT-001", "sum_insured": 300000,
                       "valid_from": str(today.replace(day=1)), "valid_to": str(today + timedelta(days=365)), "upload_id": ups[1]}]})
    farm_id = g.json()["farm_id"]
    return farm_id, str(sql("SELECT id FROM branches WHERE farm_id = :f", f=farm_id)[0][0])


async def _closure_tick() -> dict:
    from app.database import SessionLocal, engine  # noqa: F401
    from app.services.redis_client import init_redis

    await init_redis()
    from app.services.scheduler import closure_tick

    res = await closure_tick(force=True)   # the test re-runs the daily job on purpose
    await engine.dispose()
    return res


def main() -> int:
    t0 = time.time()
    reset_limits()
    a, b, w = Client("ownerA"), Client("coownerB"), Client("worker")
    pa, pb, pw = phone("9"), phone("9"), phone("8")
    signup(a, "owner", pa, "Anil Rao", "OwnerAlpha2026", 12.97, 77.59, email=f"anil{pa[-4:]}@kisansetu.in", dob="1980-01-01")
    signup(b, "owner", pb, "Bina Rao", "OwnerBeta2026x", 12.98, 77.60, email=f"bina{pb[-4:]}@kisansetu.in", dob="1982-02-02")
    signup(w, "farmer", pw, "Mohan Kumar", "739251", 12.99, 77.60, dob="1998-03-03")
    sql("UPDATE users SET photo_verified = true WHERE phone IN (:a, :b, :c)", a="+91" + pa, b="+91" + pb, c="+91" + pw)
    farm_id, branch_id = create_farm(a)

    print("1. co-owner joins; renaming now needs consensus")
    a.action("coowner.add", {"farm_id": farm_id, "phone": pb})
    inv = str(sql("SELECT id FROM invitations WHERE farm_id = :f AND role = 'co_owner'", f=farm_id)[0][0])
    b.refresh("/dashboard")
    b.action("invitation.respond", {"invitation_id": inv, "accept": True, "accept_terms": True})
    _, g = a.action("farm.rename", {"farm_id": farm_id, "name": "Harmony Fields"})
    ok(g.json().get("status") == "proposed", "rename became a proposal (co-owner must approve)", g.text[:200])
    ok(sql("SELECT name FROM farms WHERE id = :f", f=farm_id)[0][0] != "Harmony Fields", "nothing changed before approval")
    prop = str(sql("SELECT id FROM farm_proposals WHERE farm_id = :f AND status = 'pending'", f=farm_id)[0][0])
    r = b.post(f"/api/proposals/{prop}/vote", json={"vote": "approve"})
    ok(r.status_code == 200 and r.json().get("status") == "executed", "co-owner approved → executed", r.text[:200])
    ok(sql("SELECT name FROM farms WHERE id = :f", f=farm_id)[0][0] == "Harmony Fields", "farm renamed after unanimous approval")

    print("2. hire a worker; open a shop; place an order")
    _, g = a.action("job.create", {"farm_id": farm_id, "branch_id": branch_id, "title": "Field hand", "salary_type": "daily",
                                    "salary_amount": 450, "positions": 1})
    job = g.json()["job_id"]
    w.refresh(f"/jobs/{job}")
    w.action("job.apply", {"job_id": job, "lat": 12.99, "lon": 77.60, "accept_terms": True})
    a.action("application.decide", {"application_id": str(sql("SELECT id FROM job_applications WHERE posting_id = :j", j=job)[0][0]),
                                    "decision": "accept"})
    member = str(sql("SELECT id FROM farm_members WHERE farm_id = :f AND role = 'employee'", f=farm_id)[0][0])
    _, g = a.action("shop.create", {"farm_id": farm_id, "name": "Harmony Shop"})
    _, g2 = a.action("product.create", {"shop_id": g.json()["shop_id"], "branch_id": branch_id, "name": "Ragi flour", "category": "grains",
                                         "unit": "kg", "price": 60, "stock": 20})
    pid = g2.json()["product_id"]
    w.action("order.place", {"shop_id": g.json()["shop_id"], "items": [{"product_id": pid, "qty": 4}], "fulfilment": "pickup",
                             "idempotency_key": uuid.uuid4().hex})

    print("3. emergency halt cancels branch orders and restores stock")
    a.action("branch.emergency", {"branch_id": branch_id, "kind": "earthquake", "note": "Tremors felt"})
    ok(sql("SELECT status FROM orders WHERE farm_id = :f", f=farm_id)[0][0] == "cancelled", "open order cancelled by emergency")
    ok(float(sql("SELECT stock FROM products WHERE id = :p", p=pid)[0][0]) == 20.0, "stock restored (16 → 20)")
    ok(bool(sql("SELECT 1 FROM halts WHERE farm_id = :f AND reason = 'emergency' AND lifted_at IS NULL", f=farm_id)), "branch halted")
    a.action("branch.resume", {"branch_id": branch_id})
    ok(not sql("SELECT 1 FROM halts WHERE farm_id = :f AND reason = 'emergency' AND lifted_at IS NULL", f=farm_id), "work resumed")

    print("4. closure needs consensus and cannot finish while ₹ is due")
    _, g = a.action("farm.close", {"farm_id": farm_id, "reason": "Selling the land"})
    ok(g.json().get("status") == "proposed", "closing is a proposal", g.text[:160])
    prop = str(sql("SELECT id FROM farm_proposals WHERE farm_id = :f AND status = 'pending'", f=farm_id)[0][0])
    b.post(f"/api/proposals/{prop}/vote", json={"vote": "approve"})
    ok(sql("SELECT status FROM farms WHERE id = :f", f=farm_id)[0][0] == "closing", "farm is closing (1-month notice running)")
    ok(sql("SELECT status FROM farm_members WHERE id = :m", m=member)[0][0] == "notice", "worker received one-month notice")
    _, r = a.action("account.delete", {"confirm": True}, expect_ok=False)
    ok(r.status_code == 400, "owner cannot delete account while the farm is not closed", r.text[:120])
    # time-travel: closing date reached, salary still unpaid
    sql("UPDATE farms SET closure_date = (now() AT TIME ZONE 'Asia/Kolkata')::date WHERE id = :f", f=farm_id)
    sql("UPDATE branches SET closure_date = (now() AT TIME ZONE 'Asia/Kolkata')::date WHERE farm_id = :f", f=farm_id)
    sql("UPDATE farm_members SET notice_end_date = (now() AT TIME ZONE 'Asia/Kolkata')::date WHERE id = :m", m=member)
    print("     tick:", asyncio.run(_closure_tick()))
    ok(sql("SELECT status FROM farms WHERE id = :f", f=farm_id)[0][0] == "closing", "farm NOT closed while salary is due")
    ok(bool(sql("SELECT 1 FROM halts WHERE farm_id = :f AND reason = 'closure' AND lifted_at IS NULL", f=farm_id)), "all work halted for closure")
    due = sql("SELECT coalesce(sum(net),0) FROM salary_accruals WHERE member_id = :m", m=member)[0][0]
    print(f"     salary due: ₹{due}")

    print("5. pay via screenshot (OCR) → closure completes")
    shot = text_image(["Payment Successful", f"Rs {float(due):.2f}", "Paid to Mohan Kumar", "UPI Ref No: 5" + str(uuid.uuid4().int)[:11]], size=(900, 560), fs=48)
    a.refresh(f"/farms/{farm_id}?tab=payroll")
    up = a.post("/api/uploads", data={"kind": "payment"}, files={"file": ("pay.png", shot, "image/png")})
    a.action("payroll.pay", {"member_id": member, "upload_id": up.json()["upload_id"], "method": "upi"})
    print("     tick:", asyncio.run(_closure_tick()))
    ok(sql("SELECT status FROM farms WHERE id = :f", f=farm_id)[0][0] == "closed", "farm closed once every rupee was paid")
    ok(sql("SELECT status FROM farm_members WHERE id = :m", m=member)[0][0] == "ended", "worker's employment ended cleanly")
    ok(bool(sql("SELECT 1 FROM personal_ledger WHERE entry_type = 'salary_credit' AND amount > 0")), "worker statement has the salary credit")

    print("6. owner deletion is now allowed (data kept 30 days, export sent)")
    _, g = a.action("account.delete", {"confirm": True})
    ok(sql("SELECT status FROM users WHERE phone = :p", p="+91" + pa)[0][0] == "deleted", "owner account deleted (soft, 30-day retention)")
    ok(bool(sql("SELECT 1 FROM outbound_messages WHERE body LIKE '%/account/export/%' ORDER BY created_at DESC LIMIT 1")),
       "export link queued for SMS/WhatsApp")
    return finish(t0)


if __name__ == "__main__":
    sys.exit(main())
