"""CRM : fiches clients, détection et fusion des doublons, impayés, alertes VIP / liste noire, recherche."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Reservation
from app.models.customer import GENDERS, GUEST_STATUSES, ID_TYPES, Guest, normalize_id_number
from app.models.invoice import Invoice
from app.models.pos import PosOrder
from app.models.user import User
from app.services.audit_log import log_action
from app.services.common import BusinessError, like_pattern
from app.services.folio import build_folio
from app.services.loaders import load_reservations
from app.services.settings import HotelSettings, now_of

GUEST_TEXT_FIELDS = (
    "first_name", "last_name", "first_name_ar", "last_name_ar", "birth_place", "nationality", "profession",
    "phone", "email", "address", "id_number", "id_issue_place", "preferences", "notes",
)


async def find_duplicate(db: AsyncSession, id_type: str, id_number: str, exclude_id: int | None = None) -> Guest | None:
    norm = normalize_id_number(id_number)
    if not norm:
        return None
    stmt = select(Guest).where(Guest.id_type == id_type, Guest.id_number_norm == norm)
    if exclude_id is not None:
        stmt = stmt.where(Guest.id != exclude_id)
    return (await db.scalars(stmt)).unique().first()


async def save_guest(db: AsyncSession, guest: Guest | None, data: dict, hs: HotelSettings, user: User | None) -> Guest:
    """Crée ou met à jour une fiche client ; refuse un doublon de pièce d'identité."""
    clean = {k: (data.get(k) or "").strip() for k in GUEST_TEXT_FIELDS}
    if not clean["first_name"] or not clean["last_name"]:
        raise BusinessError("Nom et prénom requis.")
    id_type = data.get("id_type") or "cni"
    if id_type not in ID_TYPES:
        raise BusinessError("Type de pièce d'identité invalide.")
    gender = data.get("gender") or ""
    if gender not in GENDERS:
        raise BusinessError("Sexe invalide.")
    duplicate = await find_duplicate(db, id_type, clean["id_number"], exclude_id=guest.id if guest else None)
    if duplicate is not None:
        raise BusinessError(
            f"Doublon détecté : la pièce {ID_TYPES[id_type]} n° {clean['id_number']} appartient déjà à "
            f"{duplicate.full_name} (fiche n° {duplicate.id}).")
    creating = guest is None
    if creating:
        guest = Guest(created_at=now_of(hs))
        db.add(guest)
    for key, value in clean.items():
        setattr(guest, key, value)
    guest.id_type = id_type
    guest.gender = gender
    guest.id_number_norm = normalize_id_number(clean["id_number"])
    guest.birth_date = data.get("birth_date")
    guest.id_issue_date = data.get("id_issue_date")
    guest.company_id = data.get("company_id")
    if "nationality" in data and not clean["nationality"]:
        guest.nationality = "Algérienne"
    await db.flush()
    await log_action(db, user, "guest_created" if creating else "guest_updated", "guest", guest.id,
                     guest.full_name, now_of(hs))
    return guest


async def set_guest_status(db: AsyncSession, guest: Guest, status: str, reason: str, user: User, hs: HotelSettings) -> None:
    if status not in GUEST_STATUSES:
        raise BusinessError("Statut client invalide.")
    if status == "blacklist" and user.role != "admin":
        raise BusinessError("Seul un administrateur peut inscrire un client sur liste noire.")
    if guest.status == "blacklist" and status != "blacklist" and user.role != "admin":
        raise BusinessError("Seul un administrateur peut retirer un client de la liste noire.")
    if status == "blacklist" and not reason.strip():
        raise BusinessError("Le motif est obligatoire pour une inscription sur liste noire.")
    guest.status = status
    guest.status_reason = reason.strip()
    await log_action(db, user, "guest_status", "guest", guest.id, f"{status} – {reason}", now_of(hs))


async def potential_duplicates(db: AsyncSession) -> list[list[Guest]]:
    """Groupes de fiches probablement identiques (même nom + date de naissance, ou même téléphone)."""
    guests = list((await db.scalars(select(Guest).order_by(Guest.last_name, Guest.first_name))).unique().all())
    groups: dict[tuple, list[Guest]] = {}
    for guest in guests:
        name_key = (guest.last_name.strip().lower(), guest.first_name.strip().lower())
        if guest.birth_date:
            groups.setdefault(("name", *name_key, guest.birth_date), []).append(guest)
        phone = "".join(ch for ch in guest.phone if ch.isdigit())
        if len(phone) >= 8:
            groups.setdefault(("phone", phone), []).append(guest)
    seen: set[frozenset[int]] = set()
    result = []
    for members in groups.values():
        ids = frozenset(g.id for g in members)
        if len(ids) > 1 and ids not in seen:
            seen.add(ids)
            result.append(members)
    return result


async def merge_guests(db: AsyncSession, keep: Guest, remove: Guest, hs: HotelSettings, user: User) -> None:
    """Fusionne deux fiches : séjours rattachés à la fiche conservée, champs vides complétés."""
    if keep.id == remove.id:
        raise BusinessError("Impossible de fusionner une fiche avec elle-même.")
    await db.execute(update(Reservation).where(Reservation.guest_id == remove.id).values(guest_id=keep.id))
    for name in GUEST_TEXT_FIELDS + ("birth_date", "id_issue_date", "company_id", "gender"):
        if not getattr(keep, name) and getattr(remove, name):
            setattr(keep, name, getattr(remove, name))
    if not keep.id_number_norm and remove.id_number_norm:
        keep.id_type = remove.id_type
        keep.id_number_norm = remove.id_number_norm
        remove.id_number_norm = ""
    if remove.status == "blacklist" or (remove.status == "vip" and keep.status == "normal"):
        keep.status, keep.status_reason = remove.status, remove.status_reason
    await db.flush()
    await db.delete(remove)
    await log_action(db, user, "guest_merged", "guest", keep.id, f"Fiche {remove.id} fusionnée", now_of(hs))


async def unpaid_reservations(db: AsyncSession, hs: HotelSettings, guest_id: int | None = None) -> list[tuple[Reservation, int]]:
    criteria = [Reservation.status == "checked_out"]
    if guest_id is not None:
        criteria.append(Reservation.guest_id == guest_id)
    result = []
    for res in await load_reservations(db, *criteria, order_by=Reservation.check_out.desc()):
        balance = build_folio(res, hs, None).balance
        if balance > 0:
            result.append((res, balance))
    return result


@dataclass
class GuestAlert:
    level: str  # danger | warning | info
    message: str


async def guest_alerts(db: AsyncSession, guest: Guest, hs: HotelSettings) -> list[GuestAlert]:
    alerts = []
    if guest.is_blacklisted:
        alerts.append(GuestAlert("danger", f"Liste noire : {guest.status_reason or 'motif non précisé'}"))
    if guest.is_vip:
        alerts.append(GuestAlert("info", f"Client VIP{(' – ' + guest.status_reason) if guest.status_reason else ''}"))
    unpaid = await unpaid_reservations(db, hs, guest.id)
    if unpaid:
        total = sum(b for _, b in unpaid)
        alerts.append(GuestAlert("warning", f"Impayé : {total:,} {hs.currency_symbol} sur {len(unpaid)} séjour(s)"
                                 .replace(",", " ")))
    if not guest.id_number:
        alerts.append(GuestAlert("warning", "Pièce d'identité non renseignée (obligatoire pour la fiche de police)."))
    return alerts


@dataclass
class SearchResult:
    redirect: str | None = None
    guests: list = field(default_factory=list)
    reservations: list = field(default_factory=list)
    orders: list = field(default_factory=list)


async def search(db: AsyncSession, term: str) -> SearchResult:
    """Recherche universelle (douchette / scanner) : code réservation, QR, facture, addition, pièce, nom, téléphone."""
    q = term.strip()
    result = SearchResult()
    if not q:
        return result
    upper = q.upper()
    res = await db.scalar(select(Reservation.id).where(func.upper(Reservation.code) == upper))
    if res:
        result.redirect = f"/reservations/{res}"
        return result
    inv = await db.scalar(select(Invoice.reservation_id).where(func.upper(Invoice.number) == upper))
    if inv:
        result.redirect = f"/reservations/{inv}"
        return result
    order = await db.scalar(select(PosOrder.id).where(func.upper(PosOrder.number) == upper))
    if order:
        result.redirect = f"/pos/orders/{order}"
        return result
    norm = normalize_id_number(q)
    if norm:
        exact = list((await db.scalars(select(Guest).where(Guest.id_number_norm == norm))).unique().all())
        if len(exact) == 1:
            result.redirect = f"/guests/{exact[0].id}"
            return result

    pattern = like_pattern(q)
    result.guests = list((await db.scalars(
        select(Guest).where(or_(
            Guest.last_name.ilike(pattern, escape="\\"), Guest.first_name.ilike(pattern, escape="\\"),
            Guest.last_name_ar.like(pattern, escape="\\"), Guest.first_name_ar.like(pattern, escape="\\"),
            Guest.phone.like(pattern, escape="\\"), Guest.id_number.ilike(pattern, escape="\\"),
            Guest.email.ilike(pattern, escape="\\"),
        )).order_by(Guest.last_name).limit(50)
    )).unique().all())
    result.reservations = await load_reservations(
        db, or_(Reservation.code.ilike(pattern, escape="\\"),
                Reservation.guest_id.in_([g.id for g in result.guests] or [-1])),
        order_by=Reservation.check_in.desc(), limit=50)
    return result


def age_on(birth_date: date | None, day: date) -> int | None:
    if birth_date is None:
        return None
    return day.year - birth_date.year - ((day.month, day.day) < (birth_date.month, birth_date.day))
