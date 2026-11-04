"""Tarification d'une nuit : tarif saisonnier > prix de base, puis week-end, puis remise entreprise."""
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.property import RatePeriod, RoomType
from app.services.settings import weekend_set


def nightly_price(db: Session, room_type: RoomType, night: date, company=None, settings: dict | None = None) -> int:
    period = db.scalars(
        select(RatePeriod)
        .where(
            RatePeriod.room_type_id == room_type.id,
            RatePeriod.start_date <= night,
            RatePeriod.end_date > night,
        )
        .order_by(RatePeriod.priority.desc(), RatePeriod.id.desc())
    ).first()
    price = float(period.price_ttc if period else room_type.base_price_ttc)

    if settings and night.weekday() in weekend_set(settings):
        price *= (100 + settings["weekend_surcharge_percent"]) / 100
    if company is not None and company.discount_percent:
        price *= (100 - company.discount_percent) / 100
    return int(round(price))
