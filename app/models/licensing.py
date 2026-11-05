from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LicenseInfo(Base):
    """Licence d'exploitation activée sur ce serveur (une seule ligne active)."""

    __tablename__ = "license_info"

    id: Mapped[int] = mapped_column(primary_key=True)
    license_key: Mapped[str] = mapped_column(Text)
    license_id: Mapped[str] = mapped_column(String(40), index=True)
    hotel_name: Mapped[str] = mapped_column(String(160))
    hardware_id: Mapped[str] = mapped_column(String(40))
    license_type: Mapped[str] = mapped_column(String(20))
    max_rooms: Mapped[int] = mapped_column(Integer)
    features: Mapped[str] = mapped_column(String(200), default="")
    issued_at: Mapped[date] = mapped_column(Date)
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    activated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
