from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

ROOM_STATUSES = {
    "available": "Libre",
    "occupied": "Occupée",
    "dirty": "À nettoyer",
    "maintenance": "En maintenance",
}


class RoomType(Base):
    __tablename__ = "room_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    base_price_ttc: Mapped[int] = mapped_column(Integer, default=0)  # DZD par nuit, TTC
    max_occupancy: Mapped[int] = mapped_column(Integer, default=2)


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(12), unique=True)
    floor: Mapped[int] = mapped_column(Integer, default=0)
    room_type_id: Mapped[int] = mapped_column(ForeignKey("room_types.id"))
    status: Mapped[str] = mapped_column(String(20), default="available", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    room_type: Mapped[RoomType] = relationship(lazy="joined")


class RatePeriod(Base):
    """Tarif saisonnier : remplace le prix de base pour un type de chambre sur une période.

    end_date est exclue (la nuit du end_date n'est pas concernée).
    """

    __tablename__ = "rate_periods"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    room_type_id: Mapped[int] = mapped_column(ForeignKey("room_types.id"))
    price_ttc: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=0)

    room_type: Mapped[RoomType] = relationship(lazy="joined")
