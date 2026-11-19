"""Farm shops.

Integrity guarantees (no double buys / refunds / inventory drift):
* every stock change is a single conditional UPDATE (stock never < 0) + a timestamped InventoryMovement with a
  UNIQUE idempotency key; optimistic `version` checks for owner edits;
* orders are unique per (buyer, idempotency key) and duplicate carts within 60 s are rejected;
* status changes are conditional UPDATEs (`WHERE status = :from`) so a cancel/refund can only happen once.
Only Cash-on-Delivery or pay-at-pickup; the buyer pays the courier charge.
"""
from __future__ import annotations

import secrets
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import AppError, Conflict, NotFound
from app.models import (
    Branch, BranchCrop, Farm, InventoryMovement, LedgerEntry, Order, OrderEvent, OrderItem, PersonalLedgerEntry, Product,
    ProductQuestion, Review, Shop, User,
)
from app.services.ai.embeddings import embed_one, embed_one_safe
from app.services.geo import bbox_around, haversine_km
from app.services.halts import halted_scope
from app.services.notify import farm_leader_ids, notify, notify_many
from app.utils import format_inr, now_ist, now_utc, parse_json_loose, today_ist

OPEN = ("placed", "confirmed", "dispatched", "ready_for_pickup")
# when a shop closes: orders not yet with a courier are cancelled in one step; an order already with the courier (the buyer
# is paying for that trip) is never cancelled — it must be delivered, and the shop stays "closing" until it is
CANCEL_ON_SHOP_CLOSE = ("placed", "confirmed", "ready_for_pickup")
DELIVER_BEFORE_CLOSE = ("dispatched",)
TRANSITIONS = {
    "placed": {"confirmed", "cancelled"},
    "confirmed": {"dispatched", "ready_for_pickup", "cancelled"},
    "dispatched": {"delivered", "cancelled"},
    "ready_for_pickup": {"picked_up", "cancelled"},
}
CENT = Decimal("0.01")


def courier_charge(shop: Shop, distance_km: float | None) -> Decimal:
    d = Decimal(str(round(distance_km or 0, 1)))
    return (Decimal(shop.courier_base_fee) + Decimal(shop.courier_per_km) * d).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


_ORDER_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O or 1/I/L: easy to read out on the phone


def new_order_no() -> str:
    """KS<yymmdd>-<8 random characters> — 31⁸ ≈ 8.5·10¹¹ numbers a day (the old 6 hex digits, 1.7·10⁷, collided in the
    3-hour soak after ~9 000 orders). A collision is still possible in theory: place_order draws again."""
    return f"KS{now_ist():%y%m%d}-" + "".join(secrets.choice(_ORDER_ALPHABET) for _ in range(8))   # the Indian date


async def adjust_stock(db: AsyncSession, product_id: uuid.UUID, delta: Decimal, reason: str, *, idem: str,
                       actor_id: uuid.UUID | None = None, order_id: uuid.UUID | None = None,
                       expected_version: int | None = None, client_ts: datetime | None = None) -> Decimal:
    prev = (await db.execute(select(InventoryMovement.stock_after).where(InventoryMovement.idempotency_key == idem))).scalar_one_or_none()
    if prev is not None:
        return prev  # idempotent replay
    stmt = (update(Product).where(Product.id == product_id, Product.stock + delta >= 0)
            .values(stock=Product.stock + delta, version=Product.version + 1, updated_at=func.now())
            .returning(Product.stock))
    if expected_version is not None:
        stmt = stmt.where(Product.version == expected_version)
    new_stock = (await db.execute(stmt)).scalar_one_or_none()
    if new_stock is None:
        if expected_version is not None:
            cur = (await db.execute(select(Product.version).where(Product.id == product_id))).scalar_one_or_none()
            if cur is not None and cur != expected_version:
                raise Conflict("errors.stale_version")
        raise AppError("errors.out_of_stock", status=409)
    db.add(InventoryMovement(product_id=product_id, delta=delta, stock_after=new_stock, reason=reason, order_id=order_id,
                             actor_id=actor_id, idempotency_key=idem, client_ts=client_ts))
    return new_stock


async def place_order(db: AsyncSession, buyer: User, *, shop: Shop, branch: Branch, farm: Farm,
                      lines: list[tuple[Product, Decimal]], fulfilment: str, address: str | None, lat: float | None,
                      lon: float | None, idem: str, client_ts: datetime | None, buyer_farm_id: uuid.UUID | None) -> Order:
    existing = (await db.execute(select(Order).where(Order.buyer_id == buyer.id, Order.idempotency_key == idem))).scalar_one_or_none()
    if existing:
        return existing
    recent = (await db.execute(select(Order.id).where(
        Order.buyer_id == buyer.id, Order.shop_id == shop.id, Order.status == "placed",
        Order.created_at >= now_utc() - timedelta(seconds=60)))).first()
    if recent:
        items_prev = {(r.product_id, r.qty) for r in (await db.execute(select(OrderItem).where(OrderItem.order_id == recent[0]))).scalars()}
        if items_prev == {(p.id, q) for p, q in lines}:
            raise Conflict("errors.duplicate_order")
    order_id = uuid.uuid4()
    distance = haversine_km(lat, lon, branch.lat, branch.lon) if (lat is not None and lon is not None) else None
    charge = courier_charge(shop, distance) if fulfilment == "delivery" else Decimal("0")
    subtotal = Decimal("0")
    items = []
    for p, qty in lines:
        line_total = (Decimal(p.price) * qty).quantize(CENT, rounding=ROUND_HALF_UP)
        subtotal += line_total
        items.append(OrderItem(order_id=order_id, product_id=p.id, product_name=p.name, unit=p.unit, qty=qty,
                               unit_price=p.price, line_total=line_total))
    order = Order(id=order_id, order_no=new_order_no(), buyer_id=buyer.id, buyer_farm_id=buyer_farm_id, buyer_name=buyer.full_name,
                  buyer_phone=buyer.phone, shop_id=shop.id, farm_id=farm.id, branch_id=branch.id, fulfilment=fulfilment,
                  status="placed", payment_method="cod" if fulfilment == "delivery" else "pay_at_pickup", subtotal=subtotal,
                  courier_charge=charge, total=subtotal + charge, delivery_address=(address or None) if fulfilment == "delivery" else None,
                  buyer_lat=lat, buyer_lon=lon, distance_km=round(distance, 1) if distance is not None else None,
                  idempotency_key=idem, client_ts=client_ts)
    for attempt in range(4):
        try:
            async with db.begin_nested():
                db.add(order)
                await db.flush()  # the order row must exist before stock movements reference it
            break
        except IntegrityError as exc:
            msg = str(getattr(exc, "orig", exc))
            if "uq_order_idem" in msg:      # the same checkout raced itself (two devices / double tap): one order only
                again = (await db.execute(select(Order).where(Order.buyer_id == buyer.id, Order.idempotency_key == idem))
                         ).scalar_one_or_none()
                if again is not None:
                    return again
                raise
            if "order_no" not in msg or attempt == 3:
                raise
            order.order_no = new_order_no()  # drawn at the same instant by another checkout: draw another one
    # the owners may have voted to close the shop while this checkout waited for the shop row (the order insert waits
    # behind the closing vote's lock): re-read the committed status — a closing shop takes no new orders
    if (await db.execute(select(Shop.status).where(Shop.id == shop.id))).scalar_one() != "open":
        raise Conflict("errors.shop_closed")
    db.add_all(items)
    db.add(OrderEvent(order_id=order.id, status="placed", actor_id=buyer.id))
    leaders = await farm_leader_ids(db, farm.id, ("co_owner", "manager"))
    await notify_many(db, leaders, "order_new", title_key="notif.order_new.title", body_key="notif.order_new.body",
                      params={"no": order.order_no, "total": format_inr(order.total), "buyer": buyer.full_name},
                      link=f"/farms/{farm.id}?tab=shop", severity="success")
    await notify(db, buyer.id, "order_placed", title_key="notif.order_placed.title", body_key="notif.order_placed.body",
                 params={"no": order.order_no, "total": format_inr(order.total), "shop": shop.name}, link="/orders", severity="success")
    # Stock last: the row locks are then held only until the commit right after this. Atomic conditional decrements —
    # any shortage rolls back the whole order (notifications included). Rows are always locked in product-id order,
    # so two orders listing the same items in different orders can never deadlock each other.
    for p, qty in sorted(lines, key=lambda line: str(line[0].id)):
        await adjust_stock(db, p.id, -qty, "sale", idem=f"order:{order_id}:{p.id}", actor_id=buyer.id, order_id=order_id, client_ts=client_ts)
    return order


async def _restore_items(db: AsyncSession, orders: list[Order], actor_id: uuid.UUID | None) -> None:
    if not orders:
        return
    items = (await db.execute(select(OrderItem).where(OrderItem.order_id.in_([o.id for o in orders])))).scalars().all()
    for it in sorted(items, key=lambda i: (str(i.product_id), str(i.order_id))):  # same lock order as place_order
        await adjust_stock(db, it.product_id, Decimal(it.qty), "cancel_restore", idem=f"restore:{it.order_id}:{it.product_id}",
                           actor_id=actor_id, order_id=it.order_id)


async def _record_sale(db: AsyncSession, order: Order) -> None:
    shop = await db.get(Shop, order.shop_id)
    farm = await db.get(Farm, order.farm_id)
    db.add(LedgerEntry(farm_id=order.farm_id, branch_id=order.branch_id, entry_type="sale", direction="in", amount=order.total,
                       category="shop", description=f"Order {order.order_no} ({order.fulfilment}, incl. courier {order.courier_charge})",
                       counterparty_user_id=order.buyer_id, counterparty_name=order.buyer_name, reference_type="order",
                       reference_id=str(order.id), occurred_on=today_ist(), idempotency_key=f"sale:{order.id}"))
    db.add(PersonalLedgerEntry(user_id=order.buyer_id, entry_type="purchase", direction="out", amount=order.total,
                               description=f"Order {order.order_no} from {shop.name if shop else ''}", farm_id=order.farm_id,
                               farm_name=farm.name if farm else None, reference_type="order", reference_id=str(order.id),
                               idempotency_key=f"purchase:{order.id}"))
    if order.buyer_farm_id:
        db.add(LedgerEntry(farm_id=order.buyer_farm_id, entry_type="purchase", direction="out", amount=order.total,
                           category="shop_purchase", description=f"Purchase {order.order_no} from {shop.name if shop else ''}",
                           counterparty_name=shop.name if shop else None, reference_type="order", reference_id=str(order.id),
                           occurred_on=today_ist(), idempotency_key=f"purchase-farm:{order.id}"))


async def transition_order(db: AsyncSession, order: Order, to: str, actor: User, *, as_seller: bool, note: str | None = None) -> Order:
    frm = order.status
    if to not in TRANSITIONS.get(frm, set()):
        raise Conflict("errors.order_bad_transition")
    if not as_seller and not (to == "cancelled" and frm in ("placed", "confirmed")):
        raise AppError("errors.forbidden", status=403)
    if to == "dispatched" and order.fulfilment != "delivery" or to == "ready_for_pickup" and order.fulfilment != "pickup":
        raise Conflict("errors.order_bad_transition")
    values: dict[str, Any] = {"status": to, "updated_at": func.now()}
    if to == "cancelled":
        values.update(cancel_reason=(note or "")[:500] or None, cancelled_by_id=actor.id)
    res = await db.execute(update(Order).where(Order.id == order.id, Order.status == frm).values(**values).returning(Order.id))
    if res.scalar_one_or_none() is None:
        raise Conflict("errors.order_state_changed")
    await db.refresh(order)
    db.add(OrderEvent(order_id=order.id, status=to, actor_id=actor.id, note=(note or None)))
    if to == "cancelled":
        await _restore_items(db, [order], actor.id)
    if to in ("delivered", "picked_up"):
        await _record_sale(db, order)
    if to in ("delivered", "picked_up", "cancelled"):
        await finish_closing_shop(db, order.shop_id)
    if as_seller:
        await notify(db, order.buyer_id, "order_status", title_key="notif.order_status.title", body_key=f"order.status.{to}",
                     params={"no": order.order_no}, link="/orders", severity="info")
    else:
        await notify_many(db, await farm_leader_ids(db, order.farm_id), "order_cancelled_by_buyer",
                          title_key="notif.order_buyer_cancel.title", body_key="notif.order_buyer_cancel.body",
                          params={"no": order.order_no}, link=f"/farms/{order.farm_id}?tab=shop", severity="warning")
    return order


async def cancel_open_orders(db: AsyncSession, *, farm_id: uuid.UUID, branch_id: uuid.UUID | None, reason_key: str,
                             actor_id: uuid.UUID | None, farm_name: str) -> int:
    stmt = select(Order).where(Order.farm_id == farm_id, Order.status.in_(OPEN)).with_for_update()
    if branch_id:
        stmt = stmt.where(Order.branch_id == branch_id)
    orders = list((await db.execute(stmt)).scalars())
    if not orders:
        return 0
    ids = [o.id for o in orders]
    await db.execute(update(Order).where(Order.id.in_(ids)).values(status="cancelled", cancel_reason=reason_key,
                                                                   cancelled_by_id=actor_id, updated_at=func.now()))
    db.add_all([OrderEvent(order_id=i, status="cancelled", actor_id=actor_id, note=reason_key) for i in ids])
    await _restore_items(db, orders, actor_id)
    await notify_many(db, [o.buyer_id for o in orders], "order_cancelled_farm", title_key="notif.order_farm_cancel.title",
                      body_key="notif.order_farm_cancel.body", params={"farm": farm_name, "reason_key": reason_key},
                      link="/orders", severity="warning")
    return len(orders)


async def shop_open_orders(db: AsyncSession, shop_id: uuid.UUID) -> tuple[int, int]:
    """(orders that closing the shop cancels, orders that must still be delivered) — one query."""
    rows = dict((await db.execute(select(Order.status, func.count()).where(Order.shop_id == shop_id, Order.status.in_(OPEN))
                                  .group_by(Order.status))).all())
    return sum(rows.get(s, 0) for s in CANCEL_ON_SHOP_CLOSE), sum(rows.get(s, 0) for s in DELIVER_BEFORE_CLOSE)


async def cancel_shop_orders(db: AsyncSession, shop: Shop, actor_id: uuid.UUID | None, farm_name: str) -> int:
    """Cancel every open order of a closing shop that is not yet with a courier (buyers told, stock restored)."""
    orders = list((await db.execute(select(Order).where(Order.shop_id == shop.id, Order.status.in_(CANCEL_ON_SHOP_CLOSE))
                                    .with_for_update())).scalars())
    if not orders:
        return 0
    ids = [o.id for o in orders]
    reason = "order.cancel_reason.shop_closing"
    await db.execute(update(Order).where(Order.id.in_(ids)).values(status="cancelled", cancel_reason=reason,
                                                                   cancelled_by_id=actor_id, updated_at=func.now()))
    db.add_all([OrderEvent(order_id=i, status="cancelled", actor_id=actor_id, note=reason) for i in ids])
    await _restore_items(db, orders, actor_id)
    await notify_many(db, [o.buyer_id for o in orders], "order_cancelled_farm", title_key="notif.order_farm_cancel.title",
                      body_key="notif.order_farm_cancel.body", params={"farm": farm_name, "reason_key": reason},
                      link="/orders", severity="warning")
    return len(orders)


async def finish_closing_shop(db: AsyncSession, shop_id: uuid.UUID) -> bool:
    """A 'closing' shop closes by itself once its last open order is done.

    Runs at the end of order changes that already hold the order's and the products' row locks. Stress test: locking
    the shop row here (FOR UPDATE) deadlocked with checkouts — inserting an order holds a key-share lock on its shop
    while the checkout waits for the same product rows. So: the status is read without a lock (almost always 'open':
    nothing to do), and a closing shop is locked FOR NO KEY UPDATE (compatible with checkouts' key-share) SKIP LOCKED —
    this never waits; if another transaction holds the row, the 5-minute safety net (close_finished_shops) closes it."""
    status = (await db.execute(select(Shop.status).where(Shop.id == shop_id))).scalar_one_or_none()
    if status != "closing":
        return False
    shop = (await db.execute(select(Shop).where(Shop.id == shop_id, Shop.status == "closing")
                             .with_for_update(key_share=True, skip_locked=True)
                             .execution_options(populate_existing=True))).scalar_one_or_none()
    if not shop:
        return False
    if (await db.execute(select(Order.id).where(Order.shop_id == shop_id, Order.status.in_(OPEN)).limit(1))).first():
        return False
    shop.status = "closed"
    farm = await db.get(Farm, shop.farm_id)
    await notify_many(db, await farm_leader_ids(db, shop.farm_id), "shop_closed", title_key="notif.shop_closed.title",
                      body_key="notif.shop_closed.body", params={"shop": shop.name, "farm": farm.name if farm else ""},
                      link=f"/farms/{shop.farm_id}?tab=shop", severity="success")
    return True


async def close_finished_shops(db: AsyncSession) -> int:
    """Safety net (scheduler): closing shops whose last open order finished while their row was busy."""
    ids = (await db.execute(select(Shop.id).where(Shop.status == "closing", ~select(Order.id).where(
        Order.shop_id == Shop.id, Order.status.in_(OPEN)).exists()))).scalars().all()
    return sum([await finish_closing_shop(db, sid) for sid in ids])


async def open_order_count(db: AsyncSession, farm_id: uuid.UUID, branch_id: uuid.UUID | None = None) -> int:
    stmt = select(func.count()).select_from(Order).where(Order.farm_id == farm_id, Order.status.in_(OPEN))
    if branch_id:
        stmt = stmt.where(Order.branch_id == branch_id)
    return int((await db.execute(stmt)).scalar_one())


# ------------------------------------------------------------------ marketplace queries (no N+1)
def _ratings_subq():
    return (select(Review.product_id, func.avg(Review.rating).label("avg"), func.count(Review.id).label("n"))
            .group_by(Review.product_id).subquery())


async def search_products(db: AsyncSession, *, q: str | None = None, category: str | None = None, city: str | None = None,
                          lat: float | None = None, lon: float | None = None, radius_km: float | None = None,
                          sort: str = "relevance", shop_id: uuid.UUID | None = None, page: int = 1, per_page: int = 24) -> dict[str, Any]:
    r = _ratings_subq()
    halted_farms, halted_branches = await halted_scope(db)
    stmt = (select(Product, Shop.name.label("shop_name"), Farm.name.label("farm_name"), Farm.farming_type,
                   Branch.city, Branch.state, Branch.lat, Branch.lon, r.c.avg, r.c.n)
            .join(Shop, Shop.id == Product.shop_id).join(Farm, Farm.id == Product.farm_id).join(Branch, Branch.id == Product.branch_id)
            .outerjoin(r, r.c.product_id == Product.id)
            .where(Product.status == "active", Shop.status == "open", Farm.status == "active", Branch.status == "active"))
    if halted_farms:
        stmt = stmt.where(Product.farm_id.notin_(halted_farms))
    if halted_branches:
        stmt = stmt.where(Product.branch_id.notin_(halted_branches))
    if shop_id:
        stmt = stmt.where(Product.shop_id == shop_id)
    if category:
        stmt = stmt.where(Product.category == category)
    if city:
        stmt = stmt.where(Branch.city.ilike(f"%{city.strip()[:60]}%"))
    if lat is not None and lon is not None and radius_km:
        a, b, c, d = bbox_around(lat, lon, radius_km)
        stmt = stmt.where(Branch.lat.between(a, b), Branch.lon.between(c, d))
    if q:
        qv = await embed_one_safe(q)
        like = f"%{q.strip()[:80]}%"
        keyword = or_(Product.name.ilike(like), Product.description.ilike(like), Product.category.ilike(like))
        if qv is not None:
            dist = Product.embedding.cosine_distance(qv)
            stmt = stmt.where(or_(keyword, and_(Product.embedding.isnot(None), dist < 0.55)))
            if sort == "relevance":
                stmt = stmt.order_by(Product.name.ilike(like).desc(), dist.nulls_last())
        else:                                   # embeddings unavailable: keyword search still works
            stmt = stmt.where(keyword)
            if sort == "relevance":
                stmt = stmt.order_by(Product.name.ilike(like).desc())
    if sort == "price_asc":
        stmt = stmt.order_by(Product.price.asc())
    elif sort == "price_desc":
        stmt = stmt.order_by(Product.price.desc())
    elif sort == "rating":
        stmt = stmt.order_by(r.c.avg.desc().nulls_last())
    elif sort == "newest" or (sort == "relevance" and not q):
        stmt = stmt.order_by(Product.created_at.desc())
    rows = (await db.execute(stmt.limit(500 if sort == "distance" else per_page * page + 1))).all()
    out = []
    for p, shop_name, farm_name, ftype, bcity, bstate, blat, blon, avg, n in rows:
        dist_km = haversine_km(lat, lon, blat, blon) if lat is not None and lon is not None else None
        if radius_km and dist_km is not None and dist_km > radius_km:
            continue
        out.append({"product": p, "shop_name": shop_name, "farm_name": farm_name, "farming_type": ftype, "city": bcity,
                    "state": bstate, "rating": round(float(avg), 1) if avg else None, "reviews": int(n or 0),
                    "distance_km": round(dist_km, 1) if dist_km is not None else None})
    if sort == "distance" and lat is not None:
        out.sort(key=lambda x: x["distance_km"] if x["distance_km"] is not None else 1e9)
    start = (page - 1) * per_page
    return {"items": out[start : start + per_page], "has_more": len(out) > start + per_page}


async def product_reviews(db: AsyncSession, product_id: uuid.UUID, limit: int = 50) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Review, User.status, User.deleted_at).join(User, User.id == Review.user_id)
                             .where(Review.product_id == product_id).order_by(Review.created_at.desc()).limit(limit))).all()
    return [{"review": r, "author_banned": st == "banned", "author_deleted": st == "deleted" or deleted is not None} for r, st, deleted in rows]


async def product_questions(db: AsyncSession, product_id: uuid.UUID, limit: int = 30) -> list[ProductQuestion]:
    return list((await db.execute(select(ProductQuestion).where(
        ProductQuestion.product_id == product_id, ProductQuestion.status.in_(("answered", "ai_answered")))
        .order_by(ProductQuestion.created_at.desc()).limit(limit))).scalars())


async def ask_about_product(db: AsyncSession, user: User | None, product: Product, question: str, lang: str) -> dict[str, Any]:
    from app.i18n import lang_meta
    from app.services.ai.providers import AIUnavailable, router
    from app.services.ai.queue import PRIORITY_INTERACTIVE, ai_queue

    farm = await db.get(Farm, product.farm_id)
    shop = await db.get(Shop, product.shop_id)
    branch = await db.get(Branch, product.branch_id)
    qv = await embed_one_safe(question)
    similar = (await db.execute(select(ProductQuestion).where(
        ProductQuestion.product_id == product.id, ProductQuestion.status == "answered", ProductQuestion.embedding.isnot(None))
        .order_by(ProductQuestion.embedding.cosine_distance(qv)).limit(4))).scalars().all() if qv is not None else []
    crops = (await db.execute(select(BranchCrop.name).where(BranchCrop.branch_id == product.branch_id,
                                                            BranchCrop.status == "growing"))).scalars().all()
    facts = (f"Product: {product.name}; category {product.category}; price {product.price} per {product.unit}; in stock "
             f"{product.stock} {product.unit}; minimum order {product.min_order_qty}. Description: {(product.description or '')[:1200]}\n"
             f"Farm: {farm.name}; farming type {farm.farming_type}; crops grown: {', '.join(crops) or 'n/a'}; location "
             f"{branch.city or ''}, {branch.state or ''}. Delivery: {'yes' if shop.accepts_delivery else 'no'} (buyer pays courier "
             f"₹{shop.courier_base_fee} + ₹{shop.courier_per_km}/km; cash on delivery only); pickup: {'yes' if shop.accepts_pickup else 'no'}.\n"
             + "Owner's earlier answers:\n" + "\n".join(f"Q: {s.question[:200]}\nA: {(s.owner_answer or '')[:300]}" for s in similar))
    from app.services.guide import detect_language, language_name

    system = (f"You answer a buyer's question about ONE farm product using ONLY the facts given. "
              f"Reply in {language_name(detect_language(question, lang))}. "
              "If the facts do not contain the answer, say you are not sure and set known=false. Never invent certifications, "
              'discounts or delivery dates. Return ONLY JSON {"answer": string, "known": boolean, "confidence": number}')
    answer, known, conf, provider = None, False, 0.0, None
    try:
        res = await ai_queue.run(lambda: router.chat([{"role": "user", "content": f"FACTS:\n{facts}\n\nQUESTION: {question}"}],
                                                     system=system, max_tokens=300, temperature=0.1),
                                 priority=PRIORITY_INTERACTIVE, timeout=90, user_key=f"pqa:{user.id if user else 'anon'}")
        data = parse_json_loose(res.text) or {}
        answer = str(data.get("answer") or "").strip()[:1500] or None
        known = bool(data.get("known")) and bool(answer)
        conf = float(data.get("confidence") or 0)
        provider = res.provider
    except (AIUnavailable, Exception):
        pass
    confident = known and conf >= settings.AI_LOW_CONFIDENCE
    pq = ProductQuestion(product_id=product.id, asker_id=user.id if user else None, question=question[:1000], ai_answer=answer,
                         ai_confidence=conf, status="ai_answered" if confident else "pending_owner", embedding=qv)
    db.add(pq)
    await db.flush()
    if not confident:
        leaders = await farm_leader_ids(db, product.farm_id, ("co_owner", "manager"))
        await notify_many(db, leaders, "question_pending", title_key="notif.question_pending.title",
                          body_key="notif.question_pending.body", params={"product": product.name, "question": question[:200]},
                          link=f"/farms/{product.farm_id}?tab=shop#q-{pq.id}", severity="warning", channels=("email",))
    return {"id": str(pq.id), "answer": answer if confident else None, "ai_guess": answer if not confident else None,
            "confidence": round(conf, 2), "forwarded_to_owner": not confident, "provider": provider}


async def answer_question(db: AsyncSession, user: User, pq: ProductQuestion, text: str) -> None:
    pq.owner_answer, pq.answered_by_id, pq.answered_at, pq.status = text.strip()[:2000], user.id, now_utc(), "answered"
    pq.embedding = await embed_one_safe(f"{pq.question}\n{pq.owner_answer}")
    if pq.asker_id:
        product = await db.get(Product, pq.product_id)
        await notify(db, pq.asker_id, "question_answered", title_key="notif.question_answered.title",
                     body_key="notif.question_answered.body", params={"product": product.name if product else ""},
                     link=f"/shop/p/{pq.product_id}#qa", severity="success")


async def seller_orders(db: AsyncSession, farm_id: uuid.UUID, statuses: tuple[str, ...] | None = None, limit: int = 100) -> list[dict[str, Any]]:
    stmt = select(Order).where(Order.farm_id == farm_id).order_by(Order.created_at.desc()).limit(limit)
    if statuses:
        stmt = stmt.where(Order.status.in_(statuses))
    orders = list((await db.execute(stmt)).scalars())
    items = defaultdict(list)
    if orders:
        for it in (await db.execute(select(OrderItem).where(OrderItem.order_id.in_([o.id for o in orders])))).scalars():
            items[it.order_id].append(it)
    return [{"order": o, "items": items[o.id]} for o in orders]


async def buyer_orders(db: AsyncSession, buyer_id: uuid.UUID, limit: int = 100) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Order, Shop.name).join(Shop, Shop.id == Order.shop_id).where(Order.buyer_id == buyer_id)
                             .order_by(Order.created_at.desc()).limit(limit))).all()
    ids = [o.id for o, _ in rows]
    items, events, reviewed = defaultdict(list), defaultdict(list), set()
    if ids:
        for it in (await db.execute(select(OrderItem).where(OrderItem.order_id.in_(ids)))).scalars():
            items[it.order_id].append(it)
        for ev in (await db.execute(select(OrderEvent).where(OrderEvent.order_id.in_(ids)).order_by(OrderEvent.created_at))).scalars():
            events[ev.order_id].append(ev)
        reviewed = {(oid, pid) for oid, pid in (await db.execute(select(Review.order_id, Review.product_id).where(Review.order_id.in_(ids)))).all()}
    return [{"order": o, "shop_name": sn, "items": items[o.id], "events": events[o.id],
             "reviewed": {pid for (oid, pid) in reviewed if oid == o.id}} for o, sn in rows]


async def get_product_bundle(db: AsyncSession, product_id: uuid.UUID) -> tuple[Product, Shop, Farm, Branch]:
    row = (await db.execute(select(Product, Shop, Farm, Branch).join(Shop, Shop.id == Product.shop_id)
                            .join(Farm, Farm.id == Product.farm_id).join(Branch, Branch.id == Product.branch_id)
                            .where(Product.id == product_id))).first()
    if not row:
        raise NotFound("errors.product_not_found")
    return row[0], row[1], row[2], row[3]
