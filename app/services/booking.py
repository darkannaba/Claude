"""Règles de réservation et de séjour.

Les fonctions modifient la session sans committer : le routeur commit, ou fait un
rollback si une ``BusinessError`` est levée (la session est alors entièrement annulée).
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import ACTIVE_STATUSES, Companion, Reservation, StaySegment
from app.models.customer import Company, Guest
from app.models.invoice import PAYMENT_KINDS, PAYMENT_METHODS, FolioItem, Payment
from app.models.room import Room
from app.models.user import User
from app.services.audit_log import log_action
from app.services.common import BusinessError
from app.services.folio import build_folio, room_key
from app.services.invoicing import issue_invoice
from app.services.night_audit import is_day_closed, post_stay_nights
from app.services.pricing import load_calendar
from app.services.settings import HotelSettings, now_of, today_of


# ---------------------------------------------------------------- disponibilité
async def room_is_free(db: AsyncSession, room_id: int, start: date, end: date,
                       exclude_reservation_id: int | None = None) -> bool:
    stmt = (
        select(func.count(StaySegment.id))
        .join(Reservation, StaySegment.reservation_id == Reservation.id)
        .where(StaySegment.room_id == room_id, StaySegment.start_date < end, StaySegment.end_date > start,
               Reservation.status.in_(ACTIVE_STATUSES))
    )
    if exclude_reservation_id is not None:
        stmt = stmt.where(Reservation.id != exclude_reservation_id)
    return (await db.scalar(stmt)) == 0


async def free_rooms(db: AsyncSession, start: date, end: date, room_type_id: int | None = None,
                     exclude_reservation_id: int | None = None) -> list[Room]:
    busy_stmt = (
        select(StaySegment.room_id)
        .join(Reservation, StaySegment.reservation_id == Reservation.id)
        .where(StaySegment.start_date < end, StaySegment.end_date > start, Reservation.status.in_(ACTIVE_STATUSES))
    )
    if exclude_reservation_id is not None:
        busy_stmt = busy_stmt.where(Reservation.id != exclude_reservation_id)
    busy = set((await db.scalars(busy_stmt)).all())
    stmt = select(Room).where(Room.active.is_(True)).order_by(Room.number)
    if room_type_id:
        stmt = stmt.where(Room.room_type_id == room_type_id)
    return [room for room in (await db.scalars(stmt)).unique().all() if room.id not in busy]


async def occupying_reservation(db: AsyncSession, room_id: int, day: date,
                                exclude_id: int | None = None) -> Reservation | None:
    stmt = (
        select(Reservation)
        .join(StaySegment, StaySegment.reservation_id == Reservation.id)
        .where(StaySegment.room_id == room_id, StaySegment.start_date <= day, StaySegment.end_date > day,
               Reservation.status == "checked_in")
    )
    if exclude_id is not None:
        stmt = stmt.where(Reservation.id != exclude_id)
    return (await db.scalars(stmt)).unique().first()


def _validate_dates(check_in: date, check_out: date) -> None:
    if check_out <= check_in:
        raise BusinessError("La date de départ doit être postérieure à la date d'arrivée.")
    if (check_out - check_in).days > 365:
        raise BusinessError("Un séjour ne peut pas dépasser 365 nuits.")


def _check_capacity(room: Room, adults: int, children: int) -> None:
    if adults < 1:
        raise BusinessError("Au moins un adulte est requis.")
    if children < 0:
        raise BusinessError("Nombre d'enfants invalide.")
    if adults + children > room.room_type.max_occupancy:
        raise BusinessError(
            f"Capacité maximale de la chambre {room.number} dépassée ({room.room_type.max_occupancy} pers.).")


# ---------------------------------------------------------------- réservations
async def create_reservation(
    db: AsyncSession, *, guest: Guest, room: Room, check_in: date, check_out: date, hs: HotelSettings,
    user: User | None, adults: int = 1, children: int = 0, company: Company | None = None,
    channel: str = "Direct", coming_from: str = "", going_to: str = "", notes: str = "",
    allow_blacklisted: bool = False, nightly_rate_override: int | None = None,
) -> Reservation:
    if guest.is_blacklisted and not allow_blacklisted:
        raise BusinessError(f"Client sur liste noire : {guest.status_reason or 'motif non précisé'}. "
                            "Seul un administrateur peut forcer la réservation.")
    if not room.active:
        raise BusinessError(f"La chambre {room.number} est désactivée.")
    _validate_dates(check_in, check_out)
    _check_capacity(room, adults, children)
    if not await room_is_free(db, room.id, check_in, check_out):
        raise BusinessError(f"La chambre {room.number} n'est pas disponible sur cette période.")

    now = now_of(hs)
    res = Reservation(
        guest=guest, room=room, room_type=room.room_type, company=company,
        check_in=check_in, check_out=check_out, adults=adults, children=children, status="confirmed",
        channel=channel or "Direct", coming_from=coming_from, going_to=going_to, notes=notes,
        nightly_rate_override=nightly_rate_override, created_by_id=user.id if user else None, created_at=now,
    )
    # Collections initialisées explicitement : en asynchrone, un accès ultérieur ne doit jamais
    # déclencher de chargement paresseux (acompte saisi à la création, par exemple).
    res.segments = [StaySegment(room=room, start_date=check_in, end_date=check_out)]
    res.companions, res.folio_items, res.payments, res.invoices = [], [], [], []
    db.add(res)
    await db.flush()
    res.code = f"RES{res.id:06d}"
    await log_action(db, user, "reservation_created", "reservation", res.code,
                     f"{guest.full_name} – ch. {room.number} – {check_in} → {check_out}", now)
    return res


async def update_reservation(
    db: AsyncSession, res: Reservation, *, room: Room, check_in: date, check_out: date, adults: int,
    children: int, company: Company | None, hs: HotelSettings, user: User | None,
) -> None:
    """Modifie dates, chambre et occupation d'une réservation non encore arrivée."""
    if res.status != "confirmed":
        raise BusinessError("Seules les réservations confirmées (non arrivées) sont modifiables.")
    _validate_dates(check_in, check_out)
    _check_capacity(room, adults, children)
    if not room.active:
        raise BusinessError(f"La chambre {room.number} est désactivée.")
    if not await room_is_free(db, room.id, check_in, check_out, exclude_reservation_id=res.id):
        raise BusinessError(f"La chambre {room.number} n'est pas disponible sur la nouvelle période.")
    for segment in list(res.segments):
        res.segments.remove(segment)
    res.segments.append(StaySegment(room=room, start_date=check_in, end_date=check_out))
    res.room = room
    res.room_type = room.room_type
    res.check_in, res.check_out = check_in, check_out
    res.adults, res.children = adults, children
    res.company = company
    await log_action(db, user, "reservation_updated", "reservation", res.code,
                     f"ch. {room.number} – {check_in} → {check_out}", now_of(hs))


async def change_room(db: AsyncSession, res: Reservation, new_room: Room, from_date: date, hs: HotelSettings,
                      user: User | None) -> None:
    """Change de chambre à partir de ``from_date`` : le segment d'occupation est découpé."""
    today = today_of(hs)
    if res.status not in ACTIVE_STATUSES:
        raise BusinessError("Changement de chambre impossible pour ce statut.")
    if new_room.id == res.room_id:
        raise BusinessError("Le client occupe déjà cette chambre.")
    if not new_room.active:
        raise BusinessError(f"La chambre {new_room.number} est désactivée.")
    if res.status == "checked_in" and from_date != today:
        raise BusinessError("Pour un séjour en cours, le changement de chambre prend effet aujourd'hui.")
    if not (max(today, res.check_in) <= from_date < res.check_out):
        raise BusinessError("Date de changement hors de la période du séjour.")
    if room_key(res.id, from_date) in {i.night_key for i in res.folio_items}:
        raise BusinessError("Cette nuit est déjà clôturée par l'audit de nuit.")
    if new_room.status == "maintenance" and from_date <= today:
        raise BusinessError(f"La chambre {new_room.number} est en maintenance.")
    _check_capacity(new_room, res.adults, res.children)
    if not await room_is_free(db, new_room.id, from_date, res.check_out, exclude_reservation_id=res.id):
        raise BusinessError(f"La chambre {new_room.number} n'est pas disponible à partir de cette date.")

    old_room = res.room
    for segment in list(res.segments):
        if segment.end_date <= from_date:
            continue
        if segment.start_date >= from_date:
            res.segments.remove(segment)
        else:
            segment.end_date = from_date
    res.segments.append(StaySegment(room=new_room, start_date=from_date, end_date=res.check_out))
    if res.status == "checked_in":
        now = now_of(hs)
        old_room.status, old_room.status_changed_at = "dirty", now
        new_room.status, new_room.status_changed_at = "occupied", now
    res.room = new_room
    res.room_type = new_room.room_type
    await log_action(db, user, "room_changed", "reservation", res.code,
                     f"{old_room.number} → {new_room.number} au {from_date}", now_of(hs))


async def check_in(db: AsyncSession, res: Reservation, hs: HotelSettings, user: User | None,
                   allow_blacklisted: bool = False) -> None:
    today = today_of(hs)
    if res.status != "confirmed":
        raise BusinessError("Seules les réservations confirmées peuvent être enregistrées à l'arrivée.")
    if today < res.check_in:
        raise BusinessError(f"Arrivée prévue le {res.check_in:%d/%m/%Y}.")
    if today >= res.check_out:
        raise BusinessError("La date de départ prévue est déjà atteinte : modifiez d'abord les dates.")
    guest = res.guest
    if guest.is_blacklisted and not allow_blacklisted:
        raise BusinessError("Client sur liste noire : l'arrivée doit être autorisée par un administrateur.")
    if hs.require_id_for_checkin and not (guest.id_type and guest.id_number):
        raise BusinessError("Pièce d'identité obligatoire avant l'arrivée (fiche de police).")
    room = res.room
    if room.status == "maintenance":
        raise BusinessError(f"La chambre {room.number} est en maintenance.")
    other = await occupying_reservation(db, room.id, today, exclude_id=res.id)
    if other is not None:
        raise BusinessError(f"La chambre {room.number} est occupée par la réservation {other.code}.")
    now = now_of(hs)
    res.status = "checked_in"
    res.checked_in_at = now
    room.status, room.status_changed_at = "occupied", now
    await log_action(db, user, "check_in", "reservation", res.code, f"Chambre {room.number}", now)


async def check_out(db: AsyncSession, res: Reservation, hs: HotelSettings, user: User | None,
                    force: bool = False):
    """Départ : séjour ramené à la date réelle, nuits figées, solde contrôlé, facture définitive émise."""
    today = today_of(hs)
    now = now_of(hs)
    if res.status != "checked_in":
        raise BusinessError("Seul un séjour en cours peut être clôturé.")
    if today <= res.check_in:
        raise BusinessError("Le séjour doit compter au moins une nuit : utilisez l'annulation si besoin.")

    for segment in list(res.segments):
        if segment.start_date >= today:
            res.segments.remove(segment)
    last = sorted(res.segments, key=lambda s: s.start_date)[-1]
    last.end_date = today
    res.check_out = today

    calendar = await load_calendar(db, hs)
    post_stay_nights(res, calendar, hs, today, "checkout", user, now)
    folio = build_folio(res, hs, None)
    if folio.balance > 0 and not force:
        raise BusinessError(
            f"Solde impayé de {folio.balance:,} {hs.currency_symbol}. Encaissez le solde ou cochez "
            "« départ malgré solde impayé » (le client apparaîtra dans les impayés).".replace(",", " "))
    res.status = "checked_out"
    res.checked_out_at = now
    room = res.room
    room.status, room.status_changed_at = "dirty", now
    invoice = await issue_invoice(db, res, hs, user, now)
    await log_action(db, user, "check_out", "reservation", res.code,
                     f"Facture {invoice.number} – solde {folio.balance} DA", now)
    return invoice


async def cancel(db: AsyncSession, res: Reservation, hs: HotelSettings, user: User | None, reason: str = "") -> None:
    if res.status != "confirmed":
        raise BusinessError("Seule une réservation confirmée peut être annulée.")
    now = now_of(hs)
    res.status = "cancelled"
    res.cancelled_at = now
    res.cancel_reason = reason.strip()
    await log_action(db, user, "reservation_cancelled", "reservation", res.code, reason, now)


async def mark_no_show(db: AsyncSession, res: Reservation, hs: HotelSettings, user: User | None) -> None:
    if res.status != "confirmed":
        raise BusinessError("Seule une réservation confirmée peut être déclarée no-show.")
    if today_of(hs) < res.check_in:
        raise BusinessError("Le no-show ne peut être déclaré qu'à partir de la date d'arrivée.")
    res.status = "no_show"
    await log_action(db, user, "no_show", "reservation", res.code, "", now_of(hs))


# ---------------------------------------------------------------- folio
async def add_payment(db: AsyncSession, res: Reservation, *, amount: int, kind: str, method: str,
                      hs: HotelSettings, user: User | None, reference: str = "", note: str = "") -> Payment:
    if amount <= 0:
        raise BusinessError("Le montant doit être positif.")
    if kind not in PAYMENT_KINDS:
        raise BusinessError("Type de versement invalide.")
    if method not in PAYMENT_METHODS:
        raise BusinessError("Mode de règlement invalide.")
    if res.status in ("cancelled", "no_show") and kind != "remboursement":
        raise BusinessError("Seul un remboursement est possible sur une réservation annulée ou no-show.")
    now = now_of(hs)
    payment = Payment(amount=amount, kind=kind, method=method, business_date=now.date(), paid_at=now,
                      reference=reference.strip()[:80], note=note.strip()[:200], user_id=user.id if user else None)
    res.payments.append(payment)
    await log_action(db, user, "payment", "reservation", res.code,
                     f"{PAYMENT_KINDS[kind]} {amount} DA ({PAYMENT_METHODS[method]})", now)
    return payment


async def add_extra(db: AsyncSession, res: Reservation, *, description: str, amount: int, vat_rate: int,
                    hs: HotelSettings, user: User | None, kind: str = "extra", quantity: int = 1,
                    pos_order_id: int | None = None) -> FolioItem:
    if res.status not in ACTIVE_STATUSES:
        raise BusinessError("Prestations possibles uniquement pour une réservation confirmée ou en séjour.")
    if not description.strip():
        raise BusinessError("Libellé requis.")
    if amount <= 0:
        raise BusinessError("Le montant doit être positif.")
    now = now_of(hs)
    item = FolioItem(
        business_date=now.date(), kind=kind, description=description.strip()[:200], quantity=quantity,
        unit_price=amount // max(quantity, 1), amount=amount, vat_rate=vat_rate, source="pos" if pos_order_id else "manual",
        pos_order_id=pos_order_id, posted_by_id=user.id if user else None, posted_at=now,
    )
    res.folio_items.append(item)
    await log_action(db, user, "folio_post", "reservation", res.code, f"{description} {amount} DA", now)
    return item


async def void_item(db: AsyncSession, res: Reservation, item_id: int, hs: HotelSettings, user: User,
                    reason: str = "") -> None:
    """Annule une écriture : suppression si la journée est ouverte, sinon contre-passation (écriture négative)."""
    item = next((i for i in res.folio_items if i.id == item_id), None)
    if item is None:
        raise BusinessError("Écriture introuvable.")
    if item.invoice_id is not None:
        raise BusinessError("Écriture facturée : la facture définitive ne peut plus être modifiée.")
    if item.reversal_of_id is not None or any(i.reversal_of_id == item.id for i in res.folio_items):
        raise BusinessError("Écriture déjà contre-passée.")
    if item.kind in ("room", "tax") and user.role != "admin":
        raise BusinessError("Seul un administrateur peut corriger une nuitée ou une taxe figée.")
    now = now_of(hs)
    if item.kind in ("room", "tax") or await is_day_closed(db, item.business_date):
        res.folio_items.append(FolioItem(
            business_date=now.date(), kind=item.kind, description=f"Annulation : {item.description}"[:200],
            quantity=item.quantity, unit_price=-item.unit_price, amount=-item.amount, vat_rate=item.vat_rate,
            room_id=item.room_id, source="manual", reversal_of_id=item.id, posted_by_id=user.id, posted_at=now,
        ))
        action = "folio_reversal"
    else:
        res.folio_items.remove(item)
        await db.delete(item)
        action = "folio_delete"
    await log_action(db, user, action, "reservation", res.code, f"{item.description} {item.amount} DA – {reason}", now)


async def add_companion(db: AsyncSession, res: Reservation, data: dict, hs: HotelSettings, user: User | None) -> Companion:
    if not data.get("first_name") or not data.get("last_name"):
        raise BusinessError("Nom et prénom de l'accompagnateur requis.")
    companion = Companion(**data)
    res.companions.append(companion)
    await log_action(db, user, "companion_added", "reservation", res.code,
                     f"{data['first_name']} {data['last_name']}", now_of(hs))
    return companion


def default_move_date(res: Reservation, hs: HotelSettings) -> date:
    return max(today_of(hs), res.check_in)


def nights_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days)]
