"""Points de vente : restaurant / café / bar, parking, salle de réunion ; catalogue et stock."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func, select

from app.deps import AdminUser, DbSession, StaffUser, attempt, redirect
from app.models.booking import Reservation
from app.models.pos import OUTLETS, PosItem, PosOrder, StockMovement
from app.services import pdf, pos, printing
from app.services.common import BusinessError, to_int
from app.services.loaders import load_pos_order, load_reservation, load_reservations
from app.services.settings import enabled_modules, load_hotel_settings, today_of
from app.templating import render

router = APIRouter(prefix="/pos")


async def _order(db, order_id: int) -> PosOrder:
    order = await load_pos_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Addition introuvable.")
    return order


@router.get("")
async def pos_home(request: Request, db: DbSession, user: StaffUser):
    hs = await load_hotel_settings(db)
    modules = await enabled_modules(db)
    today = today_of(hs)
    open_orders = (await db.scalars(select(PosOrder).where(PosOrder.status == "open")
                                    .order_by(PosOrder.created_at))).unique().all()
    closed_today = (await db.scalars(
        select(PosOrder).where(PosOrder.status != "open", PosOrder.closed_at >= datetime.combine(today, datetime.min.time()))
        .order_by(PosOrder.closed_at.desc()))).unique().all()
    low_stock = (await db.scalars(select(PosItem).where(
        PosItem.active.is_(True), PosItem.track_stock.is_(True), PosItem.stock_qty <= PosItem.low_stock_threshold))).all()
    return await render(request, db, "pos/index.html", open_orders=open_orders, closed_today=closed_today,
                        low_stock=low_stock, outlets={k: v for k, v in OUTLETS.items() if k in modules},
                        day_total=sum(o.total for o in closed_today if o.status in ("paid", "room_charge")))


@router.post("/orders")
async def order_create(request: Request, db: DbSession, user: StaffUser, outlet: Annotated[str, Form()] = "",
                       label: Annotated[str, Form()] = ""):
    hs = await load_hotel_settings(db)
    modules = await enabled_modules(db)
    holder = {}

    async def action():
        holder["order"] = await pos.create_order(db, outlet, label, hs, user, modules)

    if await attempt(request, db, action, "Nouvelle addition ouverte."):
        return redirect(f"/pos/orders/{holder['order'].id}")
    return redirect("/pos")


@router.get("/orders/{order_id}")
async def order_page(request: Request, db: DbSession, user: StaffUser, order_id: int):
    order = await _order(db, order_id)
    items = (await db.scalars(select(PosItem).where(PosItem.outlet == order.outlet, PosItem.active.is_(True))
                              .order_by(PosItem.category, PosItem.name))).all()
    in_house = await load_reservations(db, Reservation.status == "checked_in", order_by=Reservation.room_id)
    categories = sorted({i.category or "Divers" for i in items})
    return await render(request, db, "pos/order.html", order=order, items=items, categories=categories,
                        in_house=in_house)


@router.post("/orders/{order_id}/lines")
async def order_add_line(request: Request, db: DbSession, user: StaffUser, order_id: int,
                         item_id: Annotated[str, Form()] = "", quantity: Annotated[str, Form()] = "1"):
    order = await _order(db, order_id)

    async def action():
        item = await db.get(PosItem, to_int(item_id, "Article"))
        if item is None:
            raise BusinessError("Article introuvable.")
        await pos.add_line(db, order, item, to_int(quantity, "Quantité", minimum=1, maximum=999))

    await attempt(request, db, action, "Article ajouté.")
    return redirect(f"/pos/orders/{order_id}")


@router.post("/orders/{order_id}/lines/{line_id}/delete")
async def order_remove_line(request: Request, db: DbSession, user: StaffUser, order_id: int, line_id: int):
    order = await _order(db, order_id)
    await attempt(request, db, lambda: pos.remove_line(db, order, line_id), "Ligne supprimée.")
    return redirect(f"/pos/orders/{order_id}")


@router.post("/orders/{order_id}/pay")
async def order_pay(request: Request, db: DbSession, user: StaffUser, order_id: int,
                    method: Annotated[str, Form()] = "especes"):
    order = await _order(db, order_id)
    hs = await load_hotel_settings(db)
    await attempt(request, db, lambda: pos.settle_cash(db, order, method, hs, user), "Addition réglée.")
    return redirect(f"/pos/orders/{order_id}")


@router.post("/orders/{order_id}/charge")
async def order_room_charge(request: Request, db: DbSession, user: StaffUser, order_id: int,
                            reservation_id: Annotated[str, Form()] = ""):
    order = await _order(db, order_id)
    hs = await load_hotel_settings(db)

    async def action():
        res = await load_reservation(db, to_int(reservation_id, "Chambre"))
        if res is None:
            raise BusinessError("Séjour introuvable.")
        await pos.settle_room_charge(db, order, res, hs, user)

    await attempt(request, db, action, "Addition imputée sur la note de la chambre.")
    return redirect(f"/pos/orders/{order_id}")


@router.post("/orders/{order_id}/void")
async def order_void(request: Request, db: DbSession, user: StaffUser, order_id: int):
    order = await _order(db, order_id)
    hs = await load_hotel_settings(db)
    if await attempt(request, db, lambda: pos.void_order(db, order, hs, user), "Addition annulée."):
        return redirect("/pos")
    return redirect(f"/pos/orders/{order_id}")


@router.get("/orders/{order_id}/ticket.pdf")
async def order_ticket(db: DbSession, user: StaffUser, order_id: int):
    order = await _order(db, order_id)
    hs = await load_hotel_settings(db)
    return Response(pdf.pos_ticket_pdf(hs, order), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{order.number}.pdf"'})


@router.post("/orders/{order_id}/print")
async def order_print(request: Request, db: DbSession, user: StaffUser, order_id: int):
    order = await _order(db, order_id)
    hs = await load_hotel_settings(db)

    async def action():
        await printing.print_raw(hs.thermal_printer_host, hs.thermal_printer_port, printing.pos_receipt(order, hs))

    await attempt(request, db, action, "Ticket envoyé à l'imprimante.")
    return redirect(f"/pos/orders/{order_id}")


# ---------------------------------------------------------------- catalogue et stock
@router.get("/catalog")
async def catalog(request: Request, db: DbSession, user: StaffUser, outlet: str = ""):
    stmt = select(PosItem).order_by(PosItem.outlet, PosItem.category, PosItem.name)
    if outlet in OUTLETS:
        stmt = stmt.where(PosItem.outlet == outlet)
    items = (await db.scalars(stmt)).all()
    return await render(request, db, "pos/catalog.html", items=items, outlet=outlet)


def _item_values(form, hs) -> dict:
    get = lambda key: str(form.get(key) or "").strip()  # noqa: E731
    outlet = get("outlet")
    if outlet not in OUTLETS:
        raise BusinessError("Point de vente invalide.")
    if not get("name"):
        raise BusinessError("Libellé requis.")
    default_vat = hs.vat_rate_fnb if outlet == "restaurant" else hs.vat_rate_services
    return {
        "outlet": outlet, "name": get("name")[:120], "category": get("category")[:60],
        "price": to_int(get("price"), "Prix", minimum=0),
        "vat_rate": to_int(get("vat_rate"), "TVA", default=default_vat, minimum=0, maximum=100),
        "track_stock": form.get("track_stock") == "1",
        "low_stock_threshold": to_int(get("low_stock_threshold"), "Seuil d'alerte", default=0, minimum=0),
        "active": form.get("active") == "1",  # case décochée = champ absent = inactif
    }


@router.post("/catalog")
async def catalog_create(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()
    hs = await load_hotel_settings(db)

    async def action():
        db.add(PosItem(**_item_values(form, hs), stock_qty=0))

    await attempt(request, db, action, "Article ajouté au catalogue.")
    return redirect("/pos/catalog")


@router.post("/catalog/{item_id}")
async def catalog_update(request: Request, db: DbSession, user: AdminUser, item_id: int):
    form = await request.form()
    hs = await load_hotel_settings(db)

    async def action():
        item = await db.get(PosItem, item_id)
        if item is None:
            raise BusinessError("Article introuvable.")
        for key, value in _item_values(form, hs).items():
            setattr(item, key, value)

    await attempt(request, db, action, "Article mis à jour.")
    return redirect("/pos/catalog")


@router.post("/catalog/{item_id}/stock")
async def catalog_stock(request: Request, db: DbSession, user: StaffUser, item_id: int,
                        quantity: Annotated[str, Form()] = "", reason: Annotated[str, Form()] = "reappro",
                        reference: Annotated[str, Form()] = ""):
    hs = await load_hotel_settings(db)

    async def action():
        item = await db.get(PosItem, item_id)
        if item is None:
            raise BusinessError("Article introuvable.")
        await pos.adjust_stock(db, item, to_int(quantity, "Quantité"), reason, reference, hs, user)

    await attempt(request, db, action, "Mouvement de stock enregistré.")
    return redirect("/pos/catalog")


@router.get("/stock")
async def stock_movements(request: Request, db: DbSession, user: StaffUser, item_id: str = "", days: int = 30):
    hs = await load_hotel_settings(db)
    since = datetime.combine(today_of(hs) - timedelta(days=max(1, min(days, 365))), datetime.min.time())
    stmt = select(StockMovement).where(StockMovement.created_at >= since).order_by(StockMovement.created_at.desc())
    if item_id.isdigit():
        stmt = stmt.where(StockMovement.item_id == int(item_id))
    movements = (await db.scalars(stmt.limit(500))).unique().all()
    items = (await db.scalars(select(PosItem).where(PosItem.track_stock.is_(True)).order_by(PosItem.name))).all()
    sold = await db.scalar(select(func.coalesce(func.sum(-StockMovement.quantity), 0))
                           .where(StockMovement.reason == "vente", StockMovement.created_at >= since))
    return await render(request, db, "pos/stock.html", movements=movements, items=items, item_id=item_id,
                        days=days, sold=sold or 0)
