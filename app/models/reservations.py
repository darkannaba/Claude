from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

ACTIVE_STATUSES = ("confirmed", "checked_in")

STATUS_LABELS = {
    "confirmed": "Confirmée",
    "checked_in": "En séjour",
    "checked_out": "Clôturée",
    "cancelled": "Annulée",
    "no_show": "No-show",
}
PAYMENT_KINDS = {"acompte": "Acompte (arrhes)", "paiement": "Paiement", "remboursement": "Remboursement"}
PAYMENT_METHODS = {"especes": "Espèces", "carte": "Carte bancaire", "virement": "Virement", "cheque": "Chèque"}
CHARGE_CATEGORIES = {
    "autre": "Extra",
    "restaurant": "Restaurant / Bar",
    "salle_reunion": "Salle de réunion",
    "parking": "Parking",
}


class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, default="")
    guest_id: Mapped[int] = mapped_column(ForeignKey("guests.id"), index=True)
    room_type_id: Mapped[int] = mapped_column(ForeignKey("room_types.id"))
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)  # chambre actuelle
    check_in: Mapped[date] = mapped_column(Date, index=True)
    check_out: Mapped[date] = mapped_column(Date)
    adults: Mapped[int] = mapped_column(Integer, default=1)
    children: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="confirmed", index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    channel: Mapped[str] = mapped_column(String(40), default="Direct")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_out_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    guest = relationship("Guest", back_populates="reservations", lazy="joined")
    room = relationship("Room", lazy="joined")
    room_type = relationship("RoomType", lazy="joined")
    company = relationship("Company", lazy="joined")
    segments = relationship(
        "StaySegment", back_populates="reservation", cascade="all, delete-orphan",
        order_by="StaySegment.start_date", lazy="selectin",
    )
    companions = relationship(
        "Companion", cascade="all, delete-orphan", order_by="Companion.id", lazy="selectin",
    )
    payments = relationship(
        "Payment", cascade="all, delete-orphan", order_by="Payment.id", lazy="selectin",
    )
    charges = relationship(
        "Charge", cascade="all, delete-orphan", order_by="Charge.id", lazy="selectin",
    )
    invoices = relationship(
        "Invoice", cascade="all, delete-orphan", order_by="Invoice.id", lazy="selectin",
    )

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


class StaySegment(Base):
    """Occupation d'une chambre sur un intervalle [start_date, end_date).

    Source de vérité de l'occupation : un changement de chambre découpe le segment.
    """

    __tablename__ = "stay_segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="CASCADE"), index=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date)

    reservation = relationship("Reservation", back_populates="segments")
    room = relationship("Room", lazy="joined")


class Companion(Base):
    """Compagnon de voyage (figure aussi sur la fiche de police)."""

    __tablename__ = "companions"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="CASCADE"), index=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    nationality: Mapped[str] = mapped_column(String(80), default="")
    id_type: Mapped[str] = mapped_column(String(20), default="cni")
    id_number: Mapped[str] = mapped_column(String(60), default="")


class Payment(Base):
    """Versement (acompte, paiement, remboursement). Montants entiers en DZD."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="CASCADE"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), default="paiement")
    method: Mapped[str] = mapped_column(String(20), default="especes")
    paid_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    note: Mapped[str] = mapped_column(String(200), default="")


class Charge(Base):
    """Prestation / extra porté au folio (TTC)."""

    __tablename__ = "charges"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(40), default="autre")
    amount_ttc: Mapped[int] = mapped_column(Integer)
    posted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Invoice(Base):
    """Numéro de facture attribué à la première impression (réimpressions = même numéro)."""

    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(40), unique=True, default="")
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="CASCADE"), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
