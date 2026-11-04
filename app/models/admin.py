from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class User(Base):
    """Utilisateur de l'application. Rôles : admin, reception, housekeeping."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="reception")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Setting(Base):
    """Paramètre métier clé/valeur (valeur stockée en texte, typée à la lecture)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class FeatureFlag(Base):
    """Activation d'un module annexe (restaurant, salle de réunion, parking). Off par défaut."""

    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    label: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class Company(Base):
    """Entreprise cliente : tarif négocié = remise appliquée aux nuitées."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    tax_id: Mapped[str] = mapped_column(String(60), default="")
    contact: Mapped[str] = mapped_column(String(200), default="")
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)


class ServiceItem(Base):
    """Prestation vendable d'un module annexe (catalogue)."""

    __tablename__ = "service_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(40))  # restaurant | salle_reunion | parking
    price_ttc: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
