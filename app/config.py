"""Configuration technique de l'application (Pydantic Settings + fichier .env).

Les paramètres MÉTIER (nom de l'hôtel, TVA, taxe de séjour, imprimantes...) sont
stockés en base et éditables dans Administration > Paramètres : voir
``app/services/settings.py``.
"""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Literal["development", "production", "test"] = "production"
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)

    data_dir: Path = BASE_DIR / "data"
    database_url: str = ""
    secret_key: str = ""
    session_max_age: int = Field(default=12 * 3600, ge=300)
    cookie_secure: bool = False

    backup_dir: Path = BASE_DIR / "backups"
    backup_retention: int = Field(default=30, ge=1, description="Nombre de sauvegardes automatiques conservées")

    scheduler_enabled: bool = True
    scheduler_interval_seconds: int = Field(default=60, ge=5)

    login_max_attempts: int = Field(default=5, ge=1)
    login_lockout_seconds: int = Field(default=900, ge=10)

    log_level: str = "INFO"
    log_dir: Path = BASE_DIR / "logs"

    fonts_dir: Path = BASE_DIR / "app" / "static" / "fonts"

    @model_validator(mode="after")
    def _derive_defaults(self) -> "AppConfig":
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            self.database_url = f"sqlite+aiosqlite:///{(self.data_dir / 'hotel.db').as_posix()}"
        if not self.secret_key:
            key_file = self.data_dir / ".secret_key"
            if not key_file.exists():
                key_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
            self.secret_key = key_file.read_text(encoding="utf-8").strip()
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def sync_database_url(self) -> str:
        """URL synchrone (Alembic, sauvegardes)."""
        return self.database_url.replace("+aiosqlite", "")

    @property
    def database_path(self) -> Path | None:
        if not self.is_sqlite:
            return None
        raw = self.sync_database_url.split("sqlite:///", 1)[1]
        return Path(raw)


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()


config = get_config()
