"""Double sauvegarde automatique : archives ZIP sur disque local et sur Google Drive, historique, restauration.

Chaque sauvegarde produit une archive ``hotel-backup-AAAA-MM-JJ_HHMMSS.zip`` (base SQLite cohérente, médias,
manifeste) et une ligne ``BackupJob`` par destination. Toute entrée/sortie Google Drive s'exécute dans un thread
(``asyncio.to_thread``) et une erreur réseau ou d'authentification se traduit par un job en échec, jamais par une
exception qui remonterait jusqu'au planificateur.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path

from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
from google.auth.transport.requests import Request
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import config
from app.database import run_migrations
from app.models.backup import BACKUP_DESTINATIONS, BackupJob
from app.models.user import User
from app.services import backup
from app.services import theme_manager
from app.services.audit_log import log_action
from app.services.common import BusinessError
from app.services.settings import HotelSettings, now_of

logger = logging.getLogger(__name__)

ARCHIVE_PREFIX = "hotel-backup-"
ARCHIVE_TIME_FORMAT = "%Y-%m-%d_%H%M%S"
ARCHIVE_SUFFIX = ".zip"
DATABASE_NAME = "hotel.db"
MANIFEST_NAME = "manifest.json"
MEDIA_PREFIX = "data/media/"
TRIGGERS = {"manuelle": "Manuelle", "automatique": "Automatique", "avant_restauration": "Avant restauration"}
RETRY_DELAY = timedelta(hours=1)  # nouvel essai d'une sauvegarde automatique en échec au plus une fois par heure
CHUNK_SIZE = 4 * 1024 * 1024

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
SERVICE_ACCOUNT_FILE = "credentials.json"
USER_TOKEN_FILE = "token.json"
LAST_TEST_FILE = "last_test.json"
CREDENTIAL_KINDS = {"service_account": "Compte de service", "user": "Compte utilisateur (OAuth2)"}
MAX_CREDENTIALS_SIZE = 64 * 1024


# ---------------------------------------------------------------- dossiers et chemins
def local_root() -> Path:
    root = config.backup_dir / "local"
    root.mkdir(parents=True, exist_ok=True)
    return root


def work_dir(name: str) -> Path:
    folder = config.backup_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def google_dir() -> Path:
    return config.data_dir / "google"


def media_dir() -> Path:
    return config.data_dir / "media"


def inside_backup_dir(path: Path) -> bool:
    """Garde-fou : aucun chemin manipulé ne doit sortir de ``config.backup_dir``."""
    try:
        return path.resolve().is_relative_to(config.backup_dir.resolve())
    except OSError:
        return False


def archive_path(job: BackupJob) -> Path | None:
    """Chemin local de l'archive d'un job, s'il existe encore et se trouve bien sous le dossier des sauvegardes."""
    if not job.local_path:
        return None
    path = Path(job.local_path)
    if path.is_file() and inside_backup_dir(path):
        return path
    return None


def archive_timestamp(name: str) -> datetime | None:
    """Date encodée dans un nom d'archive ``hotel-backup-AAAA-MM-JJ_HHMMSS[-n].zip`` (None si autre nom)."""
    stem = name[:-len(ARCHIVE_SUFFIX)] if name.endswith(ARCHIVE_SUFFIX) else name
    if not stem.startswith(ARCHIVE_PREFIX):
        return None
    raw = stem[len(ARCHIVE_PREFIX):][:len("0000-00-00_000000")]
    try:
        return datetime.strptime(raw, ARCHIVE_TIME_FORMAT)
    except ValueError:
        return None


def normalize_folder_id(value: str) -> str:
    """Accepte un identifiant de dossier Drive ou l'URL complète du dossier."""
    value = (value or "").strip()
    if "/folders/" in value:
        value = value.split("/folders/", 1)[1]
    return value.split("?", 1)[0].split("#", 1)[0].strip("/ ")


def _prune_empty_day_dirs() -> None:
    for folder in local_root().iterdir():
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _alembic_revision(db_file: Path) -> str:
    try:
        connection = sqlite3.connect(db_file)
        try:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return ""
    return str(row[0]) if row else ""


def _remove_sqlite_files(db_file: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(db_file) + suffix).unlink(missing_ok=True)


# ---------------------------------------------------------------- archive ZIP
@dataclass
class ArchiveInfo:
    name: str
    path: Path
    created_at: datetime
    size: int
    checksum: str
    manifest: dict = field(default_factory=dict)


def build_archive(hotel_name: str, when: datetime, trigger: str = "manuelle") -> ArchiveInfo:
    """Archive ZIP horodatée dans ``backup_dir/local/AAAA-MM-JJ/`` : hotel.db, data/media/**, manifest.json."""
    day_dir = local_root() / when.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    base = f"{ARCHIVE_PREFIX}{when.strftime(ARCHIVE_TIME_FORMAT)}"
    target = day_dir / f"{base}{ARCHIVE_SUFFIX}"
    suffix = 1
    while target.exists():
        target = day_dir / f"{base}-{suffix}{ARCHIVE_SUFFIX}"
        suffix += 1
    snapshot = work_dir("_tmp") / f"{target.stem}.db"
    try:
        backup.snapshot_database(snapshot)
        manifest = {
            "created_at": when.isoformat(timespec="seconds"),
            "app_version": __version__,
            "alembic_revision": _alembic_revision(snapshot),
            "hotel_name": hotel_name,
            "trigger": trigger,
            "database_file": DATABASE_NAME,
            "database_sha256": _sha256(snapshot),
            "database_size": snapshot.stat().st_size,
            "media_files": 0,
        }
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, DATABASE_NAME)
            media = media_dir()
            if media.is_dir():
                for file in sorted(p for p in media.rglob("*") if p.is_file()):
                    archive.write(file, MEDIA_PREFIX + file.relative_to(media).as_posix())
                    manifest["media_files"] += 1
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        _remove_sqlite_files(snapshot)
    return ArchiveInfo(target.name, target, when, target.stat().st_size, _sha256(target), manifest)


def read_manifest(archive: Path) -> dict:
    try:
        with zipfile.ZipFile(archive) as zf:
            if MANIFEST_NAME not in zf.namelist():
                return {}
            data = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def extract_database(archive: Path, target_dir: Path) -> Path:
    """Extrait ``hotel.db`` d'une archive ZIP ; un fichier qui n'est pas un ZIP est traité comme une base brute."""
    if not zipfile.is_zipfile(archive):
        return archive
    with zipfile.ZipFile(archive) as zf:
        if DATABASE_NAME not in zf.namelist():
            raise BusinessError("L'archive ne contient pas de fichier hotel.db : ce n'est pas une sauvegarde de l'application.")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{archive.stem}-extrait.db"
        with zf.open(DATABASE_NAME) as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)
    return target


def extract_branding(archive: Path) -> list[str]:
    """Restaure les fichiers de marque (logo, favicon) de l'archive : la base restaurée y fait référence."""
    if not zipfile.is_zipfile(archive):
        return []
    with zipfile.ZipFile(archive) as zf:
        return theme_manager.restore_branding_files(zf, MEDIA_PREFIX)


def upload_target(filename: str) -> Path:
    """Chemin sûr (sous backup_dir/_uploads) pour un fichier importé par l'administrateur."""
    safe = "".join(ch for ch in Path(filename or "import.zip").name if ch.isalnum() or ch in "._-") or "import.zip"
    return work_dir("_uploads") / f"{datetime.now():%Y%m%d%H%M%S}_{safe}"


# ---------------------------------------------------------------- erreurs lisibles
def readable_error(exc: BaseException) -> str:
    """Message court et compréhensible pour l'historique et les messages à l'écran."""
    if isinstance(exc, BusinessError):
        message = str(exc)
    elif isinstance(exc, HttpError):
        reason = exc.reason or ""
        message = f"Google Drive a répondu {exc.status_code}" + (f" : {reason}" if reason else "")
        if exc.status_code == 404:
            message += " (dossier ou fichier introuvable : vérifiez l'identifiant du dossier et son partage)"
        elif exc.status_code in (401, 403):
            message += " (accès refusé : partagez le dossier avec le compte associé)"
    elif isinstance(exc, RefreshError):
        message = f"Authentification Google refusée (jeton expiré ou révoqué) : {exc}"
    elif isinstance(exc, (TransportError, ConnectionError, TimeoutError, OSError)):
        message = f"Erreur réseau : {exc}"
    elif isinstance(exc, GoogleAuthError):
        message = f"Erreur d'authentification Google : {exc}"
    else:
        message = f"{type(exc).__name__} : {exc}"
    return " ".join(message.split())[:500]


# ---------------------------------------------------------------- identifiants Google
def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # système de fichiers sans droits POSIX (Windows)
        pass


def has_credentials() -> bool:
    folder = google_dir()
    return (folder / SERVICE_ACCOUNT_FILE).is_file() or (folder / USER_TOKEN_FILE).is_file()


def credentials_info() -> dict:
    """État de l'association Google, sans jamais exposer les secrets : type, adresse, dernier test."""
    folder = google_dir()
    info = {"kind": "", "label": "Aucun compte associé", "email": "", "file": "",
            "last_test": _read_json(folder / LAST_TEST_FILE)}
    service_file, token_file = folder / SERVICE_ACCOUNT_FILE, folder / USER_TOKEN_FILE
    if service_file.is_file():
        data = _read_json(service_file)
        info.update(kind="service_account", label=CREDENTIAL_KINDS["service_account"],
                    email=str(data.get("client_email", "")), file=SERVICE_ACCOUNT_FILE)
    elif token_file.is_file():
        data = _read_json(token_file)
        info.update(kind="user", label=CREDENTIAL_KINDS["user"],
                    email=str(data.get("email") or data.get("account") or ""), file=USER_TOKEN_FILE)
    return info


def import_credentials(content: bytes) -> dict:
    """Valide strictement un fichier JSON Google puis l'installe dans ``data/google/``."""
    if not content:
        raise BusinessError("Fichier vide.")
    if len(content) > MAX_CREDENTIALS_SIZE:
        raise BusinessError("Fichier trop volumineux pour un fichier d'identification Google.")
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessError("Le fichier n'est pas un JSON valide.") from exc
    if not isinstance(data, dict):
        raise BusinessError("Le fichier JSON doit contenir un objet.")
    folder = google_dir()
    if data.get("type") == "service_account":
        missing = [key for key in ("client_email", "private_key", "token_uri") if not data.get(key)]
        if missing:
            raise BusinessError("Fichier de compte de service incomplet (champs manquants : " + ", ".join(missing) + ").")
        target, other = folder / SERVICE_ACCOUNT_FILE, folder / USER_TOKEN_FILE
    elif all(isinstance(data.get(key), str) and data.get(key) for key in ("refresh_token", "client_id", "client_secret")):
        data.setdefault("type", "authorized_user")
        target, other = folder / USER_TOKEN_FILE, folder / SERVICE_ACCOUNT_FILE
    else:
        raise BusinessError("Fichier non reconnu : attendu un compte de service Google (type « service_account ») "
                            "ou un jeton utilisateur produit par scripts/google_drive_auth.py "
                            "(refresh_token, client_id, client_secret).")
    previous = _read_json(target) if target.is_file() else None
    _write_private(target, data)
    try:
        load_credentials()
    except BusinessError:
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            _write_private(target, previous)
        raise
    other.unlink(missing_ok=True)
    (folder / LAST_TEST_FILE).unlink(missing_ok=True)
    return credentials_info()


def unlink_credentials() -> None:
    folder = google_dir()
    for name in (SERVICE_ACCOUNT_FILE, USER_TOKEN_FILE, LAST_TEST_FILE):
        (folder / name).unlink(missing_ok=True)


def refresh_credentials(credentials) -> None:
    """Rafraîchissement OAuth2 (appel réseau, isolé pour les tests)."""
    credentials.refresh(Request())


def load_credentials():
    """Identifiants Google : compte de service, sinon jeton utilisateur (rafraîchi et réécrit s'il a expiré)."""
    folder = google_dir()
    service_file = folder / SERVICE_ACCOUNT_FILE
    if service_file.is_file():
        from google.oauth2 import service_account

        try:
            return service_account.Credentials.from_service_account_file(str(service_file), scopes=DRIVE_SCOPES)
        except (ValueError, KeyError, TypeError) as exc:
            raise BusinessError(f"Fichier de compte de service invalide : {exc}") from exc
    token_file = folder / USER_TOKEN_FILE
    if token_file.is_file():
        from google.oauth2.credentials import Credentials

        try:
            credentials = Credentials.from_authorized_user_info(_read_json(token_file), DRIVE_SCOPES)
        except ValueError as exc:
            raise BusinessError(f"Jeton utilisateur invalide : {exc}") from exc
        if not credentials.valid:
            if not credentials.refresh_token:
                raise BusinessError("Le jeton utilisateur a expiré et ne peut pas être renouvelé : relancez "
                                    "scripts/google_drive_auth.py.")
            try:
                refresh_credentials(credentials)
            except (GoogleAuthError, ConnectionError, TimeoutError, OSError) as exc:
                raise BusinessError(readable_error(exc)) from exc
            data = json.loads(credentials.to_json())
            previous = _read_json(token_file)
            for key in ("email", "account"):
                if previous.get(key) and not data.get(key):
                    data[key] = previous[key]
            _write_private(token_file, data)
        return credentials
    raise BusinessError("Aucun compte Google associé : importez un fichier credentials.json (compte de service) "
                        "ou un token.json produit par scripts/google_drive_auth.py.")


# ---------------------------------------------------------------- client Google Drive (synchrone, exécuté en thread)
def build_drive_service(credentials):
    """Construit le client Drive v3 ; isolé pour pouvoir être remplacé dans les tests."""
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _service():
    return build_drive_service(load_credentials())


def upload_to_drive(path: Path, folder_id: str = "") -> dict:
    """Envoi reprenable de l'archive dans le dossier Drive (vide = racine) ; retourne id, size, webViewLink."""
    service = _service()
    body = {"name": path.name, "mimeType": "application/zip"}
    folder_id = normalize_folder_id(folder_id)
    if folder_id:
        body["parents"] = [folder_id]
    media = MediaFileUpload(str(path), mimetype="application/zip", resumable=True, chunksize=CHUNK_SIZE)
    response = service.files().create(body=body, media_body=media, fields="id,size,webViewLink",
                                      supportsAllDrives=True).execute()
    if not isinstance(response, dict) or not response.get("id"):
        raise BusinessError("Google Drive n'a pas confirmé l'envoi du fichier.")
    return response


def download_from_drive(file_id: str, target: Path) -> Path:
    service = _service()
    target.parent.mkdir(parents=True, exist_ok=True)
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with target.open("wb") as handle:
        downloader = MediaIoBaseDownload(handle, request, chunksize=CHUNK_SIZE)
        done = False
        while not done:
            _progress, done = downloader.next_chunk()
    return target


def delete_from_drive(file_id: str, service=None) -> None:
    service = service or _service()
    try:
        service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
    except HttpError as exc:
        if exc.status_code != 404:  # déjà supprimé côté Drive : l'objectif est atteint
            raise


def list_drive_backups(folder_id: str = "", service=None) -> list[dict]:
    service = service or _service()
    query = f"name contains '{ARCHIVE_PREFIX}' and trashed = false"
    folder_id = normalize_folder_id(folder_id)
    if folder_id:
        query += f" and '{folder_id}' in parents"
    files: list[dict] = []
    page_token = None
    while True:
        response = service.files().list(q=query, fields="nextPageToken, files(id,name,createdTime,size,webViewLink)",
                                        pageSize=200, pageToken=page_token, supportsAllDrives=True,
                                        includeItemsFromAllDrives=True).execute()
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return files


def purge_drive(folder_id: str, cutoff: datetime, keep: set[str] | None = None) -> list[str]:
    """Supprime sur Drive les archives ``hotel-backup-*`` antérieures à ``cutoff`` ; retourne les identifiants."""
    service = _service()
    removed = []
    for file in list_drive_backups(folder_id, service):
        name = str(file.get("name", ""))
        if keep and name in keep:
            continue
        when = archive_timestamp(name)
        if when is None:
            created = str(file.get("createdTime", ""))[:19]
            try:
                when = datetime.strptime(created, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                continue
        if when < cutoff:
            delete_from_drive(str(file["id"]), service)
            removed.append(str(file["id"]))
    return removed


def test_connection(folder_id: str = "") -> dict:
    """Vérifie l'authentification et l'accès au dossier ; mémorise le résultat (date, succès, détail)."""
    result = {"at": datetime.now().isoformat(timespec="seconds"), "ok": False, "detail": ""}
    try:
        service = _service()
        about = service.about().get(fields="user(emailAddress,displayName)").execute()
        email = (about.get("user") or {}).get("emailAddress", "") if isinstance(about, dict) else ""
        details = [f"Connecté en tant que {email}" if email else "Connexion établie"]
        folder_id = normalize_folder_id(folder_id)
        if folder_id:
            folder = service.files().get(fileId=folder_id, fields="id,name", supportsAllDrives=True).execute()
            details.append(f"dossier « {folder.get('name', folder_id)} » accessible")
        else:
            details.append("destination : racine du Drive")
        result.update(ok=True, detail=" · ".join(details))
    except Exception as exc:  # toute erreur (réseau, authentification, droits) est rapportée, jamais propagée
        result["detail"] = readable_error(exc)
    _write_private(google_dir() / LAST_TEST_FILE, result)
    return result


# ---------------------------------------------------------------- exécution d'une sauvegarde
def _enabled_destinations(hs: HotelSettings) -> list[str]:
    return [name for name, enabled in (("local", hs.backup_local_enabled), ("drive", hs.backup_drive_enabled)) if enabled]


async def run_backup(db: AsyncSession, hs: HotelSettings, *, trigger: str = "manuelle",
                     destinations=("local",), user: User | None = None, when: datetime | None = None) -> list[BackupJob]:
    """Crée l'archive puis l'envoie vers chaque destination ; une ligne BackupJob par destination."""
    wanted = [name for name in BACKUP_DESTINATIONS if name in destinations]
    if not wanted:
        raise BusinessError("Choisissez au moins une destination de sauvegarde.")
    if trigger not in TRIGGERS:
        raise BusinessError("Déclencheur de sauvegarde inconnu.")
    when = when or now_of(hs)
    try:
        archive = await asyncio.to_thread(build_archive, hs.hotel_name, when, trigger)
    except (OSError, sqlite3.Error) as exc:
        raise BusinessError(f"Impossible de créer l'archive de sauvegarde : {exc}") from exc
    jobs = [BackupJob(created_at=when, destination=name, trigger=trigger, status="running", archive_name=archive.name,
                      size_bytes=archive.size, checksum=archive.checksum, user_id=user.id if user else None)
            for name in wanted]
    db.add_all(jobs)
    await db.flush()
    for job in jobs:
        try:
            if job.destination == "local":
                job.local_path = str(archive.path)
            else:
                remote = await asyncio.to_thread(upload_to_drive, archive.path, hs.backup_drive_folder_id)
                job.remote_id = str(remote.get("id", ""))
                job.remote_link = str(remote.get("webViewLink", ""))
                job.size_bytes = int(remote.get("size") or archive.size)
            job.status, job.error = "success", ""
        except Exception as exc:  # réseau, authentification, quota… : le job échoue, le reste continue
            logger.warning("Sauvegarde %s vers %s en échec : %s", archive.name, job.destination, exc)
            job.status, job.error = "failed", readable_error(exc)
        job.finished_at = now_of(hs)
    if "local" not in wanted:
        archive.path.unlink(missing_ok=True)
        await asyncio.to_thread(_prune_empty_day_dirs)
    await db.flush()
    try:
        await apply_retention(db, hs, when, protect={archive.name})
    except Exception:  # la rétention ne doit jamais faire échouer la sauvegarde qui vient d'être faite
        logger.exception("Erreur pendant l'application de la rétention des sauvegardes")
    return jobs


async def due_destinations(db: AsyncSession, hs: HotelSettings, now: datetime) -> list[str]:
    """Destinations activées sans sauvegarde automatique réussie aujourd'hui (réessai au plus toutes les heures)."""
    enabled = _enabled_destinations(hs)
    if not enabled:
        return []
    start_of_day = datetime.combine(now.date(), time.min)
    rows = (await db.scalars(select(BackupJob).where(
        BackupJob.trigger == "automatique", BackupJob.created_at >= start_of_day,
        BackupJob.destination.in_(enabled)))).all()
    due = []
    for destination in enabled:
        attempts = [job for job in rows if job.destination == destination]
        if any(job.status in ("success", "deleted") for job in attempts):
            continue
        latest = max((job.created_at for job in attempts), default=None)
        if latest is not None and now - latest < RETRY_DELAY:
            continue
        due.append(destination)
    return due


# ---------------------------------------------------------------- rétention
async def _protected_archives(db: AsyncSession) -> set[str]:
    """Archives jamais purgées : la sauvegarde de sécurité la plus récente et la dernière réussie par destination."""
    protected: set[str] = set()
    safety = await db.scalar(select(BackupJob.archive_name).where(
        BackupJob.trigger == "avant_restauration", BackupJob.status == "success")
        .order_by(BackupJob.created_at.desc(), BackupJob.id.desc()).limit(1))
    if safety:
        protected.add(safety)
    for destination in BACKUP_DESTINATIONS:
        latest = await db.scalar(select(BackupJob.archive_name).where(
            BackupJob.destination == destination, BackupJob.status == "success")
            .order_by(BackupJob.created_at.desc(), BackupJob.id.desc()).limit(1))
        if latest:
            protected.add(latest)
    return protected


async def apply_retention(db: AsyncSession, hs: HotelSettings, now: datetime | None = None,
                          protect: set[str] | None = None) -> dict[str, int]:
    """Supprime archives locales et fichiers Drive plus anciens que ``backup_retention_days`` (jobs → deleted)."""
    now = now or now_of(hs)
    cutoff = now - timedelta(days=hs.backup_retention_days)
    protected = (await _protected_archives(db)) | (protect or set())
    removed = {"local": 0, "drive": 0}
    seen_remote: set[str] = set()
    stmt = select(BackupJob).where(BackupJob.status == "success", BackupJob.created_at < cutoff).order_by(BackupJob.created_at)
    for job in (await db.scalars(stmt)).all():
        if job.archive_name in protected:
            continue
        try:
            if job.destination == "local":
                path = archive_path(job)
                if path is not None:
                    await asyncio.to_thread(path.unlink)
            elif job.remote_id:
                await asyncio.to_thread(delete_from_drive, job.remote_id)
                seen_remote.add(job.remote_id)
            job.status, job.error = "deleted", ""
            removed[job.destination] += 1
        except Exception as exc:  # on réessaiera au prochain passage ; le job garde son statut
            logger.warning("Purge de la sauvegarde %s (%s) impossible : %s", job.archive_name, job.destination, exc)
            job.error = readable_error(exc)
    if hs.backup_drive_enabled and has_credentials():
        try:
            ids = await asyncio.to_thread(purge_drive, hs.backup_drive_folder_id, cutoff, protected)
            removed["drive"] += len(set(ids) - seen_remote)
        except Exception as exc:
            logger.warning("Purge Google Drive impossible : %s", exc)
    await asyncio.to_thread(_prune_empty_day_dirs)
    await db.flush()
    return removed


# ---------------------------------------------------------------- restauration
async def _locate_archive(db: AsyncSession, job: BackupJob) -> Path:
    """Archive locale du job, sinon celle d'un job frère, sinon téléchargement depuis Google Drive."""
    path = archive_path(job)
    if path is not None:
        return path
    siblings = (await db.scalars(select(BackupJob).where(
        BackupJob.archive_name == job.archive_name, BackupJob.id != job.id))).all()
    for other in siblings:
        path = archive_path(other)
        if path is not None:
            return path
    remote_id = job.remote_id if job.status == "success" else ""
    if not remote_id:
        remote_id = next((o.remote_id for o in siblings if o.remote_id and o.status == "success"), "")
    if not remote_id:
        raise BusinessError("L'archive locale est introuvable et aucune copie Google Drive n'est connue.")
    target = work_dir("_restore") / job.archive_name
    try:
        return await asyncio.to_thread(download_from_drive, remote_id, target)
    except BusinessError:
        raise
    except Exception as exc:
        raise BusinessError(f"Téléchargement depuis Google Drive impossible : {readable_error(exc)}") from exc


async def download_path(db: AsyncSession, job_id: int) -> tuple[Path, str]:
    """Fichier à servir pour « Télécharger » (local, ou rapatrié depuis Drive) et son nom."""
    job = await db.get(BackupJob, job_id)
    if job is None or job.status != "success":
        raise BusinessError("Sauvegarde introuvable.")
    return await _locate_archive(db, job), job.archive_name


async def _history_rows(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(select(BackupJob.__table__))).mappings().all()
    return [dict(row) for row in rows]


async def _merge_history(db: AsyncSession, rows: list[dict]) -> int:
    """Réinjecte dans la base restaurée les lignes d'historique qu'elle ne contient pas (postérieures à l'archive)."""
    existing = set((await db.scalars(select(BackupJob.id))).all())
    users = set((await db.scalars(select(User.id))).all())
    added = 0
    for row in rows:
        if row["id"] in existing:
            continue
        data = dict(row)
        if data.get("user_id") not in users:
            data["user_id"] = None
        db.add(BackupJob(**data))
        added += 1
    await db.flush()
    return added


async def _log_user(db: AsyncSession, user: User | None) -> User | None:
    """Après restauration, l'utilisateur courant peut ne plus exister : on journalise alors « système »."""
    if user is None:
        return None
    exists = await db.scalar(select(User.id).where(User.id == user.id))
    return user if exists else None


async def restore_archive(db: AsyncSession, archive: Path, hs: HotelSettings, user: User | None = None,
                          source: str = "") -> dict:
    """Validation, sauvegarde de sécurité locale, remplacement de la base, migrations, historique conservé."""
    if not inside_backup_dir(archive):
        raise BusinessError("Chemin de restauration invalide.")
    db_file = await asyncio.to_thread(extract_database, archive, work_dir("_restore"))
    try:
        await asyncio.to_thread(backup.validate_database_file, db_file)
        manifest = read_manifest(archive) if db_file != archive else {}
        when = now_of(hs)
        safety = await asyncio.to_thread(build_archive, hs.hotel_name, when, "avant_restauration")
        history = await _history_rows(db)
        await db.commit()  # aucune transaction ouverte pendant l'écrasement de la base
        db.expunge_all()
        await asyncio.to_thread(backup.write_database, db_file)
        await asyncio.to_thread(extract_branding, archive)
    finally:
        if db_file != archive:
            _remove_sqlite_files(db_file)
    migration_error = ""
    try:
        await asyncio.to_thread(run_migrations)
    except Exception as exc:  # base plus récente que le code, ou schéma inattendu : signalé, pas masqué
        logger.exception("Migrations après restauration en échec")
        migration_error = readable_error(exc)
    await _merge_history(db, history)
    branding_note = ""
    if not migration_error:
        branding = await theme_manager.reconcile_branding(db)
        if branding["cleared"]:
            branding_note = " ; fichiers de marque absents de l'archive : " + ", ".join(branding["cleared"])
    safety_job = BackupJob(created_at=when, finished_at=when, destination="local", trigger="avant_restauration",
                           status="success", archive_name=safety.name, local_path=str(safety.path),
                           size_bytes=safety.size, checksum=safety.checksum)
    db.add(safety_job)
    await db.flush()
    actor = await _log_user(db, user)
    details = f"depuis {source or archive.name}"
    if manifest.get("created_at"):
        details += f" (archive du {manifest['created_at']}, version {manifest.get('app_version', '?')})"
    details += f" ; sauvegarde de sécurité {safety.name}"
    details += branding_note
    if migration_error:
        details += f" ; migrations : {migration_error}"
    await log_action(db, actor, "backup_restored", "backup", safety_job.id, details, when)
    return {"safety": safety_job, "manifest": manifest, "migration_error": migration_error, "source": source}


async def restore_job(db: AsyncSession, job_id: int, hs: HotelSettings, user: User | None = None) -> dict:
    """Restauration en un clic depuis l'historique (archive locale ou téléchargée depuis Drive)."""
    job = await db.get(BackupJob, job_id)
    if job is None:
        raise BusinessError("Sauvegarde introuvable.")
    if job.status != "success":
        raise BusinessError(f"Cette sauvegarde n'est pas restaurable (statut : {job.status_label}).")
    source = f"sauvegarde n° {job.id} – {job.archive_name} ({job.destination_label})"
    archive = await _locate_archive(db, job)
    try:
        return await restore_archive(db, archive, hs, user, source)
    finally:
        if archive.parent == config.backup_dir / "_restore":
            archive.unlink(missing_ok=True)


async def restore_upload(db: AsyncSession, path: Path, filename: str, hs: HotelSettings,
                         user: User | None = None) -> dict:
    """Restauration depuis un fichier importé (.zip produit par l'application ou base .db brute)."""
    try:
        return await restore_archive(db, path, hs, user, f"fichier importé {filename}")
    finally:
        path.unlink(missing_ok=True)


async def delete_job(db: AsyncSession, job_id: int) -> BackupJob:
    """Supprime l'archive (locale ou Drive) d'un job et passe la ligne en statut « supprimée »."""
    job = await db.get(BackupJob, job_id)
    if job is None:
        raise BusinessError("Sauvegarde introuvable.")
    if job.status == "deleted":
        raise BusinessError("Cette sauvegarde est déjà supprimée.")
    if job.destination == "local":
        path = archive_path(job)
        if path is not None:
            await asyncio.to_thread(path.unlink)
            await asyncio.to_thread(_prune_empty_day_dirs)
    elif job.remote_id and job.status == "success":
        try:
            await asyncio.to_thread(delete_from_drive, job.remote_id)
        except BusinessError:
            raise
        except Exception as exc:
            raise BusinessError(f"Suppression sur Google Drive impossible : {readable_error(exc)}") from exc
    job.status, job.error = "deleted", ""
    return job


async def recent_jobs(db: AsyncSession, limit: int = 200) -> list[BackupJob]:
    stmt = select(BackupJob).order_by(BackupJob.created_at.desc(), BackupJob.id.desc()).limit(limit)
    return list((await db.scalars(stmt)).all())
