"""Règles de réservation : disponibilité, création, modification, arrivée, départ, annulation.

Toutes les fonctions modifient la session sans la committer : c'est le routeur qui
commit, ou rollback en cas de BookingError.
"""
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.property import Room, RoomType
from app.models.reservations import ACTIVE_STATUSES, Reservation, StaySegment
from app.services.common import BookingError
from app.services.folio import compute_folio
from app.services.settings import today_of


def room_is_free(db: Session, room_id: int, start: date, end: date, exclude_reservation_id: int | None = None) -> bool:
    stmt = (
        select(func.count(StaySegment.id))
        .join(Reservation, StaySegment.reservation_id == Reservation.id)
        .where(
            StaySegment.room_id == room_id,
            StaySegment.start_date < end,
            StaySegment.end_date > start,
            Reservation.status.in_(ACTIVE_STATUSES),
        )
    )
    if exclude_reservation_id is not None:
        stmt = stmt.where(Reservation.id != exclude_reservation_id)
    return db.scalar(stmt) == 0


def free_rooms(db: Session, start: date, end: date, room_type_id: int | None = None,
               exclude_reservation_id: int | None = None) -> list[Room]:
    stmt = select(Room).order_by(Room.number)
    if room_type_id:
        stmt = stmt.where(Room.room_type_id == room_type_id)
    return [
        r for r in db.scalars(stmt).all()
        if room_is_free(db, r.id, start, end, exclude_reservation_id)
    ]


def occupying_reservation(db: Session, room_id: int, day: date, exclude_id: int | None = None) -> Reservation | None:
    stmt = (
        select(Reservation)
        .join(StaySegment, StaySegment.reservation_id == Reservation.id)
        .where(
            StaySegment.room_id == room_id,
            StaySegment.start_date <= day,
            StaySegment.end_date > day,
            Reservation.status == "checked_in",
        )
    )
    if exclude_id is not None:
        stmt = stmt.where(Reservation.id != exclude_id)
    return db.scalars(stmt).first()


def _validate_dates(check_in: date, check_out: date) -> None:
    if check_out <= check_in:
        raise BookingError("La date de départ doit être postérieure à la date d'arrivée.")


def _check_capacity(room_type: RoomType, adults: int, children: int) -> None:
    if adults < 1:
        raise BookingError("Au moins un adulte est requis.")
    if adults + children > room_type.max_occupancy:
        raise BookingError(f"Capacité maximale de ce type de chambre dépassée ({room_type.max_occupancy} pers.).")


def create_reservation(db: Session, *, guest, room: Room, check_in: date, check_out: date,
                       adults: int = 1, children: int = 0, company_id: int | None = None,
                       channel: str = "Direct", notes: str = "", now: datetime | None = None) -> Reservation:
    _validate_dates(check_in, check_out)
    _check_capacity(room.room_type, adults, children)
    if not room_is_free(db, room.id, check_in, check_out):
        raise BookingError(f"La chambre {room.number} n'est pas disponible sur cette période.")

    res = Reservation(
        guest_id=guest.id, room_id=room.id, room_type_id=room.room_type_id,
        check_in=check_in, check_out=check_out, adults=adults, children=children,
        status="confirmed", company_id=company_id, channel=channel or "Direct", notes=notes or "",
        created_at=now or datetime.now(),
    )
    res.segments.append(StaySegment(room=room, start_date=check_in, end_date=check_out))
    db.add(res)
    db.flush()
    res.code = f"RES{res.id:06d}"
    return res


def reassign_dates(db: Session, res: Reservation, room: Room, check_in: date, check_out: date) -> None:
    """Modifie dates / chambre d'une réservation confirmée (pas encore arrivée)."""
    if res.status != "confirmed":
        raise BookingError("Seules les réservations confirmées peuvent être modifiées ici.")
    _validate_dates(check_in, check_out)
    _check_capacity(room.room_type, res.adults, res.children)
    if not room_is_free(db, room.id, check_in, check_out, exclude_reservation_id=res.id):
        raise BookingError(f"La chambre {room.number} n'est pas disponible sur la nouvelle période.")

    for seg in list(res.segments):
        res.segments.remove(seg)
    res.segments.append(StaySegment(room=room, start_date=check_in, end_date=check_out))
    res.room = room
    res.room_id = room.id
    res.room_type_id = room.room_type_id
    res.check_in = check_in
    res.check_out = check_out


def change_room(db: Session, res: Reservation, new_room: Room, from_date: date, settings: dict) -> None:
    """Change de chambre à partir de from_date (le segment est découpé)."""
    today = today_of(settings)
    if res.status not in ("confirmed", "checked_in"):
        raise BookingError("Changement de chambre impossible pour ce statut de réservation.")
    if new_room.id == res.room_id:
        raise BookingError("Le client occupe déjà cette chambre.")
    if res.status == "checked_in" and from_date != today:
        raise BookingError("Pour un séjour en cours, le changement de chambre prend effet aujourd'hui.")
    if not (max(today, res.check_in) <= from_date < res.check_out):
        raise BookingError("Date de changement hors du séjour.")
    if new_room.status == "maintenance" and from_date <= today:
        raise BookingError(f"La chambre {new_room.number} est en maintenance.")
    if not room_is_free(db, new_room.id, from_date, res.check_out, exclude_reservation_id=res.id):
        raise BookingError(f"La chambre {new_room.number} n'est pas disponible à partir de cette date.")

    old_room = res.room
    for seg in list(res.segments):
        if seg.end_date <= from_date:
            continue
        if seg.start_date >= from_date:
            res.segments.remove(seg)
        else:
            seg.end_date = from_date
    res.segments.append(StaySegment(room=new_room, start_date=from_date, end_date=res.check_out))

    if res.status == "checked_in":
        old_room.status = "dirty"
        new_room.status = "occupied"
    res.room = new_room
    res.room_id = new_room.id
    res.room_type_id = new_room.room_type_id


def check_in(db: Session, res: Reservation, settings: dict, now: datetime) -> None:
    today = today_of(settings)
    if res.status != "confirmed":
        raise BookingError("Seules les réservations confirmées peuvent être enregistrées à l'arrivée.")
    if today < res.check_in:
        raise BookingError(f"Arrivée prévue le {res.check_in:%d/%m/%Y}.")
    if today >= res.check_out:
        raise BookingError("La date de départ est déjà atteinte.")
    guest = res.guest
    if not (guest.id_type and guest.id_number):
        raise BookingError("Pièce d'identité du client obligatoire avant l'arrivée (fiche de police).")
    room = res.room
    if room.status == "maintenance":
        raise BookingError(f"La chambre {room.number} est en maintenance.")
    other = occupying_reservation(db, room.id, today, exclude_id=res.id)
    if other is not None:
        raise BookingError(f"La chambre {room.number} est occupée par {other.code}.")

    res.status = "checked_in"
    res.checked_in_at = now
    room.status = "occupied"


def check_out(db: Session, res: Reservation, settings: dict, now: datetime, force: bool = False) -> None:
    """Départ : ramène le séjour à la date du jour (anticipé ou tardif) puis contrôle le solde."""
    today = today_of(settings)
    if res.status != "checked_in":
        raise BookingError("Seul un séjour en cours peut être clôturé.")
    if today <= res.check_in:
        raise BookingError("Le séjour doit compter au moins une nuit avant le départ.")

    for seg in list(res.segments):
        if seg.start_date >= today:
            res.segments.remove(seg)
    last = sorted(res.segments, key=lambda s: s.start_date)[-1]
    last.end_date = today
    res.check_out = today
    res.status = "checked_out"
    res.checked_out_at = now

    folio = compute_folio(db, res, settings)
    if folio.balance > 0 and not force:
        raise BookingError(
            f"Solde impayé de {folio.balance:,} {settings['currency_symbol']}. "
            "Encaissez le solde ou cochez « départ malgré solde impayé »."
        )
    res.room.status = "dirty"


def cancel(res: Reservation, now: datetime) -> None:
    if res.status != "confirmed":
        raise BookingError("Seule une réservation confirmée peut être annulée.")
    res.status = "cancelled"
    res.cancelled_at = now


def mark_no_show(res: Reservation, settings: dict) -> None:
    if res.status != "confirmed":
        raise BookingError("Seule une réservation confirmée peut être marquée no-show.")
    if today_of(settings) < res.check_in:
        raise BookingError("Le no-show ne peut être déclaré qu'à partir de la date d'arrivée.")
    res.status = "no_show"
