from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuditLog(Base):
    """Journal d'activité : qui a fait quoi, quand (traçabilité légale et interne)."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    username: Mapped[str] = mapped_column(String(64), default="système")
    action: Mapped[str] = mapped_column(String(60), index=True)
    entity: Mapped[str] = mapped_column(String(40), default="")
    entity_id: Mapped[str] = mapped_column(String(40), default="")
    details: Mapped[str] = mapped_column(Text, default="")


class NightAuditRun(Base):
    """Clôture d'une journée : les nuitées de cette date sont figées dans folio_items."""

    __tablename__ = "night_audit_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_date: Mapped[date] = mapped_column(Date, unique=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    run_by: Mapped[str] = mapped_column(String(64), default="planificateur")
    rooms_posted: Mapped[int] = mapped_column(Integer, default=0)
    room_revenue: Mapped[int] = mapped_column(Integer, default=0)
    tax_posted: Mapped[int] = mapped_column(Integer, default=0)
    no_shows: Mapped[int] = mapped_column(Integer, default=0)
    occupancy_percent: Mapped[int] = mapped_column(Integer, default=0)
