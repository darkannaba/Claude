"""Audit de nuit / clôture journalière : fige le prix des nuitées dans folio_items.

Une nuitée imputée (clé unique ``night_key``) ne bouge plus, même si les tarifs
changent ensuite dans l'administration : les factures passées restent intangibles.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import NightAuditRun
from app.models.booking import Reservation
from app.models.invoice import FolioItem
from app.models.room import Room
from app.models.user import User
from app.services.audit_log import log_action
from app.services.common import BusinessError
from app.services.folio import room_key, tax_key, taxable_persons
from app.services.loaders import load_reservations
from app.services.pricing import RateCalendar, load_calendar
from app.services.settings import HotelSettings, now_of, today_of

MAX_CATCH_UP_DAYS = 31


def post_night(res: Reservation, segment, night: date, calendar: RateCalendar, hs: HotelSettings,
               source: str, user: User | None, now: datetime) -> tuple[bool, int, int]:
    """Impute la nuitée (et la taxe de séjour) d'une nuit. Idempotent.

    Retourne (nuitée imputée maintenant ?, montant nuitée, montant taxe).
    """
    keys = {item.night_key for item in res.folio_items if item.night_key}
    posted = False
    room_amount = tax_amount = 0
    rkey = room_key(res.id, night)
    if rkey not in keys:
        posted = True
        if res.nightly_rate_override is not None:  # tarif négocié par le canal OTA
            price = res.nightly_rate_override
            label = f"Nuitée {night:%d/%m/%Y} – Chambre {segment.room.number} – tarif {res.channel}"
        else:
            price = calendar.price(segment.room.room_type, night, res.company)
            label = f"Nuitée {night:%d/%m/%Y} – Chambre {segment.room.number} ({segment.room.room_type.name})"
        res.folio_items.append(FolioItem(
            business_date=night, kind="room",
            description=label,
            quantity=1, unit_price=price, amount=price, vat_rate=hs.vat_rate_accommodation,
            room_id=segment.room_id, night_key=rkey, source=source,
            posted_by_id=user.id if user else None, posted_at=now,
        ))
        room_amount = price
    persons = taxable_persons(res, hs)
    tkey = tax_key(res.id, night)
    if hs.tourist_tax_enabled and persons and tkey not in keys:
        amount = hs.tourist_tax_per_person_night * persons
        res.folio_items.append(FolioItem(
            business_date=night, kind="tax",
            description=f"Taxe de séjour {night:%d/%m/%Y} – {persons} pers. × {hs.tourist_tax_per_person_night}",
            quantity=persons, unit_price=hs.tourist_tax_per_person_night, amount=amount, vat_rate=0,
            night_key=tkey, source=source, posted_by_id=user.id if user else None, posted_at=now,
        ))
        tax_amount = amount
    return posted, room_amount, tax_amount


def post_stay_nights(res: Reservation, calendar: RateCalendar, hs: HotelSettings, until: date, source: str,
                     user: User | None, now: datetime) -> tuple[int, int, int]:
    """Impute toutes les nuits non encore figées avant ``until`` (exclu). Retourne (nuits, CA, taxe)."""
    nights = revenue = tax = 0
    for segment in res.segments:
        night = segment.start_date
        end = min(segment.end_date, until)
        while night < end:
            posted, room_amount, tax_amount = post_night(res, segment, night, calendar, hs, source, user, now)
            nights += 1 if posted else 0
            revenue += room_amount
            tax += tax_amount
            night += timedelta(days=1)
    return nights, revenue, tax


async def is_day_closed(db: AsyncSession, day: date) -> bool:
    return bool(await db.scalar(select(func.count(NightAuditRun.id)).where(NightAuditRun.business_date == day)))


async def last_closed_date(db: AsyncSession) -> date | None:
    return await db.scalar(select(func.max(NightAuditRun.business_date)))


async def pending_audit_dates(db: AsyncSession, hs: HotelSettings) -> list[date]:
    """Journées passées non encore clôturées (rattrapage limité à 31 jours)."""
    today = today_of(hs)
    last = await last_closed_date(db)
    start = (last + timedelta(days=1)) if last else today - timedelta(days=1)
    start = max(start, today - timedelta(days=MAX_CATCH_UP_DAYS))
    days = []
    day = start
    while day < today:
        days.append(day)
        day += timedelta(days=1)
    return days


async def run_night_audit(db: AsyncSession, business_date: date, hs: HotelSettings,
                          user: User | None = None) -> NightAuditRun:
    now = now_of(hs)
    today = today_of(hs)
    if business_date > today:
        raise BusinessError("Impossible de clôturer une journée future.")
    if await is_day_closed(db, business_date):
        raise BusinessError(f"La journée du {business_date:%d/%m/%Y} est déjà clôturée.")

    calendar = await load_calendar(db, hs)
    in_house = await load_reservations(db, Reservation.status == "checked_in", Reservation.check_in <= business_date)
    rooms_posted = revenue = tax = 0
    occupied_rooms = 0
    for res in in_house:
        if any(s.start_date <= business_date < s.end_date for s in res.segments):
            occupied_rooms += 1
        nights, amount, tax_amount = post_stay_nights(
            res, calendar, hs, business_date + timedelta(days=1), "night_audit", user, now)
        rooms_posted += nights
        revenue += amount
        tax += tax_amount

    no_shows = 0
    if hs.auto_no_show:
        late = await load_reservations(db, Reservation.status == "confirmed", Reservation.check_in <= business_date)
        for res in late:
            res.status = "no_show"
            res.cancel_reason = f"No-show automatique (audit du {business_date:%d/%m/%Y})"
            no_shows += 1

    total_rooms = await db.scalar(select(func.count(Room.id)).where(Room.active.is_(True))) or 0
    run = NightAuditRun(
        business_date=business_date, run_at=now, run_by=user.username if user else "planificateur",
        rooms_posted=rooms_posted, room_revenue=revenue, tax_posted=tax, no_shows=no_shows,
        occupancy_percent=round(100 * occupied_rooms / total_rooms) if total_rooms else 0,
    )
    db.add(run)
    await log_action(db, user, "night_audit", "night_audit", business_date.isoformat(),
                     f"{rooms_posted} nuitée(s), CA {revenue} DA, taxe {tax} DA, {no_shows} no-show", now)
    await db.flush()
    return run


async def run_pending_audits(db: AsyncSession, hs: HotelSettings, user: User | None = None) -> list[NightAuditRun]:
    runs = []
    for day in await pending_audit_dates(db, hs):
        runs.append(await run_night_audit(db, day, hs, user))
    return runs
