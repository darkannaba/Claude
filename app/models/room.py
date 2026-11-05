from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Niveaux de saison (tarification dynamique) : libellés traduits via app/locales/*/pricing.json.
SEASON_LEVELS = {
    "high": "Haute saison",
    "medium": "Moyenne saison",
    "low": "Basse saison",
}

# Cibles et champs tracés dans l'historique des modifications de prix (table price_changes).
PRICE_TARGETS = {
    "room_type": "Type de chambre",
    "room": "Chambre",
    "season": "Saison",
    "long_stay": "Remise long séjour",
    "weekend": "Majoration week-end",
}
PRICE_FIELDS = {
    "base_price": "Prix de base",
    "price_override": "Prix spécifique",
    "adjustment_percent": "Ajustement (%)",
    "discount_percent": "Remise (%)",
    "season": "Saison",
    "long_stay": "Palier",
    "weekend_days": "Jours de week-end",
    "weekend_surcharge_percent": "Majoration (%)",
}

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
    # Prix spécifique de la chambre (DZD TTC par nuit) : remplace le prix du type ; None = prix du type.
    price_override: Mapped[int | None] = mapped_column(Integer, nullable=True)

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


class Season(Base):
    """Saison tarifaire : ajustement en % (positif ou négatif) du prix de référence sur [start_date, end_date).

    ``room_type_id`` nul = la saison concerne tous les types. Si plusieurs saisons actives couvrent une nuit,
    celle de plus haute priorité l'emporte (à priorité égale, la plus récente).
    """

    __tablename__ = "seasons"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    level: Mapped[str] = mapped_column(String(10), default="medium")
    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date, index=True)
    adjustment_percent: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    room_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("room_types.id", ondelete="CASCADE"), nullable=True, index=True)

    room_type: Mapped[RoomType | None] = relationship(lazy="joined")

    @property
    def level_label(self) -> str:
        return SEASON_LEVELS.get(self.level, self.level)

    def covers(self, night: date, room_type_id: int | None) -> bool:
        return (self.active and self.start_date <= night < self.end_date
                and (self.room_type_id is None or self.room_type_id == room_type_id))


class LongStayDiscount(Base):
    """Palier de remise long séjour : ``discount_percent`` dès ``min_nights`` nuits (nombre total du séjour)."""

    __tablename__ = "long_stay_discounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    min_nights: Mapped[int] = mapped_column(Integer, unique=True)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PriceChange(Base):
    """Historique des modifications de prix : qui, quand, quelle cible, ancienne et nouvelle valeur, motif."""

    __tablename__ = "price_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    username: Mapped[str] = mapped_column(String(64), default="système")
    target_type: Mapped[str] = mapped_column(String(20), index=True)
    target_id: Mapped[str] = mapped_column(String(40), default="")
    target_label: Mapped[str] = mapped_column(String(160), default="")
    field: Mapped[str] = mapped_column(String(40), default="")
    old_value: Mapped[str] = mapped_column(String(80), default="")
    new_value: Mapped[str] = mapped_column(String(80), default="")
    reason: Mapped[str] = mapped_column(Text, default="")

    @property
    def target_type_label(self) -> str:
        return PRICE_TARGETS.get(self.target_type, self.target_type)

    @property
    def field_label(self) -> str:
        return PRICE_FIELDS.get(self.field, self.field)
