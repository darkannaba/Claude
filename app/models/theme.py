"""Charte graphique de l'établissement (white-label) : une seule ligne ``id = 1``.

Le nom de l'établissement, l'adresse et les identifiants fiscaux (NIF, NIS, RC, AI) restent dans
``HotelSettings`` (app/services/settings.py) : ce modèle ne stocke que l'apparence.
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

THEME_FONTS = {"inter": "Inter", "roboto": "Roboto", "tajawal": "Tajawal", "system": "Police du système"}
THEME_DENSITIES = {"compact": "Compacte", "comfortable": "Confortable"}
BRANDING_KINDS = {"logo": "Logo principal", "logo-dark": "Logo pour le mode sombre", "favicon": "Favicon"}


class ThemeSettings(Base):
    __tablename__ = "theme_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Palette (chaînes « #rrggbb »)
    primary_color: Mapped[str] = mapped_column(String(7), default="#0d9488")
    secondary_color: Mapped[str] = mapped_column(String(7), default="#0284c7")
    card_color: Mapped[str] = mapped_column(String(7), default="#ffffff")
    status_available_color: Mapped[str] = mapped_column(String(7), default="#10b981")
    status_occupied_color: Mapped[str] = mapped_column(String(7), default="#0ea5e9")
    status_dirty_color: Mapped[str] = mapped_column(String(7), default="#f59e0b")
    status_maintenance_color: Mapped[str] = mapped_column(String(7), default="#f43f5e")
    # Typographie & mise en page
    font_family: Mapped[str] = mapped_column(String(20), default="inter")
    density: Mapped[str] = mapped_column(String(20), default="comfortable")
    # Fichiers de marque (noms sous data/media/branding, vides si absents)
    logo_file: Mapped[str] = mapped_column(String(120), default="")
    logo_dark_file: Mapped[str] = mapped_column(String(120), default="")
    favicon_file: Mapped[str] = mapped_column(String(120), default="")
    # Documents émis (factures, fiches de police, tickets)
    slogan: Mapped[str] = mapped_column(String(200), default="")
    slogan_ar: Mapped[str] = mapped_column(String(200), default="")
    document_header: Mapped[str] = mapped_column(Text, default="")
    document_footer: Mapped[str] = mapped_column(Text, default="")
    show_logo_on_documents: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    version: Mapped[int] = mapped_column(Integer, default=1)
