"""Journal des numérisations de pièces d'identité (lecture MRZ par douchette, OCR d'image ou de PDF)."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

ID_SCAN_SOURCES = {
    "mrz_scanner": "Douchette / lecteur MRZ",
    "image_upload": "Image téléversée",
    "webcam": "Photo webcam",
    "pdf": "Document PDF",
}
ID_SCAN_ENGINES = {"mrz": "Analyse MRZ", "tesseract": "OCR Tesseract", "none": "Aucun moteur"}
ID_SCAN_STATUSES = {
    "extracted": "Données extraites",
    "partial": "Extraction partielle",
    "failed": "Échec de la lecture",
    "applied": "Appliqué à la fiche client",
}
ID_SCAN_SIDES = {"recto": "Recto", "verso": "Verso"}


class IdScan(Base):
    """Une tentative de lecture : fichier conservé (le cas échéant), texte brut, champs extraits, contrôles."""

    __tablename__ = "id_scans"

    id: Mapped[int] = mapped_column(primary_key=True)
    guest_id: Mapped[int | None] = mapped_column(ForeignKey("guests.id", ondelete="SET NULL"), nullable=True, index=True)
    reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("reservations.id", ondelete="SET NULL"), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(20), default="mrz_scanner")
    side: Mapped[str] = mapped_column(String(10), default="recto")
    # Chemin RELATIF à data/media (« id_scans/<client>/<aléatoire>.jpg ») ; vide pour une saisie de douchette.
    file_path: Mapped[str] = mapped_column(String(255), default="")
    mime_type: Mapped[str] = mapped_column(String(60), default="")
    engine: Mapped[str] = mapped_column(String(20), default="none")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    raw_mrz: Mapped[str] = mapped_column(Text, default="")
    fields_json: Mapped[str] = mapped_column(Text, default="{}")
    checksums_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="failed", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def is_image(self) -> bool:
        return self.mime_type.startswith("image/")
