from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

OTA_CHANNELS = {
    "booking": "Booking.com",
    "expedia": "Expedia",
    "airbnb": "Airbnb",
    "channex": "Channex",
    "dedge": "D-Edge",
    "rategain": "RateGain",
    "autre": "Autre canal",
}
OTA_PERMISSIONS = {
    "inventory:read": "Lecture des disponibilités et tarifs (ARI)",
    "rates:write": "Mise à jour des tarifs",
    "reservations:write": "Réservations entrantes (webhook)",
}


class OtaApiKey(Base):
    """Clé d'API d'un channel manager / OTA. Seule l'empreinte SHA-256 est stockée."""

    __tablename__ = "ota_api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    channel: Mapped[str] = mapped_column(String(20), index=True)
    key_prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    permissions: Mapped[str] = mapped_column(String(200), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def permission_list(self) -> list[str]:
        return [p for p in self.permissions.split(",") if p]

    @property
    def channel_label(self) -> str:
        return OTA_CHANNELS.get(self.channel, self.channel)


class OtaLog(Base):
    """Journal de synchronisation : chaque appel API et webhook reçu."""

    __tablename__ = "ota_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    api_key_id: Mapped[int | None] = mapped_column(ForeignKey("ota_api_keys.id", ondelete="SET NULL"), nullable=True)
    channel: Mapped[str] = mapped_column(String(20), default="", index=True)
    event: Mapped[str] = mapped_column(String(40))
    endpoint: Mapped[str] = mapped_column(String(120), default="")
    external_ref: Mapped[str] = mapped_column(String(60), default="")
    payload: Mapped[str] = mapped_column(Text, default="")
    response: Mapped[str] = mapped_column(Text, default="")
    http_status: Mapped[int] = mapped_column(Integer, default=200, index=True)
    reservation_id: Mapped[int | None] = mapped_column(ForeignKey("reservations.id", ondelete="SET NULL"), nullable=True)


class OtaBooking(Base):
    """Correspondance réservation OTA (n° externe) ↔ réservation PMS, pour les modifications et annulations."""

    __tablename__ = "ota_bookings"
    __table_args__ = (UniqueConstraint("channel", "external_id", name="uq_ota_bookings_channel_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(60))
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
