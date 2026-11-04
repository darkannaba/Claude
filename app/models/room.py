from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text
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
    name_ar: Mapped[str] = mapped_column(String(80), default="")
    base_price: Mapped[int] = mapped_column(Integer, default=0)  # DZD TTC par nuit
    max_occupancy: Mapped[int] = mapped_column(Integer, default=2)
    description: Mapped[str] = mapped_column(Text, default="")


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(12), unique=True)
    floor: Mapped[int] = mapped_column(Integer, default=0)
    room_type_id: Mapped[int] = mapped_column(ForeignKey("room_types.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="available", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    room_type: Mapped[RoomType] = relationship(lazy="joined")

    @property
    def status_label(self) -> str:
        return ROOM_STATUSES.get(self.status, self.status)


class RatePeriod(Base):
    """Tarif saisonnier : remplace le prix de base sur [start_date, end_date)."""

    __tablename__ = "rate_periods"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    room_type_id: Mapped[int] = mapped_column(ForeignKey("room_types.id"), index=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    price: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=0)

    room_type: Mapped[RoomType] = relationship(lazy="joined")
