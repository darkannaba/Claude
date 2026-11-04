"""Sauvegarde et restauration de la base SQLite (API de sauvegarde en ligne, compatible WAL)."""
from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.config import config
from app.services.common import BusinessError

REQUIRED_TABLES = {"reservations", "guests", "rooms", "users", "folio_items", "alembic_version"}


@dataclass
class BackupInfo:
    name: str
    path: Path
    created_at: datetime
    size: int
    reason: str

    @property
    def size_label(self) -> str:
        size = float(self.size)
        for unit in ("o", "Ko", "Mo", "Go"):
            if size < 1024 or unit == "Go":
                return f"{size:.0f} {unit}" if unit == "o" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} Go"


def _db_path() -> Path:
    path = config.database_path
    if path is None:
        raise BusinessError("La sauvegarde intégrée ne concerne que les bases SQLite.")
    return path


def backup_root() -> Path:
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    return config.backup_dir


def create_backup(reason: str = "manuelle", when: datetime | None = None) -> BackupInfo:
    """Copie cohérente de la base dans backups/AAAA-MM-JJ_HHMMSS/hotel.db (+ meta.json)."""
    when = when or datetime.now()
    base_name = when.strftime("%Y-%m-%d_%H%M%S")
    folder = backup_root() / base_name
    suffix = 1
    while folder.exists():
        folder = backup_root() / f"{base_name}-{suffix}"
        suffix += 1
    folder.mkdir(parents=True)
    target = folder / "hotel.db"
    source = sqlite3.connect(_db_path())
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    (folder / "meta.json").write_text(
        json.dumps({"created_at": when.isoformat(timespec="seconds"), "reason": reason}, ensure_ascii=False),
        encoding="utf-8")
    return _info(folder)


def _info(folder: Path) -> BackupInfo:
    meta = {}
    meta_file = folder / "meta.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    db_file = folder / "hotel.db"
    created = meta.get("created_at")
    created_at = datetime.fromisoformat(created) if created else datetime.fromtimestamp(db_file.stat().st_mtime)
    return BackupInfo(folder.name, db_file, created_at, db_file.stat().st_size, meta.get("reason", "manuelle"))


def list_backups() -> list[BackupInfo]:
    items = [_info(f) for f in backup_root().iterdir() if f.is_dir() and (f / "hotel.db").exists()]
    return sorted(items, key=lambda b: b.created_at, reverse=True)


def get_backup(name: str) -> BackupInfo:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise BusinessError("Nom de sauvegarde invalide.")
    folder = backup_root() / name
    if not (folder / "hotel.db").exists():
        raise BusinessError("Sauvegarde introuvable.")
    return _info(folder)


def delete_backup(name: str) -> None:
    shutil.rmtree(get_backup(name).path.parent)


def prune_backups(keep: int, reason: str = "automatique") -> int:
    """Supprime les sauvegardes automatiques les plus anciennes au-delà de ``keep``."""
    automatic = [b for b in list_backups() if b.reason == reason]
    removed = 0
    for info in automatic[keep:]:
        shutil.rmtree(info.path.parent)
        removed += 1
    return removed


def has_backup_on(day, reason: str = "automatique") -> bool:
    return any(b.created_at.date() == day and b.reason == reason for b in list_backups())


def validate_database_file(path: Path) -> None:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BusinessError("Le fichier de sauvegarde est corrompu (contrôle d'intégrité en échec).")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise BusinessError("Ce fichier n'est pas une base de données SQLite valide.") from exc
    missing = REQUIRED_TABLES - tables
    if missing:
        raise BusinessError("Ce fichier n'est pas une sauvegarde de l'application (tables manquantes : "
                            + ", ".join(sorted(missing)) + ").")


def restore_from_file(path: Path) -> BackupInfo:
    """Restaure la base depuis un fichier validé ; une sauvegarde de sécurité est faite avant."""
    validate_database_file(path)
    safety = create_backup("avant restauration")
    source = sqlite3.connect(path)
    destination = sqlite3.connect(_db_path())
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return safety


def restore_backup(name: str) -> BackupInfo:
    return restore_from_file(get_backup(name).path)


def store_upload(content: bytes, filename: str) -> Path:
    if not content:
        raise BusinessError("Fichier vide.")
    folder = backup_root() / "_uploads"
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in Path(filename or "upload.db").name if ch.isalnum() or ch in "._-") or "upload.db"
    target = folder / f"{datetime.now():%Y%m%d%H%M%S}_{safe}"
    target.write_bytes(content)
    return target
