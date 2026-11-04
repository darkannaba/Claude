"""Moteur de tarification : tarif saisonnier > prix de base, puis week-end, puis remise entreprise."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.room import RatePeriod, RoomType
from app.services.settings import HotelSettings


class RateCalendar:
    """Calendrier tarifaire chargé une fois puis évalué en mémoire (aucune requête par nuit)."""

    def __init__(self, periods: list[RatePeriod], hs: HotelSettings):
        self.periods = sorted(periods, key=lambda p: (p.priority, p.id or 0), reverse=True)
        self.hs = hs

    def base_price(self, room_type: RoomType, night: date) -> int:
        for period in self.periods:
            if period.room_type_id == room_type.id and period.start_date <= night < period.end_date:
                return period.price
        return room_type.base_price

    def price(self, room_type: RoomType, night: date, company=None) -> int:
        amount = float(self.base_price(room_type, night))
        if night.weekday() in self.hs.weekend_set:
            amount *= (100 + self.hs.weekend_surcharge_percent) / 100
        if company is not None and company.discount_percent:
            amount *= (100 - company.discount_percent) / 100
        return max(0, int(round(amount)))


async def load_calendar(db: AsyncSession, hs: HotelSettings, start: date | None = None,
                        end: date | None = None) -> RateCalendar:
    stmt = select(RatePeriod)
    if start is not None and end is not None:
        stmt = stmt.where(RatePeriod.start_date < end, RatePeriod.end_date > start)
    periods = list((await db.scalars(stmt)).unique().all())
    return RateCalendar(periods, hs)
