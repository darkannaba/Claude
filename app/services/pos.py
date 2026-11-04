"""Points de vente annexes : additions, règlement comptant ou imputation sur la chambre, stock."""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Reservation
from app.models.invoice import PAYMENT_METHODS, Payment
from app.models.pos import OUTLETS, PosItem, PosOrder, PosOrderLine, StockMovement
from app.models.user import User
from app.services import booking
from app.services.audit_log import log_action
from app.services.common import BusinessError
from app.services.invoicing import next_sequence_number
from app.services.settings import HotelSettings, now_of


async def create_order(db: AsyncSession, outlet: str, label: str, hs: HotelSettings, user: User | None,
                       enabled: dict[str, str]) -> PosOrder:
    if outlet not in OUTLETS:
        raise BusinessError("Point de vente inconnu.")
    if outlet not in enabled:
        raise BusinessError(f"Le module « {OUTLETS[outlet]} » est désactivé.")
    now = now_of(hs)
    order = PosOrder(outlet=outlet, label=label.strip()[:80], status="open", created_at=now,
                     user_id=user.id if user else None,
                     number=await next_sequence_number(db, PosOrder.number, hs.pos_prefix, now.year))
    db.add(order)
    await db.flush()
    return order


def _ensure_open(order: PosOrder) -> None:
    if order.status != "open":
        raise BusinessError("Cette addition est déjà clôturée.")


def _recompute(order: PosOrder) -> None:
    order.total = sum(line.amount for line in order.lines)


async def add_line(db: AsyncSession, order: PosOrder, item: PosItem, quantity: int) -> PosOrderLine:
    _ensure_open(order)
    if quantity < 1:
        raise BusinessError("Quantité invalide.")
    if not item.active:
        raise BusinessError("Article inactif.")
    if item.outlet != order.outlet:
        raise BusinessError("Cet article n'appartient pas à ce point de vente.")
    already = sum(line.quantity for line in order.lines if line.item_id == item.id)
    if item.track_stock and item.stock_qty < already + quantity:
        raise BusinessError(f"Stock insuffisant pour « {item.name} » ({item.stock_qty} disponible(s)).")
    line = next((ln for ln in order.lines if ln.item_id == item.id and ln.unit_price == item.price), None)
    if line is None:
        line = PosOrderLine(item_id=item.id, description=item.name, quantity=quantity, unit_price=item.price,
                            amount=item.price * quantity, vat_rate=item.vat_rate)
        order.lines.append(line)
    else:
        line.quantity += quantity
        line.amount = line.unit_price * line.quantity
    _recompute(order)
    return line


async def remove_line(db: AsyncSession, order: PosOrder, line_id: int) -> None:
    _ensure_open(order)
    line = next((ln for ln in order.lines if ln.id == line_id), None)
    if line is None:
        raise BusinessError("Ligne introuvable.")
    order.lines.remove(line)
    await db.delete(line)
    _recompute(order)


async def _consume_stock(db: AsyncSession, order: PosOrder, user: User | None, hs: HotelSettings) -> None:
    for line in order.lines:
        if line.item_id is None:
            continue
        item = await db.get(PosItem, line.item_id)
        if item is None or not item.track_stock:
            continue
        if item.stock_qty < line.quantity:
            raise BusinessError(f"Stock insuffisant pour « {item.name} » ({item.stock_qty} disponible(s)).")
        item.stock_qty -= line.quantity
        db.add(StockMovement(item_id=item.id, quantity=-line.quantity, reason="vente", reference=order.number,
                             user_id=user.id if user else None, created_at=now_of(hs)))


async def settle_cash(db: AsyncSession, order: PosOrder, method: str, hs: HotelSettings, user: User | None) -> Payment:
    _ensure_open(order)
    if not order.lines:
        raise BusinessError("L'addition est vide.")
    if method not in PAYMENT_METHODS:
        raise BusinessError("Mode de règlement invalide.")
    await _consume_stock(db, order, user, hs)
    now = now_of(hs)
    payment = Payment(pos_order_id=order.id, amount=order.total, method=method, kind="paiement",
                      business_date=now.date(), paid_at=now, reference=order.number,
                      user_id=user.id if user else None, note=OUTLETS[order.outlet])
    db.add(payment)
    order.status, order.payment_method, order.closed_at = "paid", method, now
    await log_action(db, user, "pos_paid", "pos_order", order.number,
                     f"{order.total} DA – {PAYMENT_METHODS[method]}", now)
    return payment


async def settle_room_charge(db: AsyncSession, order: PosOrder, res: Reservation, hs: HotelSettings,
                             user: User | None) -> None:
    """Imputation sur la note de chambre (« Room Charge ») : une écriture de folio par taux de TVA."""
    _ensure_open(order)
    if not order.lines:
        raise BusinessError("L'addition est vide.")
    if res.status != "checked_in":
        raise BusinessError("Imputation possible uniquement sur une chambre en séjour.")
    await _consume_stock(db, order, user, hs)
    by_rate: dict[int, int] = defaultdict(int)
    for line in order.lines:
        by_rate[line.vat_rate] += line.amount
    for rate, amount in sorted(by_rate.items()):
        await booking.add_extra(
            db, res, description=f"{OUTLETS[order.outlet]} – addition {order.number}"
                                 + (f" (TVA {rate} %)" if len(by_rate) > 1 else ""),
            amount=amount, vat_rate=rate, hs=hs, user=user, kind="pos", pos_order_id=order.id)
    now = now_of(hs)
    order.status, order.reservation, order.closed_at = "room_charge", res, now
    await log_action(db, user, "pos_room_charge", "pos_order", order.number,
                     f"{order.total} DA → {res.code} (ch. {res.room.number})", now)


async def void_order(db: AsyncSession, order: PosOrder, hs: HotelSettings, user: User | None) -> None:
    _ensure_open(order)
    order.status, order.closed_at = "void", now_of(hs)
    await log_action(db, user, "pos_void", "pos_order", order.number, "", now_of(hs))


async def adjust_stock(db: AsyncSession, item: PosItem, quantity: int, reason: str, reference: str,
                       hs: HotelSettings, user: User | None) -> StockMovement:
    """Entrée (réapprovisionnement) ou ajustement d'inventaire (quantité signée)."""
    if reason not in ("reappro", "ajustement"):
        raise BusinessError("Motif de mouvement invalide.")
    if quantity == 0:
        raise BusinessError("Quantité nulle.")
    if reason == "reappro" and quantity < 0:
        raise BusinessError("Un réapprovisionnement doit être positif.")
    if item.stock_qty + quantity < 0:
        raise BusinessError("Le stock ne peut pas devenir négatif.")
    item.track_stock = True
    item.stock_qty += quantity
    movement = StockMovement(item_id=item.id, quantity=quantity, reason=reason, reference=reference.strip()[:80],
                             user_id=user.id if user else None, created_at=now_of(hs))
    db.add(movement)
    await log_action(db, user, "stock", "pos_item", item.id, f"{reason} {quantity:+d} → {item.stock_qty}", now_of(hs))
    return movement
