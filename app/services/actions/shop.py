"""Shop actions: shop/product management, timestamped inventory, orders (COD / pickup), reviews, Q&A answers."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from app.errors import AppError, Conflict, Forbidden, NotFound
from app.models import Branch, Farm, Order, OrderItem, Product, ProductQuestion, Review, Shop
from app.models.shop import PRODUCT_CATEGORIES, UNITS
from app.services.actions.base import Action, Ctx, Payload, Prepared, register
from app.services.actions.fields import Lat, LongText, Lon, Money, Name, PositiveMoney, Qty, ShortText
from app.services.ai.embeddings import embed_one, embed_one_safe
from app.services.ai.moderation import abuse_monitor
from app.services.geo import haversine_km
from app.services.halts import is_halted
from app.services.perms import require_perm, roles_by_farm
from app.services.shop import (adjust_stock, answer_question, cancel_shop_orders, courier_charge, place_order, shop_open_orders,
                               transition_order)
from app.services.uploads import owned_upload
from app.utils import format_inr


@register
class ShopCreate(Action):
    type = "shop.create"

    class Payload(Payload):
        farm_id: uuid.UUID
        name: Name
        description: LongText | None = None
        courier_base_fee: Money = Decimal("40")
        courier_per_km: Money = Decimal("5")
        delivery_radius_km: int = Field(default=50, ge=1, le=1000)
        accepts_delivery: bool = True
        accepts_pickup: bool = True

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        farm, _ = await require_perm(ctx.db, p.farm_id, ctx.user, "shop")
        if farm.status != "active":
            raise AppError("errors.farm_not_active", status=409)
        if (await ctx.db.execute(select(Shop.id).where(Shop.farm_id == farm.id))).first():
            raise Conflict("errors.shop_exists")
        if not (p.accepts_delivery or p.accepts_pickup):
            raise AppError("errors.shop_fulfilment")
        return Prepared([ctx.t("sum.shop", name=p.name, farm=farm.name),
                         ctx.t("sum.courier", base=format_inr(p.courier_base_fee), per_km=format_inr(p.courier_per_km)),
                         ctx.t("sum.cod_only")], farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        s = Shop(**p.model_dump())
        ctx.db.add(s)
        await ctx.db.flush()
        return {"message": ctx.t("msg.shop_created"), "shop_id": str(s.id)}


@register
class ShopUpdate(Action):
    type = "shop.update"

    class Payload(Payload):
        shop_id: uuid.UUID
        name: Name | None = None
        description: LongText | None = None
        courier_base_fee: Money | None = None
        courier_per_km: Money | None = None
        delivery_radius_km: int | None = Field(default=None, ge=1, le=1000)
        accepts_delivery: bool | None = None
        accepts_pickup: bool | None = None
        status: Literal["open"] | None = None       # reopen only — closing is a vote (shop.close)

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        s = await ctx.db.get(Shop, p.shop_id)
        if not s:
            raise NotFound()
        farm, _ = await require_perm(ctx.db, s.farm_id, ctx.user, "shop")
        if p.status == "open" and s.status != "open":
            await require_perm(ctx.db, s.farm_id, ctx.user, "settings")     # reopening: the owner / a co-owner
        changes = {k: v for k, v in p.model_dump(exclude_unset=True).items() if k != "shop_id" and getattr(s, k) != v}
        if not changes:
            raise AppError("errors.no_changes")
        return Prepared([ctx.t("sum.setting_change", field=ctx.t(f"field.{k}"), old=getattr(s, k), new=v) for k, v in changes.items()],
                        farm_id=farm.id, data={"changes": {k: str(v) if isinstance(v, Decimal) else v for k, v in changes.items()}})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        s = await ctx.db.get(Shop, p.shop_id)
        for k, v in p.model_dump(exclude_unset=True).items():
            if k != "shop_id":
                setattr(s, k, v)
        return {"message": ctx.t("msg.saved")}


@register
class ShopClose(Action):
    """Close a farm shop — a decision of the owner and the co-owners (they vote; with no co-owner the owner decides).
    One click also cancels every open order not yet with a courier (buyers told, stock restored). Orders already with the
    courier — the buyer is paying for that trip — are never cancelled: until they are delivered the shop is 'closing'
    (no new orders, hidden from search), then it closes by itself."""
    type = "shop.close"
    consensus = True

    class Payload(Payload):
        shop_id: uuid.UUID
        cancel_open_orders: bool = False

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        s = await ctx.db.get(Shop, p.shop_id)
        if not s:
            raise NotFound()
        farm, _ = await require_perm(ctx.db, s.farm_id, ctx.user, "settings")
        if s.status != "open":
            raise Conflict("errors.shop_not_open")
        cancel, deliver = await shop_open_orders(ctx.db, s.id)
        if cancel and not p.cancel_open_orders:
            raise AppError("errors.shop_open_orders", n=cancel)
        lines = [ctx.t("sum.shop_close", shop=s.name)]
        if cancel:
            lines.append(ctx.t("sum.shop_close_cancel", n=cancel))
        lines.append(ctx.t("sum.shop_close_wait", n=deliver) if deliver else ctx.t("sum.shop_close_now"))
        return Prepared(lines, warnings=[ctx.t("warn.shop_close_buyers")] if cancel else [], farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        s = (await ctx.db.execute(select(Shop).where(Shop.id == p.shop_id).with_for_update())).scalar_one()
        if s.status != "open":
            raise Conflict("errors.shop_not_open")
        farm = await ctx.db.get(Farm, s.farm_id)
        cancelled = await cancel_shop_orders(ctx.db, s, ctx.user.id, farm.name) if p.cancel_open_orders else 0
        cancel, deliver = await shop_open_orders(ctx.db, s.id)     # orders may have come in while the owners voted
        if cancel:
            raise AppError("errors.shop_open_orders", n=cancel)
        s.status = "closing" if deliver else "closed"
        return {"message": ctx.t("msg.shop_closing", n=deliver, cancelled=cancelled) if deliver
                else ctx.t("msg.shop_closed", cancelled=cancelled)}


async def _product_embedding(name: str, description: str | None, category: str) -> list[float] | None:
    return await embed_one_safe(f"{name}. {category}. {description or ''}"[:2000])


@register
class ProductCreate(Action):
    type = "product.create"

    class Payload(Payload):
        shop_id: uuid.UUID
        branch_id: uuid.UUID
        name: Name
        description: LongText | None = None
        category: Literal[PRODUCT_CATEGORIES]  # type: ignore[valid-type]
        unit: Literal[UNITS]  # type: ignore[valid-type]
        price: PositiveMoney
        stock: Money = Decimal("0")
        min_order_qty: Qty = Decimal("1")
        image_upload_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5)

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        s = await ctx.db.get(Shop, p.shop_id)
        if not s:
            raise NotFound()
        farm, _ = await require_perm(ctx.db, s.farm_id, ctx.user, "shop")
        b = await ctx.db.get(Branch, p.branch_id)
        if not b or b.farm_id != farm.id or b.status == "closed":
            raise NotFound("errors.branch_not_found")
        for uid in p.image_upload_ids:
            await owned_upload(ctx.db, ctx.user, uid, "product", unused=True)
        return Prepared([ctx.t("sum.product", name=p.name, price=format_inr(p.price), unit=ctx.t(f"unit.{p.unit}"),
                               stock=p.stock, branch=b.name)], farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        s = await ctx.db.get(Shop, p.shop_id)
        pid = uuid.uuid4()
        prod = Product(id=pid, shop_id=s.id, farm_id=s.farm_id, branch_id=p.branch_id, name=p.name, description=p.description,
                       category=p.category, unit=p.unit, price=p.price, stock=Decimal("0"), min_order_qty=p.min_order_qty,
                       images=[str(u) for u in p.image_upload_ids], embedding=await _product_embedding(p.name, p.description, p.category))
        ctx.db.add(prod)
        await ctx.db.flush()
        for uid in p.image_upload_ids:
            up = await owned_upload(ctx.db, ctx.user, uid, "product")
            up.used_for = f"product:{pid}"
        if p.stock > 0:
            await adjust_stock(ctx.db, pid, p.stock, "initial", idem=f"init:{pid}", actor_id=ctx.user.id)
        return {"message": ctx.t("msg.product_created"), "product_id": str(pid)}


@register
class ProductUpdate(Action):
    type = "product.update"

    class Payload(Payload):
        product_id: uuid.UUID
        expected_version: int = Field(ge=1)
        name: Name | None = None
        description: LongText | None = None
        category: Literal[PRODUCT_CATEGORIES] | None = None  # type: ignore[valid-type]
        unit: Literal[UNITS] | None = None  # type: ignore[valid-type]
        price: PositiveMoney | None = None
        min_order_qty: Qty | None = None
        status: Literal["active", "inactive"] | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        prod = await ctx.db.get(Product, p.product_id)
        if not prod:
            raise NotFound()
        farm, _ = await require_perm(ctx.db, prod.farm_id, ctx.user, "shop")
        if prod.version != p.expected_version:
            raise Conflict("errors.stale_version")
        changes = {k: v for k, v in p.model_dump(exclude_unset=True).items()
                   if k not in ("product_id", "expected_version") and getattr(prod, k) != v}
        if not changes:
            raise AppError("errors.no_changes")
        return Prepared([ctx.t("sum.setting_change", field=ctx.t(f"field.{k}"), old=getattr(prod, k), new=v) for k, v in changes.items()],
                        farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        prod = (await ctx.db.execute(select(Product).where(Product.id == p.product_id, Product.version == p.expected_version)
                                     .with_for_update())).scalar_one_or_none()
        if not prod:
            raise Conflict("errors.stale_version")
        for k, v in p.model_dump(exclude_unset=True).items():
            if k not in ("product_id", "expected_version"):
                setattr(prod, k, v)
        prod.version += 1
        prod.embedding = await _product_embedding(prod.name, prod.description, prod.category)
        return {"message": ctx.t("msg.saved"), "version": prod.version}


@register
class InventoryAdjust(Action):
    type = "inventory.adjust"

    class Payload(Payload):
        product_id: uuid.UUID
        delta: Decimal = Field(ge=Decimal("-1000000"), le=Decimal("1000000"), max_digits=12, decimal_places=2)
        reason: Literal["restock", "correction", "damaged", "sold_offline", "harvest"]
        expected_version: int = Field(ge=1)
        client_ts: datetime | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        prod = await ctx.db.get(Product, p.product_id)
        if not prod:
            raise NotFound()
        farm, _ = await require_perm(ctx.db, prod.farm_id, ctx.user, "shop")
        if p.delta == 0:
            raise AppError("errors.no_changes")
        if prod.version != p.expected_version:
            raise Conflict("errors.stale_version")
        new = Decimal(prod.stock) + p.delta
        if new < 0:
            raise AppError("errors.out_of_stock")
        return Prepared([ctx.t("sum.inventory", name=prod.name, old=prod.stock, new=new, unit=ctx.t(f"unit.{prod.unit}"),
                               reason=ctx.t(f"inv.{p.reason}"))], farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        stock = await adjust_stock(ctx.db, p.product_id, p.delta, p.reason, idem=f"adj:{ctx.action_id}", actor_id=ctx.user.id,
                                   expected_version=p.expected_version, client_ts=p.client_ts)
        return {"message": ctx.t("msg.stock_updated", stock=stock)}


class LineIn(Payload):
    product_id: uuid.UUID
    qty: Qty


@register
class OrderPlace(Action):
    type = "order.place"

    class Payload(Payload):
        shop_id: uuid.UUID
        items: list[LineIn] = Field(min_length=1, max_length=30)
        fulfilment: Literal["delivery", "pickup"]
        address: ShortText | None = None
        lat: Lat | None = None
        lon: Lon | None = None
        idempotency_key: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9\-_]+$")
        client_ts: datetime | None = None
        buyer_farm_id: uuid.UUID | None = None

    async def _load(self, ctx: Ctx, p: Payload):
        shop = await ctx.db.get(Shop, p.shop_id)
        if not shop or shop.status != "open":
            raise NotFound("errors.shop_closed")
        farm = await ctx.db.get(Farm, shop.farm_id)
        if farm.status != "active":
            raise AppError("errors.shop_closed", status=409)
        ids = [i.product_id for i in p.items]
        if len(set(ids)) != len(ids):
            raise AppError("errors.duplicate_items")
        prods = {x.id: x for x in (await ctx.db.execute(select(Product).where(Product.id.in_(ids)))).scalars()}
        if len(prods) != len(ids) or any(x.shop_id != shop.id or x.status != "active" for x in prods.values()):
            raise NotFound("errors.product_not_found")
        branch_ids = {x.branch_id for x in prods.values()}
        if len(branch_ids) != 1:
            raise AppError("errors.order_one_branch")
        branch = await ctx.db.get(Branch, branch_ids.pop())
        if branch.status != "active" or await is_halted(ctx.db, farm.id, branch.id):
            raise AppError("errors.branch_halted", status=409)
        return shop, farm, branch, prods

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        shop, farm, branch, prods = await self._load(ctx, p)
        if p.buyer_farm_id:
            roles = await roles_by_farm(ctx.db, ctx.user)
            if roles.get(p.buyer_farm_id) not in ("owner", "co_owner", "manager", "accountant"):
                raise Forbidden("errors.no_farm_permission")
        lines, subtotal = [], Decimal("0")
        for it in p.items:
            prod = prods[it.product_id]
            if it.qty < Decimal(prod.min_order_qty):
                raise AppError("errors.min_order_qty", name=prod.name, qty=prod.min_order_qty)
            if it.qty > Decimal(prod.stock):
                raise AppError("errors.out_of_stock_item", status=409, name=prod.name, stock=prod.stock)
            line = (Decimal(prod.price) * it.qty).quantize(Decimal("0.01"))
            subtotal += line
            lines.append(ctx.t("sum.order_line", name=prod.name, qty=it.qty, unit=ctx.t(f"unit.{prod.unit}"),
                               price=format_inr(prod.price), total=format_inr(line)))
        charge, lat, lon = Decimal("0"), None, None
        if p.fulfilment == "delivery":
            if not shop.accepts_delivery:
                raise AppError("errors.no_delivery")
            lat, lon = (p.lat, p.lon) if p.lat is not None and p.lon is not None else (ctx.user.lat, ctx.user.lon)
            if lat is None or lon is None or not p.address:     # no shared position: the saved (GPS or typed) location
                raise AppError("errors.delivery_location_required")
            dist = haversine_km(lat, lon, branch.lat, branch.lon)
            if dist > shop.delivery_radius_km:
                raise AppError("errors.delivery_too_far", km=round(dist, 1), max=shop.delivery_radius_km)
            charge = courier_charge(shop, dist)
            lines.append(ctx.t("sum.courier_charge", amount=format_inr(charge), km=round(dist, 1)))
            if p.lat is None or p.lon is None:
                lines.append(ctx.t("sum.delivery_from_saved"))
        else:
            if not shop.accepts_pickup:
                raise AppError("errors.no_pickup")
            lines.append(ctx.t("sum.pickup_at", branch=branch.name, place=branch.city or branch.address or ""))
        lines.append(ctx.t("sum.order_total", total=format_inr(subtotal + charge)))
        lines.append(ctx.t("sum.cod_only") if p.fulfilment == "delivery" else ctx.t("sum.pay_at_pickup"))
        return Prepared(lines, warnings=[ctx.t("warn.courier_paid_by_buyer")] if charge else [], farm_id=None,
                        data={"lat": lat, "lon": lon})

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        shop, farm, branch, prods = await self._load(ctx, p)
        order = await place_order(ctx.db, ctx.user, shop=shop, branch=branch, farm=farm,
                                  lines=[(prods[i.product_id], i.qty) for i in p.items], fulfilment=p.fulfilment,
                                  address=p.address, lat=prep.data.get("lat"), lon=prep.data.get("lon"),
                                  idem=p.idempotency_key, client_ts=p.client_ts, buyer_farm_id=p.buyer_farm_id)
        return {"message": ctx.t("msg.order_placed", no=order.order_no), "order_no": order.order_no, "redirect": "/orders",
                "clear_cart_shop": str(shop.id)}


@register
class OrderUpdate(Action):
    type = "order.update"

    class Payload(Payload):
        order_id: uuid.UUID
        to: Literal["confirmed", "dispatched", "ready_for_pickup", "delivered", "picked_up", "cancelled"]
        note: ShortText | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        o = await ctx.db.get(Order, p.order_id)
        if not o:
            raise NotFound()
        farm, _ = await require_perm(ctx.db, o.farm_id, ctx.user, "orders")
        lines = [ctx.t("sum.order_status", no=o.order_no, old=ctx.t(f"order.status_name.{o.status}"), new=ctx.t(f"order.status_name.{p.to}"))]
        if p.to == "cancelled":
            lines.append(ctx.t("sum.order_cancel_restock"))
        if p.to in ("delivered", "picked_up"):
            lines.append(ctx.t("sum.order_cash_received", total=format_inr(o.total)))
        return Prepared(lines, farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        o = (await ctx.db.execute(select(Order).where(Order.id == p.order_id).with_for_update())).scalar_one()
        await transition_order(ctx.db, o, p.to, ctx.user, as_seller=True, note=p.note)
        return {"message": ctx.t("msg.saved")}


@register
class OrderCancel(Action):
    type = "order.cancel"

    class Payload(Payload):
        order_id: uuid.UUID
        note: ShortText | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        o = await ctx.db.get(Order, p.order_id)
        if not o or o.buyer_id != ctx.user.id:
            raise NotFound()
        if o.status not in ("placed", "confirmed"):
            raise Conflict("errors.order_cannot_cancel")
        return Prepared([ctx.t("sum.order_cancel", no=o.order_no)])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        o = (await ctx.db.execute(select(Order).where(Order.id == p.order_id).with_for_update())).scalar_one()
        await transition_order(ctx.db, o, "cancelled", ctx.user, as_seller=False, note=p.note)
        return {"message": ctx.t("msg.order_cancelled")}


@register
class ReviewCreate(Action):
    type = "review.create"

    class Payload(Payload):
        order_id: uuid.UUID
        product_id: uuid.UUID
        rating: int = Field(ge=1, le=5)
        text: LongText | None = None

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        o = await ctx.db.get(Order, p.order_id)
        if not o or o.buyer_id != ctx.user.id or o.status not in ("delivered", "picked_up"):
            raise Forbidden("errors.review_verified_only")
        has = (await ctx.db.execute(select(OrderItem.id).where(OrderItem.order_id == o.id, OrderItem.product_id == p.product_id))).first()
        if not has:
            raise Forbidden("errors.review_verified_only")
        if (await ctx.db.execute(select(Review.id).where(Review.order_id == o.id, Review.product_id == p.product_id,
                                                         Review.user_id == ctx.user.id))).first():
            raise Conflict("errors.already_reviewed")
        if p.text:
            from app.services.ai.moderation import ensure_can_post

            ensure_can_post(ctx.user)
            v = await abuse_monitor.check_text(p.text, user_id=ctx.user.id, ip=ctx.ip, channel="review")
            if not v.allow:
                raise AppError("errors.content_blocked")
        return Prepared([ctx.t("sum.review", stars="★" * p.rating + "☆" * (5 - p.rating))])

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        ctx.db.add(Review(product_id=p.product_id, order_id=p.order_id, user_id=ctx.user.id, author_name=ctx.user.full_name,
                          rating=p.rating, text=p.text))
        return {"message": ctx.t("msg.review_saved")}


@register
class QuestionAnswer(Action):
    type = "question.answer"

    class Payload(Payload):
        question_id: uuid.UUID
        answer: str = Field(min_length=2, max_length=2000)

    async def prepare(self, ctx: Ctx, p: Payload) -> Prepared:
        q = await ctx.db.get(ProductQuestion, p.question_id)
        if not q:
            raise NotFound()
        prod = await ctx.db.get(Product, q.product_id)
        farm, _ = await require_perm(ctx.db, prod.farm_id, ctx.user, "answer")
        return Prepared([ctx.t("sum.answer", product=prod.name), ctx.t("sum.answer_public")], farm_id=farm.id)

    async def execute(self, ctx: Ctx, p: Payload, prep: Prepared) -> dict:
        q = await ctx.db.get(ProductQuestion, p.question_id)
        await answer_question(ctx.db, ctx.user, q, p.answer)
        return {"message": ctx.t("msg.saved")}
