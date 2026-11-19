"""Server-rendered pages (Jinja2). Every page loads its data with a constant number of queries (no N+1)."""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_user, get_db, lang_of, nav_counts, require_active, require_admin, require_user
from app.errors import Forbidden, NotFound
from app.i18n import t
from app.security.ratelimit import ip_key, limiter
from app.models import (
    AbuseEvent, AuditLog, Branch, BranchCrop, BranchDay, CalamityEvent, Farm, FarmMember, FarmProposal, GstinVerification,
    Grievance, Halt, IdentityDocument, InsurancePolicy, Invitation, IpBan, JobApplication, JobPosting, JobRun, LedgerEntry,
    MinimumWage, Holiday, OutboundMessage, Product, ProductQuestion, ProposalVote, SafetyCase, SalaryPayment, Shop,
    UnbanRequest, User, WeatherReport, WellbeingSurvey, GovScheme, SchemeVisit, LoadTestRun,
)
from app.services.crops import CROPS
from app.services.halts import active_halts
from app.services.india_data import STATES
from app.services.jobs import city_options
from app.services.payroll import farms_dues_summary, member_rows, next_due_date
from app.services.perms import PERMS, RANK, can, effective_role, holds_role, require_perm, roles_by_farm
from app.services.schemes import (
    SLUG_RE, all_schemes, get_scheme, official_url_problem, record_visit, rules_text, scheme_card, scheme_domains,
)
from app.services.settings_store import get_all
from app.services.shop import buyer_orders, get_product_bundle, product_questions, product_reviews, seller_orders
from app.services.terms import BOILERPLATE_KEYS
from app.services.weather import insurance_state
from app.services.welfare import pending_survey_for
from app.templating import render
from app.utils import age_on, now_utc, today_ist

router = APIRouter(include_in_schema=False)
FARM_TABS = ["overview", "branches", "team", "hiring", "payroll", "ledger", "shop", "insurance", "weather", "proposals", "settings"]
TAB_PERM = {"team": "view", "hiring": "hiring", "payroll": "payroll", "ledger": "ledger", "shop": "shop", "settings": "view",
            "insurance": "view", "weather": "view", "proposals": "view", "branches": "view", "overview": "view"}


async def _nav(request: Request, db: AsyncSession, user: User | None) -> None:
    request.state.unread = (await nav_counts(db, user))["unread"]


# ------------------------------------------------------------------ public
@router.get("/", response_class=HTMLResponse)
async def index(request: Request, user: User | None = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await _nav(request, db, user)
    stats = (await db.execute(select(
        select(func.count()).select_from(Farm).where(Farm.status == "active").scalar_subquery(),
        select(func.count()).select_from(FarmMember).where(FarmMember.status != "ended").scalar_subquery(),
        select(func.count()).select_from(JobPosting).where(JobPosting.status == "open").scalar_subquery(),
        select(func.count()).select_from(Product).where(Product.status == "active").scalar_subquery(),
    ))).one()
    return render(request, "index.html", {"stats": {"farms": stats[0], "workers": stats[1], "jobs": stats[2], "products": stats[3]},
                                          "stress": await latest_stress(db)})


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, reason: str | None = None, d: str | None = None, user: User | None = Depends(current_user)):
    """`reason` says why the server signed this device out; device/time details only come from the signed `d` token."""
    from datetime import datetime

    from app.security.sessions import REASONS
    from app.security.tokens import verify_purpose
    from app.utils import IST

    if user:
        return RedirectResponse("/dashboard", 303)
    notice = None
    if reason in REASONS:
        info = (verify_purpose("signout", d) if d and len(d) < 800 else None) or {}
        at = info.get("at")
        notice = {"reason": reason, "device": info.get("device"), "ip": info.get("ip"),
                  "when": datetime.fromtimestamp(int(at), IST).strftime("%d-%m-%Y %H:%M IST") if isinstance(at, (int, float)) else None}
    return render(request, "auth/login.html", {"signout": notice})


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, user: User | None = Depends(current_user)):
    if user:
        return RedirectResponse("/dashboard", 303)
    return render(request, "auth/signup.html")


@router.get("/forgot", response_class=HTMLResponse)
async def forgot_page(request: Request):
    return render(request, "auth/forgot.html")


@router.get("/legal/{doc}", response_class=HTMLResponse)
async def legal(request: Request, doc: str, user: User | None = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if doc not in ("terms", "privacy", "disclaimer", "accessibility"):
        raise NotFound()
    await _nav(request, db, user)
    return render(request, "legal.html", {"doc": doc, "boiler_keys": BOILERPLATE_KEYS})


@router.get("/offline", response_class=HTMLResponse)
async def offline(request: Request):
    return render(request, "offline.html")


# ------------------------------------------------------------------ onboarding / dashboard
@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding(request: Request, user: User = Depends(require_active), db: AsyncSession = Depends(get_db)):
    await _nav(request, db, user)
    return render(request, "onboarding.html")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    if user.status == "banned":
        return RedirectResponse("/banned", 303)
    if user.is_admin:
        return RedirectResponse("/admin", 303)
    await _nav(request, db, user)
    ctx: dict[str, Any] = {"today": today_ist()}
    invitations = (await db.execute(select(Invitation, Farm.name).join(Farm, Farm.id == Invitation.farm_id).where(
        Invitation.phone == user.phone, Invitation.status == "pending"))).all()
    ctx["invitations"] = invitations
    ctx["working_at"] = await _working_farm(db, user) if invitations else None
    roles = await roles_by_farm(db, user)
    led = [fid for fid, r in roles.items() if r in ("owner", "co_owner", "manager", "accountant")]
    if led:
        farms = list((await db.execute(select(Farm).where(Farm.id.in_(led)).order_by(Farm.created_at))).scalars())
        ctx["farms"] = farms
        ctx["roles"] = roles
        ctx["dues"] = await farms_dues_summary(db, led)
        ctx["halts"] = await active_halts(db, led)
        ctx["proposals"] = (await db.execute(select(FarmProposal, Farm.name).join(Farm, Farm.id == FarmProposal.farm_id).where(
            FarmProposal.farm_id.in_(led), FarmProposal.status == "pending"))).all()
        ctx["questions"] = (await db.execute(select(func.count()).select_from(ProductQuestion).join(Product, Product.id == ProductQuestion.product_id)
                                             .where(Product.farm_id.in_(led), ProductQuestion.status == "pending_owner"))).scalar_one()
        ctx["alerts"] = (await db.execute(select(WeatherReport, Branch.name).join(Branch, Branch.id == WeatherReport.branch_id).where(
            WeatherReport.farm_id.in_(led), WeatherReport.report_date >= today_ist() - timedelta(days=1))
            .order_by(WeatherReport.report_date.desc()))).all()
        ctx["billing_due"] = next_due_date(today_ist(), int((await get_all(db))["billing_day"]))
        ctx["gstins"] = (await db.execute(select(GstinVerification).where(GstinVerification.user_id == user.id,
                                                                          GstinVerification.status == "verified"))).scalars().all()
    jobs = await _my_jobs(db, user)
    if jobs:
        ctx["jobs"] = jobs
        ctx["billing_due"] = next_due_date(today_ist(), int((await get_all(db))["billing_day"]))
        await db.commit()
    ctx["survey"] = await pending_survey_for(db, user)
    ctx["applications"] = (await db.execute(select(JobApplication, JobPosting.title, Farm.name)
                                            .join(JobPosting, JobPosting.id == JobApplication.posting_id)
                                            .join(Farm, Farm.id == JobApplication.farm_id)
                                            .where(JobApplication.applicant_id == user.id)
                                            .order_by(JobApplication.created_at.desc()).limit(5))).all()
    if user.role == "buyer" or not (led or jobs):
        ctx["orders"] = (await buyer_orders(db, user.id, limit=5))
    return render(request, "dashboard.html", ctx)


async def _working_farm(db: AsyncSession, user: User | None) -> dict[str, str] | None:
    """Where the person works now: {"farm": name, "status": "active" | "notice"} (one working farm at a time; own /
    co-owned farms don't count). An active job blocks joining another farm; a notice period does not (that job then ends)."""
    if user is None:
        return None
    from app.models.farm import JOB_ROLES

    row = (await db.execute(select(Farm.name, FarmMember.status).join(FarmMember, FarmMember.farm_id == Farm.id).where(
        FarmMember.user_id == user.id, FarmMember.status != "ended", FarmMember.role.in_(JOB_ROLES))
        .order_by(FarmMember.status).limit(1))).first()          # 'active' sorts before 'notice'
    return {"farm": row[0], "status": row[1]} if row else None


async def _my_jobs(db: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Every job the person holds — or left in the last 60 days, or is still owed money for: pay, recent payments and
    WHO TO CALL (the farm owner and its managers: tap to call or WhatsApp). Constant queries for any number of jobs, plus
    one accrual refresh per current farm (a person has very few)."""
    from app.services.payroll import ensure_accruals_for_farm, farm_money

    rows = (await db.execute(select(FarmMember, Farm, Branch).join(Farm, Farm.id == FarmMember.farm_id)
                             .outerjoin(Branch, Branch.id == FarmMember.branch_id)
                             .where(FarmMember.user_id == user.id, FarmMember.role != "co_owner")
                             .order_by(FarmMember.joined_on.desc()).limit(20))).all()
    if not rows:
        return []
    recent = today_ist() - timedelta(days=60)
    for fid in dict.fromkeys(m.farm_id for m, _, _ in rows if m.status != "ended" or (m.ended_on and m.ended_on >= recent)):
        await ensure_accruals_for_farm(db, fid)
    money = await farm_money(db, rows[0][0].farm_id, members=[r[0] for r in rows])
    keep = [r for r in rows if r[0].status != "ended" or (r[0].ended_on and r[0].ended_on >= recent) or money[r[0].id].due > 0]
    if not keep:
        return []
    farm_ids = list(dict.fromkeys(m.farm_id for m, _, _ in keep))
    owners = {uid: (name, phone) for uid, name, phone in (await db.execute(
        select(User.id, User.full_name, User.phone).where(User.id.in_({f.owner_id for _, f, _ in keep}),
                                                          User.status != "deleted"))).all() if phone}
    leaders: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    for fid, name, phone, role in (await db.execute(
            select(FarmMember.farm_id, User.full_name, User.phone, FarmMember.role).join(User, User.id == FarmMember.user_id)
            .where(FarmMember.farm_id.in_(farm_ids), FarmMember.role.in_(("co_owner", "manager")), holds_role(),
                   FarmMember.user_id != user.id, User.status == "active")
            .order_by(FarmMember.role, FarmMember.joined_on))).all():
        if phone and len(leaders[fid]) < 4:
            leaders[fid].append({"name": name, "phone": phone, "role": role})
    rn = func.row_number().over(partition_by=SalaryPayment.member_id, order_by=SalaryPayment.created_at.desc()).label("rn")
    recent_pay = select(SalaryPayment.id, rn).where(SalaryPayment.member_id.in_([m.id for m, _, _ in keep])).subquery()
    pays: dict[uuid.UUID, list[SalaryPayment]] = defaultdict(list)
    for p in (await db.execute(select(SalaryPayment).join(recent_pay, recent_pay.c.id == SalaryPayment.id)
                               .where(recent_pay.c.rn <= 5).order_by(SalaryPayment.created_at.desc()))).scalars():
        pays[p.member_id].append(p)
    out = []
    for m, f, b in keep:
        contacts = ([{"name": owners[f.owner_id][0], "phone": owners[f.owner_id][1], "role": "owner"}] if f.owner_id in owners else [])
        out.append({"mem": m, "farm": f, "branch": b, "money": money[m.id], "payments": pays[m.id],
                    "contacts": contacts + leaders.get(f.id, []),
                    "paused": m.status != "ended" and effective_role(m.role, m.status, m.notice_kind) != m.role})
    return out


# ------------------------------------------------------------------ farms
@router.get("/farms/new", response_class=HTMLResponse)
async def farm_new(request: Request, user: User = Depends(require_active), db: AsyncSession = Depends(get_db)):
    if user.role != "owner":
        raise Forbidden("errors.role_not_allowed")
    await _nav(request, db, user)
    gstins = (await db.execute(select(GstinVerification).where(GstinVerification.user_id == user.id,
                                                               GstinVerification.status.in_(("verified", "pending")))
                               .order_by(GstinVerification.created_at.desc()))).scalars().all()
    cfg = await get_all(db)
    return render(request, "farms/new.html", {"gstins": gstins, "crops": CROPS, "cfg": cfg, "boiler_keys": BOILERPLATE_KEYS})


@router.get("/farms/{farm_id}", response_class=HTMLResponse)
async def farm_manage(request: Request, farm_id: uuid.UUID, tab: str = "overview", month: str | None = None, page: int = 1,
                      user: User = Depends(require_active), db: AsyncSession = Depends(get_db)):
    farm, role = await require_perm(db, farm_id, user, "view")
    if role in ("employee", "supervisor"):
        return RedirectResponse(f"/f/{farm.id}", 303)
    tab = tab if tab in FARM_TABS else "overview"
    if not can(role, TAB_PERM[tab]):
        tab = "overview"
    await _nav(request, db, user)
    branches = list((await db.execute(select(Branch).where(Branch.farm_id == farm.id).order_by(Branch.is_main.desc(), Branch.created_at))).scalars())
    halts = (await active_halts(db, [farm.id]))[farm.id]
    ctx: dict[str, Any] = {"farm": farm, "role": role, "tab": tab, "tabs": [x for x in FARM_TABS if can(role, TAB_PERM[x])],
                           "branches": branches, "halts": halts, "perms": {p: can(role, p) for p in PERMS}, "rank": RANK,
                           "today": today_ist(), "cfg": await get_all(db)}
    from app.services.terms import strong_flags

    ctx["terms_blocked"] = strong_flags(farm.terms_text or "") if farm.terms_mode == "custom" else []
    bids = [b.id for b in branches]
    if tab in ("overview", "branches"):
        crops = defaultdict(list)
        for c in (await db.execute(select(BranchCrop).where(BranchCrop.branch_id.in_(bids), BranchCrop.status == "growing")
                                   .order_by(BranchCrop.created_at))).scalars():
            crops[c.branch_id].append(c)
        ctx["crops"] = crops
        ctx["crop_list"] = CROPS
        ctx["dues"] = (await farms_dues_summary(db, [farm.id]))[farm.id]
        ctx["insurance"] = (await insurance_state(db, [farm.id]))[farm.id]
        ctx["days"] = (await db.execute(select(BranchDay).where(BranchDay.branch_id.in_(bids), BranchDay.day >= today_ist() - timedelta(days=7))
                                        .order_by(BranchDay.day))).scalars().all()
        ctx["proposals_pending"] = (await db.execute(select(func.count()).select_from(FarmProposal).where(
            FarmProposal.farm_id == farm.id, FarmProposal.status == "pending"))).scalar_one()
    if tab in ("team", "payroll"):
        ctx["rows"] = await member_rows(db, farm.id)
        ctx["invitations"] = (await db.execute(select(Invitation).where(Invitation.farm_id == farm.id, Invitation.status == "pending"))).scalars().all()
        ctx["payments"] = (await db.execute(select(SalaryPayment).where(SalaryPayment.farm_id == farm.id)
                                            .order_by(SalaryPayment.created_at.desc()).limit(30))).scalars().all()
        ctx["billing_due"] = next_due_date(today_ist(), int(ctx["cfg"]["billing_day"]))
        await db.commit()
    if tab == "hiring":
        ctx["postings"] = (await db.execute(select(JobPosting).where(JobPosting.farm_id == farm.id).order_by(JobPosting.created_at.desc()))).scalars().all()
        ctx["applications"] = (await db.execute(select(JobApplication, User.full_name, User.photo_path, User.phone, User.dob, JobPosting.title)
                                                .join(User, User.id == JobApplication.applicant_id)
                                                .join(JobPosting, JobPosting.id == JobApplication.posting_id)
                                                .where(JobApplication.farm_id == farm.id, JobApplication.status == "applied")
                                                .order_by(JobApplication.created_at))).all()
    if tab == "ledger":
        from app.services.statements import farm_statement

        ctx["statement"] = await farm_statement(db, farm.id, month, page)
    if tab == "shop":
        shop = (await db.execute(select(Shop).where(Shop.farm_id == farm.id))).scalar_one_or_none()
        ctx["shop"] = shop
        if shop:
            ctx["products"] = (await db.execute(select(Product).where(Product.shop_id == shop.id).order_by(Product.created_at.desc()))).scalars().all()
            ctx["orders"] = await seller_orders(db, farm.id, limit=60)
            from app.services.shop import shop_open_orders

            ctx["shop_close"] = await shop_open_orders(db, shop.id)
            ctx["questions"] = (await db.execute(select(ProductQuestion, Product.name).join(Product, Product.id == ProductQuestion.product_id)
                                                 .where(Product.farm_id == farm.id, ProductQuestion.status == "pending_owner")
                                                 .order_by(ProductQuestion.created_at))).all()
        from app.models.shop import PRODUCT_CATEGORIES, UNITS

        ctx["categories"], ctx["units"] = PRODUCT_CATEGORIES, UNITS
    if tab == "insurance":
        ctx["policies"] = (await db.execute(select(InsurancePolicy).where(InsurancePolicy.farm_id == farm.id)
                                            .order_by(InsurancePolicy.valid_to.desc()))).scalars().all()
        ctx["insurance"] = (await insurance_state(db, [farm.id]))[farm.id]
    if tab == "weather":
        ctx["reports"] = (await db.execute(select(WeatherReport).distinct(WeatherReport.branch_id).where(WeatherReport.branch_id.in_(bids))
                                           .order_by(WeatherReport.branch_id, WeatherReport.report_date.desc()))).scalars().all()
    if tab == "proposals":
        props = (await db.execute(select(FarmProposal).where(FarmProposal.farm_id == farm.id).order_by(FarmProposal.created_at.desc()).limit(50))).scalars().all()
        votes = defaultdict(dict)
        if props:
            for v in (await db.execute(select(ProposalVote).where(ProposalVote.proposal_id.in_([p.id for p in props])))).scalars():
                votes[v.proposal_id][v.user_id] = v.vote
        coowners = (await db.execute(select(User.id, User.full_name).join(FarmMember, FarmMember.user_id == User.id).where(
            FarmMember.farm_id == farm.id, FarmMember.role == "co_owner", FarmMember.status != "ended"))).all()
        owner = await db.get(User, farm.owner_id)
        # a co-owner being removed does not vote on their own removal
        excluded: dict[uuid.UUID, uuid.UUID] = {}
        removals = {p.id: p.payload.get("member_id") for p in props if p.action_type == "coowner.remove"}
        if removals:
            mids = set()
            for v in removals.values():
                try:
                    mids.add(uuid.UUID(str(v)))
                except ValueError:
                    continue
            uid_of = dict((await db.execute(select(FarmMember.id, FarmMember.user_id).where(FarmMember.id.in_(mids)))).all()) if mids else {}
            for pid, v in removals.items():
                try:
                    excluded[pid] = uid_of.get(uuid.UUID(str(v)))
                except ValueError:
                    continue
        ctx.update(proposals=props, votes=votes, voters=[(owner.id, owner.full_name), *coowners], excluded=excluded)
    return render(request, "farms/manage.html", ctx)


@router.get("/f/{farm_id}", response_class=HTMLResponse)
async def farm_public(request: Request, farm_id: uuid.UUID, user: User | None = Depends(current_user), db: AsyncSession = Depends(get_db)):
    farm = await db.get(Farm, farm_id)
    if not farm or farm.status in ("rejected",):
        raise NotFound("errors.farm_not_found")
    await _nav(request, db, user)
    branches = list((await db.execute(select(Branch).where(Branch.farm_id == farm.id, Branch.status != "closed"))).scalars())
    crops = defaultdict(list)
    for c in (await db.execute(select(BranchCrop).where(BranchCrop.branch_id.in_([b.id for b in branches]), BranchCrop.status == "growing"))).scalars():
        crops[c.branch_id].append(c.name)
    policies = (await db.execute(select(InsurancePolicy).where(InsurancePolicy.farm_id == farm.id, InsurancePolicy.status.in_(("verified", "pending")))
                                 .order_by(InsurancePolicy.valid_to.desc()))).scalars().all()
    shop = (await db.execute(select(Shop).where(Shop.farm_id == farm.id))).scalar_one_or_none()
    owner = await db.get(User, farm.owner_id)
    jobs = (await db.execute(select(JobPosting).where(JobPosting.farm_id == farm.id, JobPosting.status == "open"))).scalars().all()
    return render(request, "farms/public.html", {"farm": farm, "branches": branches, "crops": crops, "policies": policies,
                                                 "shop": shop, "owner": owner, "jobs": jobs, "boiler_keys": BOILERPLATE_KEYS,
                                                 "halts": (await active_halts(db, [farm.id]))[farm.id], "cfg": await get_all(db)})


@router.get("/payments/{payment_id}", response_class=HTMLResponse)
async def payment_proof(request: Request, payment_id: uuid.UUID, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    pay = await db.get(SalaryPayment, payment_id)
    if not pay:
        raise NotFound()
    farm = await db.get(Farm, pay.farm_id)
    if pay.employee_id != user.id and not user.is_admin:
        await require_perm(db, farm.id, user, "ledger")
    contact = None
    if pay.employee_id == user.id:           # the worker can call the owner about this payment
        owner = await db.get(User, farm.owner_id)
        if owner and owner.status != "deleted" and owner.phone:
            contact = {"name": owner.full_name, "phone": owner.phone}
    await _nav(request, db, user)
    return render(request, "payment.html", {"pay": pay, "farm": farm, "contact": contact})


async def _proof_payment(db: AsyncSession, token: str) -> SalaryPayment:
    """A salary-proof link is valid only for one payment AND the employee it was sent to, until it expires."""
    from app.security.tokens import verify_purpose

    data = verify_purpose("payproof", token) if len(token) < 1000 else None
    if not data:
        raise NotFound()
    try:
        pay = await db.get(SalaryPayment, uuid.UUID(str(data.get("p"))))
    except ValueError:
        pay = None
    if not pay or str(pay.employee_id) != str(data.get("u")) or not pay.upload_id:
        raise NotFound()
    return pay


_PRIVATE = {"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer"}


@router.get("/proof/{token}", response_class=HTMLResponse)
@limiter.limit("30/minute", key_func=ip_key)
async def payment_proof_link(request: Request, token: str, db: AsyncSession = Depends(get_db)):
    """Opened from the SMS / WhatsApp / e-mail sent to the worker: no sign-in needed, private, expiring."""
    from datetime import datetime

    from app.security.tokens import verify_purpose
    from app.utils import IST

    pay = await _proof_payment(db, token)
    farm = await db.get(Farm, pay.farm_id)
    exp = datetime.fromtimestamp(int(verify_purpose("payproof", token)["exp"]), IST).strftime("%d-%m-%Y")
    resp = render(request, "proof.html", {"pay": pay, "farm": farm, "token": token, "expires": exp})
    resp.headers.update(_PRIVATE)
    return resp


@router.get("/proof/{token}/image")
@limiter.limit("60/minute", key_func=ip_key)
async def payment_proof_image(request: Request, token: str, db: AsyncSession = Depends(get_db)):
    from fastapi.responses import Response

    from app.models import Upload
    from app.services.uploads import read_upload_bytes

    pay = await _proof_payment(db, token)
    up = await db.get(Upload, pay.upload_id)
    if not up or up.path == "purged":
        raise NotFound()
    return Response(await read_upload_bytes(up), media_type=up.mime, headers={
        **_PRIVATE, "Content-Disposition": f'inline; filename="salary-proof.{up.mime.split("/")[-1]}"',
        "Content-Security-Policy": "default-src 'none'; sandbox", "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "cross-origin"})   # WhatsApp's servers fetch it as the message picture


# ------------------------------------------------------------------ jobs / shop / schemes
@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request, user: User | None = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await _nav(request, db, user)
    return render(request, "jobs/index.html", {"cities": city_options()})


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: uuid.UUID, user: User | None = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(JobPosting, Farm, Branch).join(Farm, Farm.id == JobPosting.farm_id)
                            .join(Branch, Branch.id == JobPosting.branch_id).where(JobPosting.id == job_id))).first()
    if not row:
        raise NotFound("errors.job_not_open")
    await _nav(request, db, user)
    job, farm, branch = row
    crops = (await db.execute(select(BranchCrop.name).where(BranchCrop.branch_id == branch.id, BranchCrop.status == "growing"))).scalars().all()
    policies = (await db.execute(select(InsurancePolicy).where(InsurancePolicy.farm_id == farm.id, InsurancePolicy.status.in_(("verified", "pending"))))).scalars().all()
    halts = (await active_halts(db, [farm.id]))[farm.id]
    return render(request, "jobs/detail.html", {"job": job, "farm": farm, "branch": branch, "crops": crops, "policies": policies,
                                                "boiler_keys": BOILERPLATE_KEYS, "cfg": await get_all(db),
                                                "halted": any(h.branch_id in (None, branch.id) for h in halts),
                                                "working_at": await _working_farm(db, user)})


@router.get("/shop", response_class=HTMLResponse)
async def shop_page(request: Request, user: User | None = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from app.models.shop import PRODUCT_CATEGORIES

    await _nav(request, db, user)
    return render(request, "shop/index.html", {"categories": PRODUCT_CATEGORIES, "cities": city_options()})


@router.get("/shop/s/{shop_id}", response_class=HTMLResponse)
async def shop_detail(request: Request, shop_id: uuid.UUID, user: User | None = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(Shop, Farm).join(Farm, Farm.id == Shop.farm_id).where(Shop.id == shop_id))).first()
    if not row:
        raise NotFound()
    await _nav(request, db, user)
    return render(request, "shop/shop.html", {"shop": row[0], "farm": row[1]})


@router.get("/shop/p/{product_id}", response_class=HTMLResponse)
async def product_page(request: Request, product_id: uuid.UUID, user: User | None = Depends(current_user), db: AsyncSession = Depends(get_db)):
    prod, shop, farm, branch = await get_product_bundle(db, product_id)
    await _nav(request, db, user)
    reviews = await product_reviews(db, prod.id)
    qa = await product_questions(db, prod.id)
    ratings = [r["review"].rating for r in reviews]
    halted = any(h.branch_id in (None, branch.id) for h in (await active_halts(db, [farm.id]))[farm.id])
    return render(request, "shop/product.html", {"p": prod, "shop": shop, "farm": farm, "branch": branch, "reviews": reviews, "qa": qa,
                                                 "avg": round(sum(ratings) / len(ratings), 1) if ratings else None, "halted": halted})


@router.get("/cart", response_class=HTMLResponse)
async def cart_page(request: Request, user: User | None = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await _nav(request, db, user)
    farms = []
    if user:
        roles = await roles_by_farm(db, user)
        ids = [fid for fid, r in roles.items() if r in ("owner", "co_owner", "manager", "accountant")]
        if ids:
            farms = (await db.execute(select(Farm).where(Farm.id.in_(ids)))).scalars().all()
    return render(request, "shop/cart.html", {"buyer_farms": farms})


@router.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, user: User = Depends(require_active), db: AsyncSession = Depends(get_db)):
    await _nav(request, db, user)
    return render(request, "shop/orders.html", {"orders": await buyer_orders(db, user.id)})


@router.get("/schemes", response_class=HTMLResponse)
async def schemes_page(request: Request, user: User | None = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from datetime import datetime

    await _nav(request, db, user)
    lang = lang_of(request)
    items = await all_schemes(db)
    cutoff = now_utc() - timedelta(days=180)
    withdrawn = [scheme_card(x, lang) for x in items if x["status"] == "archived" and x["archived_at"]
                 and datetime.fromisoformat(x["archived_at"]) >= cutoff]
    return render(request, "schemes/index.html", {
        "schemes": [scheme_card(x, lang) for x in items if x["status"] == "active"], "withdrawn": withdrawn,
        "age": age_on(user.dob) if user else None, "states": STATES, "user_state": user.state if user else None})


@router.get("/schemes/go/{slug}", response_class=HTMLResponse)
async def scheme_go(request: Request, slug: str, user: User | None = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if not SLUG_RE.match(slug):
        raise NotFound()
    s = await get_scheme(db, slug)
    if not s:
        raise NotFound()
    await _nav(request, db, user)
    lang = lang_of(request)
    # the link is re-validated against the current allow-list on every visit (no open redirect, even for old rows)
    url = s["url"] if official_url_problem(s["url"], await scheme_domains(db)) is None else None
    replacement = None
    if s["status"] == "archived" and s["replaced_by"]:
        rep = await get_scheme(db, s["replaced_by"])
        replacement = scheme_card(rep, lang) if rep and rep["status"] == "active" else None
    if user and s["status"] == "active" and request.headers.get("sec-fetch-site", "none") in ("same-origin", "none"):
        await record_visit(db, user.id, s["uuid"])  # so this person is told if the scheme changes or is withdrawn
        await db.commit()
    return render(request, "schemes/go.html", {"s": scheme_card(s, lang), "url": url, "replacement": replacement})


# ------------------------------------------------------------------ personal
@router.get("/assistant", response_class=HTMLResponse)
async def assistant_page(request: Request, user: User = Depends(require_active), db: AsyncSession = Depends(get_db)):
    await _nav(request, db, user)
    return render(request, "assistant.html")


@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    from app.models import Notification
    from app.services.notify import render_notification

    await _nav(request, db, user)
    rows = (await db.execute(select(Notification).where(Notification.user_id == user.id).order_by(Notification.created_at.desc()).limit(100))).scalars().all()
    return render(request, "notifications.html", {"items": [render_notification(n, request.state.lang) for n in rows]})


@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    if user.status == "banned":
        return RedirectResponse("/banned", 303)
    await _nav(request, db, user)
    docs = (await db.execute(select(IdentityDocument).where(IdentityDocument.user_id == user.id))).scalars().all()
    return render(request, "account.html", {"docs": docs, "age": age_on(user.dob), "states": STATES,
                                            "statement_farms": await _owned_farms(db, user)})


async def _owned_farms(db: AsyncSession, user: User) -> list[Any]:
    """Farms whose statement this person sees in the account: the ones they own or co-own (one query)."""
    ids = [fid for fid, r in (await roles_by_farm(db, user)).items() if r in ("owner", "co_owner")]
    if not ids:
        return []
    return (await db.execute(select(Farm.id, Farm.name).where(Farm.id.in_(ids)).order_by(Farm.name))).all()


@router.get("/account/statement", response_class=HTMLResponse)
async def statement_page(request: Request, of: str = "me", month: str | None = None, page: int = 1,
                         user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """Credit / debit statement: the person's own, or — for the owner and co-owners — a farm's."""
    from app.services.statements import farm_statement, personal_statement

    await _nav(request, db, user)
    farms = await _owned_farms(db, user)
    farm = None
    if of != "me":
        if user.status != "active":         # farm money stays with active accounts (same rule as the farm pages / CSV)
            raise NotFound()
        try:
            fid = uuid.UUID(of)
        except ValueError:
            raise NotFound() from None
        farm = next((f for f in farms if f.id == fid), None)
        if farm is None:                    # not an owner / co-owner of that farm (or no such farm): nothing to see
            raise NotFound()
        st = await farm_statement(db, farm.id, month, page)
        csv = f"/farms/{farm.id}/ledger.csv"
    else:
        st = await personal_statement(db, user.id, month, page)
        csv = f"/account/statement.csv?month={st.month}"
    return render(request, "statement.html", {"st": st, "of": of if farm else "me", "farm": farm, "farms": farms, "csv": csv})


@router.get("/banned", response_class=HTMLResponse)
async def banned_page(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    if user.status != "banned":
        return RedirectResponse("/dashboard", 303)
    appeal = (await db.execute(select(UnbanRequest).where(UnbanRequest.user_id == user.id).order_by(UnbanRequest.created_at.desc()))).scalars().first()
    return render(request, "banned.html", {"appeal": appeal})


@router.get("/survey", response_class=HTMLResponse)
async def survey_page(request: Request, user: User = Depends(require_active), db: AsyncSession = Depends(get_db)):
    await _nav(request, db, user)
    s = await pending_survey_for(db, user)
    farm = await db.get(Farm, s.farm_id) if s else None
    return render(request, "survey.html", {"survey": s, "farm": farm})


@router.get("/report", response_class=HTMLResponse)
async def report_page(request: Request, user: User = Depends(require_active), db: AsyncSession = Depends(get_db)):
    from app.models.welfare import GRIEVANCE_CATEGORIES

    from app.services.perms import has_worker_job

    await _nav(request, db, user)
    can_report = user.role == "farmer" or await has_worker_job(db, user.id)
    return render(request, "report.html", {"categories": GRIEVANCE_CATEGORIES, "can_report": can_report})


@router.get("/community", response_class=HTMLResponse)
@limiter.limit("40/minute;600/day")              # a directory of people: browsing yes, harvesting no
async def community(request: Request, city: str | None = None, page: int = 1, user: User | None = Depends(current_user),
                    db: AsyncSession = Depends(get_db)):
    await _nav(request, db, user)
    page = max(1, min(page, 200))
    stmt = select(User).where(User.role == "farmer", User.status == "active", User.photo_verified.is_(True))
    if city:
        stmt = stmt.where(User.city.ilike(f"%{city.strip()[:60]}%"))
    people = (await db.execute(stmt.order_by(User.created_at.desc()).offset((page - 1) * 30).limit(31))).scalars().all()
    return render(request, "community.html", {"people": people[:30], "has_more": len(people) > 30, "page": page, "city": city or "",
                                              "cities": city_options()})


@router.get("/policies", response_class=HTMLResponse)
async def policies(request: Request, page: int = 1, user: User | None = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await _nav(request, db, user)
    page = max(1, min(page, 500))
    rows = (await db.execute(select(InsurancePolicy, Farm.name, Farm.id).join(Farm, Farm.id == InsurancePolicy.farm_id)
                             .where(Farm.status.in_(("active", "closing")), InsurancePolicy.status.in_(("verified", "pending")))
                             .order_by(Farm.name, InsurancePolicy.policy_type).offset((page - 1) * 50).limit(51))).all()
    return render(request, "policies.html", {"rows": rows[:50], "has_more": len(rows) > 50, "page": page})


# ------------------------------------------------------------------ admin
@router.get("/admin/2fa", response_class=HTMLResponse)
async def admin_2fa(request: Request, user: User = Depends(require_active)):
    if not user.is_admin:
        raise Forbidden("errors.admin_only")
    return render(request, "admin/2fa.html")


ADMIN_TABS = ["overview", "farms", "users", "reports", "cases", "appeals", "security", "schemes", "settings", "outbox", "audit"]


@router.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, tab: str = "overview", q: str | None = None, user: User = Depends(require_admin),
                db: AsyncSession = Depends(get_db)):
    tab = tab if tab in ADMIN_TABS else "overview"
    await _nav(request, db, user)
    ctx: dict[str, Any] = {"tab": tab, "tabs": ADMIN_TABS, "q": q or ""}
    if tab == "overview":
        from app.i18n import LANGUAGES, coverage
        from app.services.ai.providers import router as ai_router
        from app.services.ai.queue import ai_queue
        from app.services.redis_client import MODE

        counts = (await db.execute(select(
            select(func.count()).select_from(User).where(User.status == "active").scalar_subquery(),
            select(func.count()).select_from(Farm).where(Farm.status == "active").scalar_subquery(),
            select(func.count()).select_from(Farm).where(Farm.status == "pending_approval").scalar_subquery(),
            select(func.count()).select_from(Grievance).where(Grievance.status == "open").scalar_subquery(),
            select(func.count()).select_from(SafetyCase).where(SafetyCase.status != "resolved").scalar_subquery(),
            select(func.count()).select_from(Halt).where(Halt.lifted_at.is_(None)).scalar_subquery(),
        ))).one()
        ctx.update(counts=counts, ai=ai_router.status(), queue=ai_queue.stats(), redis=MODE,
                   runs=(await db.execute(select(JobRun).order_by(JobRun.started_at.desc()).limit(20))).scalars().all(),
                   coverage=[(l, coverage(l["code"])) for l in LANGUAGES],
                   halts=(await db.execute(select(Halt, Farm.name).join(Farm, Farm.id == Halt.farm_id).where(Halt.lifted_at.is_(None))
                                           .order_by(Halt.created_at.desc()).limit(50))).all())
    elif tab == "farms":
        from app.models import GstinVerification
        from app.services.gstin import names_match

        stmt = (select(Farm, User.full_name, User.phone, GstinVerification.legal_name).join(User, User.id == Farm.owner_id)
                .outerjoin(GstinVerification, GstinVerification.id == Farm.gstin_verification_id)
                .order_by(Farm.created_at.desc()).limit(200))
        if q:
            stmt = stmt.where(or_(Farm.name.ilike(f"%{q}%"), Farm.gstin.ilike(f"%{q}%"), User.full_name.ilike(f"%{q}%")))
        rows = (await db.execute(stmt)).all()
        ctx["rows"] = [r[:3] for r in rows]
        # name on the GST registration vs the person claiming it: a hint for the admin before approving (squatting)
        ctx["gst_names"] = {r[0].id: (r[3], names_match(r[1], r[3])) for r in rows}
        ctx["dues"] = await farms_dues_summary(db, [r[0].id for r in rows])
    elif tab == "users":
        stmt = select(User).order_by(User.created_at.desc()).limit(200)
        if q:
            stmt = stmt.where(or_(User.full_name.ilike(f"%{q}%"), User.phone.ilike(f"%{q}%"), User.email.ilike(f"%{q}%")))
        ctx["users"] = (await db.execute(stmt)).scalars().all()
    elif tab == "reports":
        reporter = User.__table__.alias("reporter")
        ctx["grievances"] = (await db.execute(select(Grievance, Farm.name, Branch.name, reporter.c.full_name, reporter.c.phone,
                                                     reporter.c.false_report_warnings)
                                              .join(Farm, Farm.id == Grievance.farm_id).outerjoin(Branch, Branch.id == Grievance.branch_id)
                                              .join(reporter, reporter.c.id == Grievance.reporter_id)
                                              .order_by(Grievance.created_at.desc()).limit(200))).all()
        ctx["surveys_bad"] = (await db.execute(select(func.count()).select_from(WellbeingSurvey).where(
            WellbeingSurvey.day_rating == "bad", WellbeingSurvey.answered_at >= today_ist() - timedelta(days=30)))).scalar_one()
    elif tab == "cases":
        ctx["cases"] = (await db.execute(select(SafetyCase, Farm.name, Branch.name).join(Farm, Farm.id == SafetyCase.farm_id)
                                         .outerjoin(Branch, Branch.id == SafetyCase.branch_id).order_by(SafetyCase.created_at.desc()).limit(100))).all()
    elif tab == "appeals":
        ctx["appeals"] = (await db.execute(select(UnbanRequest, User.full_name, User.phone, User.ban_reason).join(User, User.id == UnbanRequest.user_id)
                                           .order_by(UnbanRequest.created_at.desc()).limit(100))).all()
    elif tab == "security":
        ctx["bans"] = (await db.execute(select(IpBan).order_by(IpBan.created_at.desc()).limit(100))).scalars().all()
        from app.models import UserSanction

        ctx["sanctions"] = (await db.execute(select(UserSanction, User.full_name, User.phone).join(User, User.id == UserSanction.user_id)
                                             .order_by(UserSanction.created_at.desc()).limit(100))).all()
        ctx["banned_users"] = (await db.execute(select(User).where(User.status == "banned").order_by(User.banned_at.desc().nulls_last())
                                                .limit(100))).scalars().all()
        ctx["now"] = now_utc()
        ctx["events"] = (await db.execute(select(AbuseEvent).order_by(AbuseEvent.created_at.desc()).limit(150))).scalars().all()
        ctx["calamities"] = (await db.execute(select(CalamityEvent).order_by(CalamityEvent.created_at.desc()).limit(30))).scalars().all()
    elif tab == "settings":
        ctx["cfg"] = await get_all(db)
        ctx["wages"] = {w.state: w for w in (await db.execute(select(MinimumWage))).scalars()}
        ctx["states"] = STATES
        ctx["holidays"] = (await db.execute(select(Holiday).where(Holiday.day >= today_ist() - timedelta(days=30)).order_by(Holiday.day))).scalars().all()
    elif tab == "schemes":
        lang = lang_of(request)
        ctx["schemes"] = (await db.execute(select(GovScheme).order_by(GovScheme.status, GovScheme.builtin.desc(), GovScheme.slug))).scalars().all()
        ctx["visits"] = dict((await db.execute(select(SchemeVisit.scheme_id, func.count()).group_by(SchemeVisit.scheme_id))).all())
        ctx["rules_txt"] = {x.id: rules_text(x.rules or {}, lambda k, **kw: t(k, lang, **kw)) for x in ctx["schemes"]}
        ctx["domains"] = await scheme_domains(db)
        ctx["states"] = STATES
        ctx["today"] = today_ist()
    elif tab == "outbox":
        ctx["messages"] = (await db.execute(select(OutboundMessage).order_by(OutboundMessage.created_at.desc()).limit(150))).scalars().all()
    elif tab == "audit":
        ctx["logs"] = (await db.execute(select(AuditLog, User.full_name).outerjoin(User, User.id == AuditLog.actor_id)
                                        .order_by(AuditLog.created_at.desc()).limit(200))).all()
    return render(request, "admin/index.html", ctx)


_PUBLIC_TIMELINE = ("t", "users", "rps", "fail_s", "p50", "p95", "p99")


def _readable_check(detail: str | None) -> str:
    """Older runs stored some checks by their code name ("spammer_ip_banned (11 attempts)"): show words instead."""
    import re

    m = re.match(r"^([a-z0-9_]+)( \(.*\))?$", detail or "")
    return (m.group(1).replace("_", " ").capitalize() + (m.group(2) or "")) if m else (detail or "")


async def latest_stress(db: AsyncSession) -> Any:
    """Headline of the most recent stress / soak run (for the home page) — None if there is none."""
    return (await db.execute(select(LoadTestRun.id, LoadTestRun.summary, LoadTestRun.started_at, LoadTestRun.passed,
                                    LoadTestRun.config).where(LoadTestRun.kind.in_(("stress", "soak")))
                             .order_by(LoadTestRun.started_at.desc()).limit(1))).first()


@router.get("/stress-test", response_class=HTMLResponse)
async def stress_results(request: Request, run: uuid.UUID | None = None, user: User | None = Depends(current_user),
                         db: AsyncSession = Depends(get_db)):
    """Public, read-only Locust results: headline numbers, load and latency over time, run history and the database
    correctness checks. Endpoint names, error texts, server internals, addresses and bug details stay on the
    superadmin's /admin/production-test."""
    await _nav(request, db, user)
    runs = (await db.execute(select(LoadTestRun.id, LoadTestRun.kind, LoadTestRun.started_at, LoadTestRun.passed,
                                    LoadTestRun.peak_users, LoadTestRun.total_requests, LoadTestRun.total_failures,
                                    LoadTestRun.summary, LoadTestRun.config)
                             .where(LoadTestRun.kind.in_(("stress", "soak")))
                             .order_by(LoadTestRun.started_at.desc()).limit(12))).all()
    selected = None
    if run is not None:
        selected = await db.get(LoadTestRun, run)
        if not selected or selected.kind not in ("stress", "soak"):
            raise NotFound()
    elif runs:
        selected = await db.get(LoadTestRun, runs[0].id)
    ctx: dict[str, Any] = {"runs": runs, "run": selected}
    if selected:
        ctx.update(
            summary={k: (selected.summary or {}).get(k) for k in ("requests", "failures", "error_rate", "five_xx", "peak_users",
                                                                 "peak_rps", "p50", "p95", "p99", "invariants_passed",
                                                                 "invariants_total")},
            timeline=[{k: p.get(k) for k in _PUBLIC_TIMELINE} for p in (selected.timeline or [])],
            checks=[{"ok": bool(i.get("ok")), "detail": _readable_check(i.get("detail"))} for i in (selected.invariants or [])
                    if i.get("kind") != "info"],
            workers=(selected.config or {}).get("workers", 1), findings=len(selected.findings or []))
    return render(request, "stress.html", ctx)


@router.get("/admin/production-test", response_class=HTMLResponse)
async def production_test(request: Request, run: uuid.UUID | None = None, user: User = Depends(require_admin),
                          db: AsyncSession = Depends(get_db)):
    """Locust load-test results (imported by scripts/run_loadtest.py), charted with Chart.js."""
    await _nav(request, db, user)
    runs = (await db.execute(select(LoadTestRun.id, LoadTestRun.label, LoadTestRun.kind, LoadTestRun.started_at, LoadTestRun.passed,
                                    LoadTestRun.peak_users, LoadTestRun.total_requests, LoadTestRun.total_failures, LoadTestRun.duration_s,
                                    LoadTestRun.summary, LoadTestRun.config)
                             .order_by(LoadTestRun.started_at.desc()).limit(50))).all()
    selected = None
    if run is not None:
        selected = await db.get(LoadTestRun, run)
        if not selected:
            raise NotFound()
    elif runs:
        pick = next((r for r in runs if r.kind == "stress"), runs[0])
        selected = await db.get(LoadTestRun, pick.id)
    data = None
    if selected:
        data = {"summary": selected.summary, "timeline": selected.timeline, "endpoints": selected.endpoints,
                "failures": selected.failures, "server": selected.server, "invariants": selected.invariants,
                "findings": selected.findings, "config": selected.config, "duration_s": selected.duration_s,
                "peak_users": selected.peak_users, "total_requests": selected.total_requests,
                "total_failures": selected.total_failures, "passed": selected.passed, "label": selected.label,
                "kind": selected.kind, "started_at": selected.started_at.isoformat()}
    return render(request, "admin/production_test.html", {"runs": runs, "run": selected, "data": data, "tabs": ADMIN_TABS,
                                                          "tab": "production"})
