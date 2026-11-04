from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

ITEM_KINDS = {
    "room": "Hébergement",
    "tax": "Taxe de séjour",
    "pos": "Point de vente",
    "extra": "Extra",
    "adjustment": "Correction",
}
PAYMENT_METHODS = {
    "especes": "Espèces",
    "cib": "CIB / EDAHABIA",
    "carte": "Carte internationale",
    "virement": "Virement",
    "cheque": "Chèque",
}
PAYMENT_KINDS = {"acompte": "Acompte (arrhes)", "paiement": "Paiement", "remboursement": "Remboursement"}


class FolioItem(Base):
    """Écriture du folio. Les nuitées sont figées ici par l'audit de nuit (prix gelé)."""

    __tablename__ = "folio_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="CASCADE"), index=True)
    business_date: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    description: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[int] = mapped_column(Integer)  # TTC, signé (négatif pour une contre-passation)
    vat_rate: Mapped[int] = mapped_column(Integer, default=0)
    room_id: Mapped[int | None] = mapped_column(ForeignKey("rooms.id"), nullable=True)
    night_key: Mapped[str | None] = mapped_column(String(60), unique=True, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # night_audit | checkout | pos | manual
    pos_order_id: Mapped[int | None] = mapped_column(ForeignKey("pos_orders.id", ondelete="SET NULL"), nullable=True)
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("folio_items.id"), nullable=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True)
    posted_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    room = relationship("Room", lazy="joined")

    @property
    def kind_label(self) -> str:
        return ITEM_KINDS.get(self.kind, self.kind)


class Payment(Base):
    """Encaissement : rattaché à une réservation, ou à une addition de point de vente payée comptant."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int | None] = mapped_column(ForeignKey("reservations.id", ondelete="CASCADE"), nullable=True, index=True)
    pos_order_id: Mapped[int | None] = mapped_column(ForeignKey("pos_orders.id", ondelete="SET NULL"), nullable=True, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(20), default="especes")
    kind: Mapped[str] = mapped_column(String(20), default="paiement")
    business_date: Mapped[date] = mapped_column(Date, index=True)
    paid_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    reference: Mapped[str] = mapped_column(String(80), default="")
    note: Mapped[str] = mapped_column(String(200), default="")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    user = relationship("User", lazy="joined")

    @property
    def signed_amount(self) -> int:
        return -self.amount if self.kind == "remboursement" else self.amount

    @property
    def method_label(self) -> str:
        return PAYMENT_METHODS.get(self.method, self.method)

    @property
    def kind_label(self) -> str:
        return PAYMENT_KINDS.get(self.kind, self.kind)


class Invoice(Base):
    """Facture définitive émise au départ : montants figés (snapshot), écritures verrouillées."""

    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(40), unique=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="CASCADE"), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    customer_name: Mapped[str] = mapped_column(String(200), default="")
    customer_details: Mapped[str] = mapped_column(Text, default="")
    total_ht: Mapped[int] = mapped_column(Integer, default=0)
    total_vat: Mapped[int] = mapped_column(Integer, default=0)
    tourist_tax: Mapped[int] = mapped_column(Integer, default=0)
    total_ttc: Mapped[int] = mapped_column(Integer, default=0)
    paid: Mapped[int] = mapped_column(Integer, default=0)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    issued_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
