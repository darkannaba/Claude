from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

RES_STATUSES = {
    "confirmed": "Confirmée",
    "checked_in": "En séjour",
    "checked_out": "Clôturée",
    "cancelled": "Annulée",
    "no_show": "No-show",
}
ACTIVE_STATUSES = ("confirmed", "checked_in")
CHANNELS = ["Direct", "Téléphone", "Walk-in", "Email", "Booking.com", "Agence de voyage", "Entreprise"]


class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, default="")
    guest_id: Mapped[int] = mapped_column(ForeignKey("guests.id"), index=True)
    room_type_id: Mapped[int] = mapped_column(ForeignKey("room_types.id"))
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    check_in: Mapped[date] = mapped_column(Date, index=True)
    check_out: Mapped[date] = mapped_column(Date, index=True)
    adults: Mapped[int] = mapped_column(Integer, default=1)
    children: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="confirmed", index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    channel: Mapped[str] = mapped_column(String(40), default="Direct")
    # Prix par nuit imposé par un canal OTA (Booking.com, Expedia…) : remplace le calendrier tarifaire
    nightly_rate_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coming_from: Mapped[str] = mapped_column(String(120), default="")
    going_to: Mapped[str] = mapped_column(String(120), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    cancel_reason: Mapped[str] = mapped_column(Text, default="")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_out_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    guest = relationship("Guest", lazy="joined")
    room = relationship("Room", lazy="joined")
    room_type = relationship("RoomType", lazy="joined")
    company = relationship("Company", lazy="joined")
    segments = relationship(
        "StaySegment", back_populates="reservation", cascade="all, delete-orphan",
        order_by="StaySegment.start_date", lazy="selectin",
    )
    companions = relationship("Companion", cascade="all, delete-orphan", order_by="Companion.id", lazy="selectin")
    folio_items = relationship("FolioItem", cascade="all, delete-orphan", order_by="FolioItem.id", lazy="selectin")
    payments = relationship(
        "Payment", cascade="all, delete-orphan", order_by="Payment.id", lazy="selectin",
        primaryjoin="Reservation.id == Payment.reservation_id",
    )
    invoices = relationship("Invoice", cascade="all, delete-orphan", order_by="Invoice.id", lazy="selectin")

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def status_label(self) -> str:
        return RES_STATUSES.get(self.status, self.status)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def final_invoice(self):
        return self.invoices[-1] if self.invoices else None


class StaySegment(Base):
    """Occupation d'une chambre sur [start_date, end_date) – découpée lors d'un changement de chambre."""

    __tablename__ = "stay_segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="CASCADE"), index=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date, index=True)

    reservation = relationship("Reservation", back_populates="segments", lazy="joined")
    room = relationship("Room", lazy="joined")

    @property
    def nights(self) -> int:
        return (self.end_date - self.start_date).days


class Companion(Base):
    """Accompagnateur (famille, collègue) – figure sur la fiche de police."""

    __tablename__ = "companions"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="CASCADE"), index=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    first_name_ar: Mapped[str] = mapped_column(String(80), default="")
    last_name_ar: Mapped[str] = mapped_column(String(80), default="")
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    birth_place: Mapped[str] = mapped_column(String(120), default="")
    nationality: Mapped[str] = mapped_column(String(80), default="")
    relationship_label: Mapped[str] = mapped_column(String(60), default="")
    id_type: Mapped[str] = mapped_column(String(20), default="cni")
    id_number: Mapped[str] = mapped_column(String(60), default="")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def full_name_ar(self) -> str:
        return f"{self.first_name_ar} {self.last_name_ar}".strip()
