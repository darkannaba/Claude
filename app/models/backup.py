from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

BACKUP_DESTINATIONS = {"local": "Disque local", "drive": "Google Drive"}
BACKUP_STATUSES = {"running": "En cours d'exécution", "success": "Réussie", "failed": "Échec", "deleted": "Supprimée"}


class BackupJob(Base):
    """Historique des sauvegardes : une ligne par archive et par destination."""

    __tablename__ = "backup_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    destination: Mapped[str] = mapped_column(String(10), index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="manuelle")  # manuelle | automatique | avant_restauration
    status: Mapped[str] = mapped_column(String(10), default="running", index=True)
    archive_name: Mapped[str] = mapped_column(String(120), default="")
    local_path: Mapped[str] = mapped_column(Text, default="")
    remote_id: Mapped[str] = mapped_column(String(120), default="")
    remote_link: Mapped[str] = mapped_column(Text, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    @property
    def destination_label(self) -> str:
        return BACKUP_DESTINATIONS.get(self.destination, self.destination)

    @property
    def status_label(self) -> str:
        return BACKUP_STATUSES.get(self.status, self.status)

    @property
    def size_label(self) -> str:
        size = float(self.size_bytes)
        for unit in ("o", "Ko", "Mo", "Go"):
            if size < 1024 or unit == "Go":
                return f"{size:.0f} {unit}" if unit == "o" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} Go"
