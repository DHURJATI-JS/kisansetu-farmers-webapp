"""Salary payments (amount read ONLY from the payment screenshot via OCR + Gemma — never typed by the owner)
and farm ledger entries (expenses / income)."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from app.errors import AppError, Conflict, NotFound
from app.models import Branch, Farm, FarmMember, LedgerEntry, PersonalLedgerEntry, SalaryPayment, User
from app.services.actions.base import Action, Ctx, Payload, Prepared, register
from app.services.actions.fields import PositiveMoney, ShortText
from app.services.halts import lift_halts
from app.services.images import dhash, hamming, load_image
from app.services.notify import notify, notify_farm_leaders
from app.services.ocr import analyze_payment_screenshot
from app.services.payroll import ensure_accruals_for_farm, farm_money, farms_dues_summary
from app.services.perms import require_perm
from app.services.uploads import owned_upload, read_upload_bytes
from app.utils import format_inr, today_ist

D0 = Decimal("0")


async def _screenshot_analysis(ctx: Ctx, upload) -> dict:
    cached = (upload.meta or {}).get("payment_analysis")
    if cached:
        return cached
    data = await read_upload_bytes(upload)
    res = await analyze_payment_screenshot(data)
    im, _ = load_image(data)
    res["dhash"] = dhash(im)
    upload.meta = {**(upload.meta or {}), "payment_analysis": res}
    return res


@register
class PayrollPay(Action):
    type = "payroll.pay"

    class Payload(Payload):
        member_id: uuid.UUID
        upload_id: uuid.UUID
        method: Literal["upi", "bank", "cash"]

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        m = await ctx.db.get(FarmMember, p.member_id)
        if not m:
            raise NotFound()
        farm, _ = await require_perm(ctx.db, m.farm_id, ctx.user, "payroll")
        employee = await ctx.db.get(User, m.user_id)
        up = await owned_upload(ctx.db, ctx.user, p.upload_id, "payment", unused=True)
        res = await _screenshot_analysis(ctx, up)
        if not res.get("ok"):
            raise AppError(f"errors.pay_{res.get('reason') or 'unreadable'}")
        amount = Decimal(res["amount"])
        recent = (await ctx.db.execute(select(SalaryPayment.image_hash, SalaryPayment.upi_ref).where(
            SalaryPayment.farm_id == farm.id).order_by(SalaryPayment.created_at.desc()).limit(500))).all()
        if res.get("reference") and any(r[1] == res["reference"] for r in recent):
            raise Conflict("errors.pay_duplicate_ref")
        if any(r[0] and hamming(r[0], res["dhash"]) <= 3 for r in recent):
            raise Conflict("errors.pay_duplicate_image")
        await ensure_accruals_for_farm(ctx.db, farm.id)
        money = (await farm_money(ctx.db, farm.id, members=[m]))[m.id]
        remaining = money.due - amount
        engines = ", ".join(f"{k}: {('₹' + v) if v else '—'}" for k, v in (res.get("engines") or {}).items())
        lines = [ctx.t("sum.pay_1", name=employee.full_name, amount=format_inr(amount)),
                 ctx.t("sum.pay_engines", engines=engines),
                 ctx.t("sum.pay_due", due=format_inr(money.due), overdue=format_inr(money.overdue)),
                 ctx.t("sum.pay_after", remaining=format_inr(max(D0, remaining)))]
        warnings = []
        if remaining > 0:
            warnings.append(ctx.t("warn.pay_partial", due=format_inr(money.due)))
        elif remaining < 0:
            warnings.append(ctx.t("warn.pay_advance", extra=format_inr(-remaining)))
        if money.due <= 0:
            warnings.append(ctx.t("warn.pay_nothing_due"))
        return Prepared(lines, warnings=warnings, farm_id=farm.id, data={"amount": str(amount), "ref": res.get("reference"),
                                                                         "dhash": res["dhash"], "conf": res.get("confidence")})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        m = (await ctx.db.execute(select(FarmMember).where(FarmMember.id == p.member_id).with_for_update())).scalar_one()
        farm = await ctx.db.get(Farm, m.farm_id)
        employee = await ctx.db.get(User, m.user_id)
        amount = Decimal(prep.data["amount"])
        money = (await farm_money(ctx.db, farm.id, members=[m]))[m.id]
        tds_portion = max(D0, money.tds - money.tds_recorded)
        up = await owned_upload(ctx.db, ctx.user, p.upload_id, "payment", unused=True)
        up.used_for = f"payment:{farm.id}"
        pay = SalaryPayment(member_id=m.id, farm_id=farm.id, employee_id=employee.id, employee_name=employee.full_name,
                            amount=amount, tds_portion=tds_portion, method=p.method, upload_id=up.id, image_hash=prep.data["dhash"],
                            upi_ref=prep.data.get("ref"), ocr=(up.meta or {}).get("payment_analysis"), confidence=prep.data.get("conf"),
                            recorded_by_id=ctx.user.id, recorded_by_name=ctx.user.full_name)
        ctx.db.add(pay)
        await ctx.db.flush()
        ref = str(pay.id)
        ctx.db.add(LedgerEntry(farm_id=farm.id, branch_id=m.branch_id, entry_type="salary_payment", direction="out", amount=amount,
                               category="salary", description=f"Salary to {employee.full_name} ({p.method})",
                               counterparty_user_id=employee.id, counterparty_name=employee.full_name, reference_type="salary_payment",
                               reference_id=ref, occurred_on=today_ist(), created_by_id=ctx.user.id,
                               created_by_name=ctx.user.full_name, upload_id=up.id, idempotency_key=f"salpay:{ref}"))
        ctx.db.add(PersonalLedgerEntry(user_id=employee.id, entry_type="salary_credit", direction="in", amount=amount,
                                       description=f"Salary from {farm.name} ({p.method})", farm_id=farm.id, farm_name=farm.name,
                                       reference_type="salary_payment", reference_id=ref, idempotency_key=f"salcredit:{ref}"))
        if tds_portion > 0:
            ctx.db.add(LedgerEntry(farm_id=farm.id, entry_type="tds_withheld", direction="info", amount=tds_portion, category="tax",
                                   description=f"TDS withheld for {employee.full_name} — deposit with Income-tax Dept.",
                                   counterparty_user_id=employee.id, counterparty_name=employee.full_name, reference_type="salary_payment",
                                   reference_id=ref, occurred_on=today_ist(), created_by_id=ctx.user.id,
                                   created_by_name=ctx.user.full_name, idempotency_key=f"tds:{ref}"))
            ctx.db.add(PersonalLedgerEntry(user_id=employee.id, entry_type="tds_deducted", direction="info", amount=tds_portion,
                                           description=f"TDS deducted by {farm.name}", farm_id=farm.id, farm_name=farm.name,
                                           reference_type="salary_payment", reference_id=ref, idempotency_key=f"tdsded:{ref}"))
        # proof of payment for the worker: the screenshot itself — attached to the e-mail, as a WhatsApp picture, and
        # behind a private signed link in the SMS (phones cannot sign in from a text) — on every channel they have
        from pathlib import Path

        from app.config import settings
        from app.security.tokens import sign_purpose

        token = sign_purpose("payproof", {"p": ref, "u": str(employee.id)}, minutes=PROOF_LINK_DAYS * 24 * 60)
        base = settings.BASE_URL.rstrip("/")
        await notify(ctx.db, employee.id, "salary_credited", title_key="notif.salary_credited.title", body_key="notif.salary_credited.body",
                     params={"amount": format_inr(amount), "farm": farm.name}, link=f"/payments/{pay.id}", severity="success",
                     channels=("email", "whatsapp", "sms"), attachment_path=str(Path(settings.UPLOAD_DIR) / up.path),
                     media_url=f"{base}/proof/{token}/image", public_link=f"{base}/proof/{token}")
        await notify_farm_leaders(ctx.db, farm.id, "salary_paid", title_key="notif.salary_paid.title", body_key="notif.salary_paid.body",
                                  params={"amount": format_inr(amount), "name": employee.full_name, "by": ctx.user.full_name},
                                  link=f"/farms/{farm.id}?tab=ledger", roles=("co_owner", "manager", "accountant"))
        await ctx.db.flush()
        summary = (await farms_dues_summary(ctx.db, [farm.id]))[farm.id]
        lifted = 0
        if summary["overdue"] <= 0:
            lifted += await lift_halts(ctx.db, farm.id, "salary_overdue", by=ctx.user.id, note="dues cleared")
        if summary["due"] <= 0:
            lifted += await lift_halts(ctx.db, farm.id, "settlement_pending", by=ctx.user.id, note="settled")
        return {"message": ctx.t("msg.salary_recorded", amount=format_inr(amount), lifted=lifted), "payment_id": ref}


PROOF_LINK_DAYS = 30


@register
class LedgerAdd(Action):
    type = "ledger.add"

    class Payload(Payload):
        farm_id: uuid.UUID
        branch_id: uuid.UUID | None = None
        entry_type: Literal["expense", "income"]
        amount: PositiveMoney
        category: str = Field(min_length=2, max_length=64)
        description: ShortText
        occurred_on: date
        upload_id: uuid.UUID | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        farm, _ = await require_perm(ctx.db, p.farm_id, ctx.user, "ledger")
        if p.branch_id:                     # only a branch of THIS farm — never tag an entry onto another farm's branch
            b = await ctx.db.get(Branch, p.branch_id)
            if not b or b.farm_id != farm.id:
                raise NotFound("errors.branch_not_found")
        if p.occurred_on > today_ist():
            raise AppError("errors.date_future")
        if p.upload_id:
            await owned_upload(ctx.db, ctx.user, p.upload_id, "expense", unused=True)
        return Prepared([ctx.t(f"sum.ledger_{p.entry_type}", amount=format_inr(p.amount), category=p.category,
                               date=p.occurred_on.isoformat()), ctx.t("sum.ledger_visible")], farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        if p.upload_id:
            up = await owned_upload(ctx.db, ctx.user, p.upload_id, "expense")
            up.used_for = f"expense:{p.farm_id}"
        ctx.db.add(LedgerEntry(farm_id=p.farm_id, branch_id=p.branch_id, entry_type=p.entry_type,
                               direction="out" if p.entry_type == "expense" else "in", amount=p.amount, category=p.category,
                               description=p.description, occurred_on=p.occurred_on, created_by_id=ctx.user.id,
                               created_by_name=ctx.user.full_name, upload_id=p.upload_id,
                               idempotency_key=f"ledger:{ctx.action_id}" if ctx.action_id else None))
        return {"message": ctx.t("msg.saved")}
