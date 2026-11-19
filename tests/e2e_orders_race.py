"""Order placement under collisions — directly against the services and the real database (no HTTP).

    python tests/e2e_orders_race.py        (needs the load-test seed: python tests/load/seed_loadtest.py)

* The 3-hour soak found two checkouts drawing the same order number (HTTP 500). Here the number generator is forced to
  repeat an existing number: the checkout must draw again and succeed, with stock taken exactly once.
* Two concurrent checkouts with the same idempotency key (two devices / double tap) must yield ONE order, no error.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from e2e_flow import finish, ok  # noqa: E402

SEED = Path(__file__).resolve().parent / "load" / ".seed.json"


async def run() -> None:
    from sqlalchemy import func, select

    from app.database import SessionLocal, engine
    from app.models import Branch, Farm, InventoryMovement, Order, Product, Shop, User
    from app.services import shop as shop_svc
    from app.services.redis_client import init_redis

    await init_redis()
    seed = json.loads(SEED.read_text("utf-8"))
    o = seed["owners"][-1]
    buyer_id = uuid.UUID(seed["workers"][-1]["user_id"])
    pid = uuid.UUID(next(p["id"] for p in o["products"] if not p["scarce"]))

    async def load(db):
        buyer = await db.get(User, buyer_id)
        shop = await db.get(Shop, uuid.UUID(o["shop_id"]))
        branch = await db.get(Branch, uuid.UUID(o["branch_id"]))
        farm = await db.get(Farm, uuid.UUID(o["farm_id"]))
        prod = await db.get(Product, pid)
        return buyer, shop, branch, farm, prod

    print("1. a colliding order number is drawn again (no HTTP 500)")
    async with SessionLocal() as db:
        taken = (await db.execute(select(Order.order_no).order_by(Order.created_at.desc()).limit(1))).scalar_one()
    real = shop_svc.new_order_no
    draws = iter([taken, taken])
    shop_svc.new_order_no = lambda: next(draws, None) or real()
    try:
        async with SessionLocal() as db:
            buyer, shop, branch, farm, prod = await load(db)
            before = Decimal(prod.stock)
            order = await shop_svc.place_order(db, buyer, shop=shop, branch=branch, farm=farm, lines=[(prod, Decimal("1"))],
                                               fulfilment="pickup", address=None, lat=None, lon=None,
                                               idem=f"race-{uuid.uuid4().hex}", client_ts=None, buyer_farm_id=None)
            await db.commit()
            no, oid = order.order_no, order.id
        ok(no != taken and no.startswith("KS"), "the checkout drew a fresh number after two collisions", (taken, no))
        async with SessionLocal() as db:
            prod = await db.get(Product, pid)
            moves = (await db.execute(select(func.count()).select_from(InventoryMovement).where(InventoryMovement.order_id == oid))).scalar_one()
        ok(Decimal(prod.stock) == before - 1 and moves == 1, "stock taken exactly once", (before, prod.stock, moves))
    finally:
        shop_svc.new_order_no = real

    print("2. the same checkout twice at the same moment → one order, no error")
    idem = f"twin-{uuid.uuid4().hex}"

    async def checkout():
        async with SessionLocal() as db:
            buyer, shop, branch, farm, prod = await load(db)
            # a different quantity than step 1: the "same order again within a minute" guard is a separate protection
            order = await shop_svc.place_order(db, buyer, shop=shop, branch=branch, farm=farm, lines=[(prod, Decimal("2"))],
                                               fulfilment="pickup", address=None, lat=None, lon=None, idem=idem, client_ts=None,
                                               buyer_farm_id=None)
            await db.commit()
            return order.id

    res = await asyncio.gather(checkout(), checkout(), return_exceptions=True)
    errors = [r for r in res if isinstance(r, Exception)]
    async with SessionLocal() as db:
        n = (await db.execute(select(func.count()).select_from(Order).where(Order.idempotency_key == idem))).scalar_one()
    ok(not errors and n == 1 and res[0] == res[1], "one order for both taps, both got it back", (res, n))

    print("3. checkouts and cancellations racing on one shop's products → no deadlock (the security stress test found one)")
    errs = await checkout_cancel_race(o, [uuid.UUID(w["user_id"]) for w in seed["workers"][-22:-2]], shop_svc)
    ok(not errs, "10 cancellations + 10 checkouts at the same moment on the same products: all succeed", errs[:3])
    await engine.dispose()


async def checkout_cancel_race(o: dict, buyers: list[uuid.UUID], shop_svc) -> list[str]:
    """A checkout's order insert holds a key-share lock on its shop while it waits for product rows; a cancellation holds
    product rows (stock restored) and then looked at the shop row FOR UPDATE — a lock cycle under load."""
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Branch, Farm, Order, Product, Shop, User

    prods = [uuid.UUID(p["id"]) for p in o["products"] if not p["scarce"]][:2]
    half = len(buyers) // 2

    async def place(buyer_id: uuid.UUID, qty: int, ids: list[uuid.UUID]) -> uuid.UUID:
        async with SessionLocal() as db:
            buyer = await db.get(User, buyer_id)
            shop, branch = await db.get(Shop, uuid.UUID(o["shop_id"])), await db.get(Branch, uuid.UUID(o["branch_id"]))
            farm = await db.get(Farm, uuid.UUID(o["farm_id"]))
            lines = [(await db.get(Product, i), Decimal(qty)) for i in ids]
            order = await shop_svc.place_order(db, buyer, shop=shop, branch=branch, farm=farm, lines=lines, fulfilment="pickup",
                                               address=None, lat=None, lon=None, idem=f"dl-{uuid.uuid4().hex}", client_ts=None,
                                               buyer_farm_id=None)
            await db.commit()
            return order.id

    async def cancel(order_id: uuid.UUID, buyer_id: uuid.UUID) -> None:
        async with SessionLocal() as db:
            order = (await db.execute(select(Order).where(Order.id == order_id).with_for_update())).scalar_one()
            await shop_svc.transition_order(db, order, "cancelled", await db.get(User, buyer_id), as_seller=False, note="race")
            await db.commit()

    placed = [await place(b, 1 + i % 3, prods) for i, b in enumerate(buyers[:half])]          # the orders to cancel
    res = await asyncio.gather(*[cancel(oid, buyers[i]) for i, oid in enumerate(placed)],
                               *[place(b, 1, prods[::-1]) for b in buyers[half:]], return_exceptions=True)
    return [repr(r)[:200] for r in res if isinstance(r, Exception)]


def main() -> int:
    t0 = time.time()
    if not SEED.exists():
        print("(skipped — run tests/load/seed_loadtest.py first)")
        return 0
    asyncio.run(run())
    return finish(t0)


if __name__ == "__main__":
    sys.exit(main())
