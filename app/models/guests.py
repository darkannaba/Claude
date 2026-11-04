from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

ID_TYPES = {
    "passport": "Passeport",
    "cni": "Carte nationale d'identité",
    "autre": "Autre pièce",
}


class Guest(Base):
    """Fiche client (CRM). Le numéro de pièce est indexé pour la recherche / scanner."""

    __tablename__ = "guests"

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80), index=True)
    phone: Mapped[str] = mapped_column(String(40), default="", index=True)
    email: Mapped[str] = mapped_column(String(160), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    nationality: Mapped[str] = mapped_column(String(80), default="")
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    id_type: Mapped[str] = mapped_column(String(20), default="cni")
    id_number: Mapped[str] = mapped_column(String(60), default="", index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    preferences: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    company = relationship("Company", lazy="joined")
    reservations = relationship("Reservation", back_populates="guest")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()
