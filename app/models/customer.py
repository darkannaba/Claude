from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

ID_TYPES = {
    "cni": "Carte nationale d'identité",
    "passport": "Passeport",
    "permis": "Permis de conduire",
    "autre": "Autre pièce",
}
ID_TYPES_AR = {
    "cni": "بطاقة التعريف الوطنية",
    "passport": "جواز السفر",
    "permis": "رخصة السياقة",
    "autre": "وثيقة أخرى",
}
GUEST_STATUSES = {"normal": "Normal", "vip": "VIP", "blacklist": "Liste noire"}
GENDERS = {"": "—", "M": "Homme", "F": "Femme"}


def normalize_id_number(value: str | None) -> str:
    """Numéro de pièce normalisé pour la détection de doublons (majuscules, sans séparateurs)."""
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    nif: Mapped[str] = mapped_column(String(40), default="")
    rc: Mapped[str] = mapped_column(String(40), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    email: Mapped[str] = mapped_column(String(160), default="")
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Guest(Base):
    __tablename__ = "guests"
    __table_args__ = (
        Index(
            "uq_guests_identity", "id_type", "id_number_norm", unique=True,
            sqlite_where=text("id_number_norm != ''"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80), index=True)
    first_name_ar: Mapped[str] = mapped_column(String(80), default="")
    last_name_ar: Mapped[str] = mapped_column(String(80), default="")
    gender: Mapped[str] = mapped_column(String(1), default="")
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    birth_place: Mapped[str] = mapped_column(String(120), default="")
    nationality: Mapped[str] = mapped_column(String(80), default="Algérienne")
    profession: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(40), default="", index=True)
    email: Mapped[str] = mapped_column(String(160), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    id_type: Mapped[str] = mapped_column(String(20), default="cni")
    id_number: Mapped[str] = mapped_column(String(60), default="")
    id_number_norm: Mapped[str] = mapped_column(String(60), default="", index=True)
    id_issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    id_issue_place: Mapped[str] = mapped_column(String(120), default="")
    id_expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Pièce numérisée : chemins RELATIFS à data/media (ex. « id_scans/12/3f…a9.jpg »), jamais servis par /static.
    id_scan_path: Mapped[str] = mapped_column(String(255), default="")
    id_scan_back_path: Mapped[str] = mapped_column(String(255), default="")
    id_scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="normal", index=True)
    status_reason: Mapped[str] = mapped_column(Text, default="")
    preferences: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    company: Mapped[Company | None] = relationship(lazy="joined")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def full_name_ar(self) -> str:
        return f"{self.first_name_ar} {self.last_name_ar}".strip()

    @property
    def id_type_label(self) -> str:
        return ID_TYPES.get(self.id_type, self.id_type)

    @property
    def has_id_scan(self) -> bool:
        return bool(self.id_scan_path or self.id_scan_back_path)

    def id_expired_on(self, day: date) -> bool:
        """Vrai si la pièce d'identité est expirée à la date indiquée."""
        return self.id_expiry_date is not None and self.id_expiry_date < day

    @property
    def is_vip(self) -> bool:
        return self.status == "vip"

    @property
    def is_blacklisted(self) -> bool:
        return self.status == "blacklist"
