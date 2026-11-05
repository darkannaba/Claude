"""Channel manager / OTA : clés d'API, ARI (disponibilités, tarifs), réservations entrantes, journal."""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import ACTIVE_STATUSES, Reservation, StaySegment
from app.models.customer import Guest
from app.models.ota import OTA_CHANNELS, OTA_PERMISSIONS, OtaApiKey, OtaBooking, OtaLog
from app.models.room import RatePeriod, Room, RoomType
from app.models.user import User
from app.services import booking, crm
from app.services.audit_log import log_action
from app.services.common import BusinessError
from app.services.loaders import load_reservation
from app.services.pricing import load_calendar
from app.services.settings import HotelSettings, now_of, today_of

MAX_INVENTORY_DAYS = 366
LOG_PAYLOAD_LIMIT = 20_000
# Un seul processus (SQLite) : ce verrou sérialise les réservations entrantes et empêche deux
# webhooks simultanés d'attribuer la même chambre (protection contre la surréservation).
reservation_lock = asyncio.Lock()


# ---------------------------------------------------------------- schémas d'échange
class OtaGuestIn(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    email: str = Field("", max_length=160)
    phone: str = Field("", max_length=40)
    nationality: str = Field("", max_length=80)
    id_type: Literal["cni", "passport", "permis", "autre"] = "passport"
    id_number: str = Field("", max_length=60)
    address: str = Field("", max_length=300)


class OtaReservationIn(BaseModel):
    event: Literal["new", "modify", "cancel"]
    external_id: str = Field(min_length=1, max_length=60)
    room_type: str | None = Field(None, max_length=20)
    check_in: date | None = None
    check_out: date | None = None
    adults: int = Field(1, ge=1, le=12)
    children: int = Field(0, ge=0, le=12)
    guest: OtaGuestIn | None = None
    nightly_rate: int | None = Field(None, ge=0, description="Prix TTC par nuit négocié par le canal")
    total_amount: int | None = Field(None, ge=0, description="Montant TTC total du séjour (si pas de prix par nuit)")
    prepaid_amount: int = Field(0, ge=0, description="Montant déjà encaissé par l'OTA (virement)")
    notes: str = Field("", max_length=1000)

    @model_validator(mode="after")
    def _required_fields(self) -> "OtaReservationIn":
        if self.event in ("new", "modify"):
            missing = [name for name in ("room_type", "check_in", "check_out") if getattr(self, name) is None]
            if self.event == "new" and self.guest is None:
                missing.append("guest")
            if missing:
                raise ValueError(f"champs requis pour « {self.event} » : {', '.join(missing)}")
            if self.check_out <= self.check_in:
                raise ValueError("check_out doit être postérieur à check_in")
        return self


class OtaRateIn(BaseModel):
    room_type: str = Field(min_length=1, max_length=20)
    start: date
    end: date
    price: int = Field(ge=0)

    @model_validator(mode="after")
    def _dates(self) -> "OtaRateIn":
        if self.end <= self.start:
            raise ValueError("end doit être postérieur à start")
        return self


class OtaRatesIn(BaseModel):
    rates: list[OtaRateIn] = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------- clés d'API
def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


async def create_api_key(db: AsyncSession, *, name: str, channel: str, permissions: list[str],
                         hs: HotelSettings, user: User | None) -> tuple[OtaApiKey, str]:
    """Crée une clé : la valeur en clair n'est retournée qu'une fois, seule son empreinte est stockée."""
    if not name.strip():
        raise BusinessError("Nom de la clé requis.")
    if channel not in OTA_CHANNELS:
        raise BusinessError("Canal inconnu.")
    perms = [p for p in permissions if p in OTA_PERMISSIONS]
    if not perms:
        raise BusinessError("Sélectionnez au moins une permission.")
    raw = f"ota_{secrets.token_urlsafe(32)}"
    key = OtaApiKey(name=name.strip()[:120], channel=channel, key_prefix=raw[:12], key_hash=hash_key(raw),
                    permissions=",".join(perms), active=True, sync_enabled=True, created_at=now_of(hs))
    db.add(key)
    await db.flush()
    await log_action(db, user, "ota_key_created", "ota_key", key.id, f"{OTA_CHANNELS[channel]} – {key.key_prefix}…")
    return key, raw


async def authenticate(db: AsyncSession, raw_key: str) -> OtaApiKey | None:
    if not raw_key or not raw_key.startswith("ota_"):
        return None
    return await db.scalar(select(OtaApiKey).where(OtaApiKey.key_hash == hash_key(raw_key), OtaApiKey.active.is_(True)))


async def log_event(db: AsyncSession, *, key_id: int | None, channel: str, event: str, endpoint: str,
                    payload: object, response: object, http_status: int, external_ref: str = "",
                    reservation_id: int | None = None, when: datetime | None = None) -> None:
    """Journalise un échange. Reçoit des scalaires (et non l'objet clé) : utilisable après un rollback."""
    def dump(value: object) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        return text[:LOG_PAYLOAD_LIMIT]

    db.add(OtaLog(created_at=when or datetime.now(), api_key_id=key_id,
                  channel=channel, event=event, endpoint=endpoint[:120],
                  external_ref=external_ref[:60], payload=dump(payload), response=dump(response),
                  http_status=http_status, reservation_id=reservation_id))


# ---------------------------------------------------------------- ARI : disponibilités et tarifs
async def inventory(db: AsyncSession, hs: HotelSettings, start: date, end: date) -> dict:
    """Disponibilités et tarifs réels par type de chambre et par nuit sur [start, end)."""
    if end <= start:
        raise BusinessError("end doit être postérieur à start.")
    if (end - start).days > MAX_INVENTORY_DAYS:
        raise BusinessError(f"Période limitée à {MAX_INVENTORY_DAYS} jours.")
    rooms = list((await db.scalars(select(Room).where(Room.active.is_(True)))).unique().all())
    room_types = list((await db.scalars(select(RoomType).order_by(RoomType.code))).all())
    segments = (await db.scalars(
        select(StaySegment).join(Reservation, StaySegment.reservation_id == Reservation.id)
        .where(StaySegment.start_date < end, StaySegment.end_date > start, Reservation.status.in_(ACTIVE_STATUSES))
    )).unique().all()
    calendar = await load_calendar(db, hs, start, end)
    nights = [start + timedelta(days=i) for i in range((end - start).days)]
    room_type_of = {room.id: room.room_type_id for room in rooms}
    result = []
    for room_type in room_types:
        sellable = [r for r in rooms if r.room_type_id == room_type.id and r.status != "maintenance"]
        out_of_service = sum(1 for r in rooms if r.room_type_id == room_type.id and r.status == "maintenance")
        days = []
        for night in nights:
            booked = sum(1 for s in segments if room_type_of.get(s.room_id) == room_type.id
                         and s.start_date <= night < s.end_date and s.room_id in {r.id for r in sellable})
            available = max(0, len(sellable) - booked)
            days.append({"date": night.isoformat(), "available": available, "booked": booked,
                         "rate": calendar.price(room_type, night), "stop_sell": available == 0})
        result.append({"code": room_type.code, "name": room_type.name, "max_occupancy": room_type.max_occupancy,
                       "total_rooms": len(sellable) + out_of_service, "out_of_service": out_of_service, "days": days})
    return {"hotel": hs.hotel_name, "currency": hs.currency_code, "start": start.isoformat(), "end": end.isoformat(),
            "room_types": result}


async def apply_rates(db: AsyncSession, payload: OtaRatesIn, key: OtaApiKey) -> dict:
    """Mises à jour tarifaires reçues d'un canal : périodes « OTA <canal> » prioritaires sur les saisons."""
    types = {rt.code.upper(): rt for rt in (await db.scalars(select(RoomType))).all()}
    name = f"OTA {key.channel_label}"
    created = updated = 0
    for rate in payload.rates:
        room_type = types.get(rate.room_type.upper())
        if room_type is None:
            raise BusinessError(f"Type de chambre inconnu : {rate.room_type}.")
        existing = await db.scalar(select(RatePeriod).where(
            RatePeriod.name == name, RatePeriod.room_type_id == room_type.id,
            RatePeriod.start_date == rate.start, RatePeriod.end_date == rate.end))
        if existing is None:
            db.add(RatePeriod(name=name, room_type_id=room_type.id, start_date=rate.start, end_date=rate.end,
                              price=rate.price, priority=50))
            created += 1
        else:
            existing.price = rate.price
            updated += 1
    return {"created": created, "updated": updated}


# ---------------------------------------------------------------- réservations entrantes
async def match_or_create_guest(db: AsyncSession, data: OtaGuestIn, hs: HotelSettings) -> tuple[Guest, bool]:
    """Rapprochement CRM : pièce d'identité, puis email, téléphone, nom ; sinon création de la fiche."""
    if data.id_number:
        guest = await crm.find_duplicate(db, data.id_type, data.id_number)
        if guest is not None:
            return guest, False
    if data.email.strip():
        guest = await db.scalar(select(Guest).where(func.lower(Guest.email) == data.email.strip().lower()))
        if guest is not None:
            return guest, False
    digits = "".join(ch for ch in data.phone if ch.isdigit())
    if len(digits) >= 8:
        candidates = (await db.scalars(select(Guest).where(Guest.phone.like(f"%{digits[-4:]}%")))).unique().all()
        for guest in candidates:
            if "".join(ch for ch in guest.phone if ch.isdigit())[-9:] == digits[-9:]:
                return guest, False
    guest = await db.scalar(select(Guest).where(
        func.lower(Guest.first_name) == data.first_name.strip().lower(),
        func.lower(Guest.last_name) == data.last_name.strip().lower()))
    if guest is not None:
        return guest, False
    guest = await crm.save_guest(db, None, {
        "first_name": data.first_name, "last_name": data.last_name, "email": data.email, "phone": data.phone,
        "nationality": data.nationality, "id_type": data.id_type, "id_number": data.id_number,
        "address": data.address, "birth_date": None, "id_issue_date": None, "company_id": None,
    }, hs, None)
    return guest, True


def _nightly_rate(payload: OtaReservationIn) -> int | None:
    if payload.nightly_rate is not None:
        return payload.nightly_rate
    if payload.total_amount is not None:
        return round(payload.total_amount / (payload.check_out - payload.check_in).days)
    return None


async def _room_type(db: AsyncSession, code: str) -> RoomType:
    room_type = await db.scalar(select(RoomType).where(func.upper(RoomType.code) == code.upper()))
    if room_type is None:
        raise BusinessError(f"Type de chambre inconnu : {code}.")
    return room_type


def _summary(res: Reservation, status: str) -> dict:
    return {"status": status, "reservation_code": res.code, "reservation_id": res.id, "room": res.room.number,
            "check_in": res.check_in.isoformat(), "check_out": res.check_out.isoformat(),
            "pms_status": res.status}


async def handle_reservation(db: AsyncSession, payload: OtaReservationIn, key: OtaApiKey,
                             hs: HotelSettings) -> tuple[int, dict]:
    """Traite un webhook de réservation. Retourne (code HTTP, corps JSON). Ne committe pas."""
    channel = key.channel_label
    mapping = await db.scalar(select(OtaBooking).where(OtaBooking.channel == key.channel,
                                                       OtaBooking.external_id == payload.external_id))
    if payload.event == "new":
        if mapping is not None:  # même webhook reçu deux fois : réponse idempotente
            res = await load_reservation(db, mapping.reservation_id)
            return 200, _summary(res, "duplicate")
        if payload.check_in < today_of(hs):
            raise BusinessError("Date d'arrivée passée.")
        room_type = await _room_type(db, payload.room_type)
        free = await booking.free_rooms(db, payload.check_in, payload.check_out, room_type.id)
        free = [r for r in free if r.status != "maintenance"]
        if not free:
            return 409, {"status": "overbooking", "detail": f"Plus aucune chambre « {room_type.name} » disponible "
                                                            f"du {payload.check_in} au {payload.check_out}."}
        free.sort(key=lambda r: (r.status != "available", r.number))
        guest, created = await match_or_create_guest(db, payload.guest, hs)
        notes = f"Réservation {channel} n° {payload.external_id}." + (f" {payload.notes}" if payload.notes else "")
        res = await booking.create_reservation(
            db, guest=guest, room=free[0], check_in=payload.check_in, check_out=payload.check_out, hs=hs, user=None,
            adults=payload.adults, children=payload.children, channel=channel, notes=notes,
            nightly_rate_override=_nightly_rate(payload))
        if payload.prepaid_amount:
            await booking.add_payment(db, res, amount=payload.prepaid_amount, kind="acompte", method="virement",
                                      hs=hs, user=None, reference=f"{channel} {payload.external_id}",
                                      note=f"Prépaiement {channel}")
        db.add(OtaBooking(channel=key.channel, external_id=payload.external_id, reservation_id=res.id,
                          created_at=now_of(hs), updated_at=now_of(hs)))
        await db.flush()
        body = _summary(res, "created")
        body["guest_created"] = created
        return 201, body

    if mapping is None:
        return 404, {"status": "unknown", "detail": f"Réservation {channel} n° {payload.external_id} inconnue."}
    res = await load_reservation(db, mapping.reservation_id)
    if payload.event == "cancel":
        if res.status == "cancelled":
            return 200, _summary(res, "already_cancelled")
        await booking.cancel(db, res, hs, None, f"Annulation {channel} n° {payload.external_id}")
        mapping.updated_at = now_of(hs)
        return 200, _summary(res, "cancelled")

    # modification
    room_type = await _room_type(db, payload.room_type)
    room = res.room
    if room.room_type_id != room_type.id or not await booking.room_is_free(
            db, room.id, payload.check_in, payload.check_out, exclude_reservation_id=res.id):
        free = await booking.free_rooms(db, payload.check_in, payload.check_out, room_type.id, exclude_reservation_id=res.id)
        free = [r for r in free if r.status != "maintenance"]
        if not free:
            return 409, {"status": "overbooking", "detail": "Modification impossible : aucune chambre disponible."}
        room = free[0]
    await booking.update_reservation(db, res, room=room, check_in=payload.check_in, check_out=payload.check_out,
                                     adults=payload.adults, children=payload.children, company=res.company,
                                     hs=hs, user=None)
    rate = _nightly_rate(payload)
    if rate is not None:
        res.nightly_rate_override = rate
    mapping.updated_at = now_of(hs)
    return 200, _summary(res, "modified")
