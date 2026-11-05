"""Double sauvegarde (disque local + Google Drive) : service backup_manager, routeur /admin/backups, planificateur.

Aucun test n'accède au réseau : Google Drive est simulé en remplaçant ``build_drive_service`` (et le
téléchargeur ``MediaIoBaseDownload``) ; les identifiants sont des fichiers JSON factices.
"""
import asyncio
import hashlib
import html
import io
import json
import re
import sqlite3
import zipfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httplib2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from sqlalchemy import delete, select

from app import __version__
from app.config import config
from app.database import SessionLocal
from app.models import AuditLog, BackupJob
from app.services import backup, backup_manager, scheduler
from app.services.common import BusinessError
from app.services.settings import load_hotel_settings, save_settings_subset, today_of

SERVICE_EMAIL = "sauvegarde@hotel.iam.gserviceaccount.com"
USER_EMAIL = "hotel@example.com"


# ---------------------------------------------------------------- simulacre Google Drive
def _http_error(status: int, reason: str, message: str) -> HttpError:
    return HttpError(httplib2.Response({"status": status, "reason": reason}),
                     json.dumps({"error": {"message": message}}).encode(), uri="https://www.googleapis.com/drive/v3")


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _Files:
    def __init__(self, drive):
        self.drive = drive

    def create(self, body, media_body, fields, **_kwargs):
        def run():
            if self.drive.fail is not None:
                raise self.drive.fail
            content = media_body.getbytes(0, media_body.size())
            file_id = self.drive.add_file(body["name"], content, body.get("parents", []))
            return {"id": file_id, "size": str(len(content)), "webViewLink": f"https://drive.example/{file_id}"}
        return _Call(run)

    def list(self, q, fields, pageSize, pageToken=None, **_kwargs):
        def run():
            files = [{k: v for k, v in f.items() if k != "content"} for f in self.drive.store.values()
                     if backup_manager.ARCHIVE_PREFIX in f["name"]]
            return {"files": files}
        return _Call(run)

    def get(self, fileId, fields, **_kwargs):
        def run():
            if fileId == "dossier-ok":
                return {"id": fileId, "name": "Sauvegardes hôtel"}
            if fileId in self.drive.store:
                return {"id": fileId, "name": self.drive.store[fileId]["name"]}
            raise _http_error(404, "Not Found", "File not found")
        return _Call(run)

    def get_media(self, fileId, **_kwargs):
        if fileId not in self.drive.store:
            raise _http_error(404, "Not Found", "File not found")
        return SimpleNamespace(content=self.drive.store[fileId]["content"])

    def delete(self, fileId, **_kwargs):
        def run():
            if fileId not in self.drive.store:
                raise _http_error(404, "Not Found", "File not found")
            del self.drive.store[fileId]
            return ""
        return _Call(run)


class _About:
    def __init__(self, drive):
        self.drive = drive

    def get(self, fields):
        def run():
            if self.drive.fail is not None:
                raise self.drive.fail
            return {"user": {"emailAddress": SERVICE_EMAIL}}
        return _Call(run)


class FakeDrive:
    """API Drive v3 minimale : files().create/list/get/get_media/delete et about().get, en mémoire."""

    def __init__(self):
        self.store: dict[str, dict] = {}
        self.fail = None
        self.counter = 0

    def add_file(self, name: str, content: bytes, parents=None) -> str:
        self.counter += 1
        file_id = f"drive-{self.counter}"
        self.store[file_id] = {"id": file_id, "name": name, "content": content, "parents": list(parents or []),
                               "createdTime": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                               "webViewLink": f"https://drive.example/{file_id}"}
        return file_id

    def files(self):
        return _Files(self)

    def about(self):
        return _About(self)


class FakeDownloader:
    def __init__(self, handle, request, chunksize=None):
        handle.write(request.content)

    def next_chunk(self):
        return None, True


@pytest.fixture()
def drive(monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(backup_manager, "load_credentials", lambda: object())
    monkeypatch.setattr(backup_manager, "build_drive_service", lambda credentials: fake)
    monkeypatch.setattr(backup_manager, "MediaIoBaseDownload", FakeDownloader)
    monkeypatch.setattr(backup_manager, "has_credentials", lambda: True)
    return fake


def service_account_json() -> dict:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return {"type": "service_account", "project_id": "hotel-annaba", "private_key_id": "k1", "private_key": pem,
            "client_email": SERVICE_EMAIL, "client_id": "1234567890", "token_uri": "https://oauth2.googleapis.com/token"}


def user_token_json(expired: bool = True) -> dict:
    expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=-1 if expired else 1)
    return {"type": "authorized_user", "client_id": "abc.apps.googleusercontent.com", "client_secret": "secret-123",
            "refresh_token": "refresh-xyz", "token": "jeton-ancien", "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": backup_manager.DRIVE_SCOPES, "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"), "email": USER_EMAIL}


def run(coro):
    return asyncio.run(coro)


async def _settings():
    async with SessionLocal() as db:
        return await load_hotel_settings(db)


# ---------------------------------------------------------------- archive
def test_build_archive_contents_manifest_and_checksum(app, tmp_dir):
    media = backup_manager.media_dir() / "photos"
    media.mkdir(parents=True, exist_ok=True)
    (media / "chambre-101.jpg").write_bytes(b"\xff\xd8image-factice")
    when = datetime(2026, 1, 2, 3, 4, 5)
    info = backup_manager.build_archive("Hôtel Test", when, "manuelle")
    assert info.path == config.backup_dir / "local" / "2026-01-02" / "hotel-backup-2026-01-02_030405.zip"
    assert info.name == info.path.name and info.size == info.path.stat().st_size > 0
    assert info.checksum == hashlib.sha256(info.path.read_bytes()).hexdigest()
    with zipfile.ZipFile(info.path) as archive:
        assert {"hotel.db", "manifest.json", "data/media/photos/chambre-101.jpg"} <= set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        database = archive.read("hotel.db")
        assert archive.read("data/media/photos/chambre-101.jpg") == b"\xff\xd8image-factice"
    assert manifest["hotel_name"] == "Hôtel Test" and manifest["app_version"] == __version__
    assert manifest["alembic_revision"] and manifest["media_files"] == 1 and manifest["trigger"] == "manuelle"
    assert manifest["created_at"] == "2026-01-02T03:04:05"
    assert manifest["database_sha256"] == hashlib.sha256(database).hexdigest()
    assert manifest["database_size"] == len(database)
    extracted = tmp_dir / "extrait.db"
    extracted.write_bytes(database)
    backup.validate_database_file(extracted)  # instantané lisible, complet (toutes les tables attendues)
    assert backup_manager.read_manifest(info.path) == manifest
    assert backup_manager.read_manifest(extracted) == {}
    # Même seconde : suffixe numérique, jamais d'écrasement
    second = backup_manager.build_archive("Hôtel Test", when)
    assert second.path.name == "hotel-backup-2026-01-02_030405-1.zip"
    assert backup_manager.archive_timestamp(second.name) == when
    assert backup_manager.archive_timestamp("autre-fichier.zip") is None
    assert backup_manager.archive_timestamp("hotel-backup-pas-une-date.zip") is None
    assert not list(backup_manager.work_dir("_tmp").iterdir())  # aucun instantané résiduel
    for path in (info.path, second.path):
        path.unlink()
    backup_manager._prune_empty_day_dirs()
    assert not (config.backup_dir / "local" / "2026-01-02").exists()


def test_helpers_paths_and_errors():
    assert backup_manager.normalize_folder_id(" https://drive.google.com/drive/folders/ABC_123?usp=sharing ") == "ABC_123"
    assert backup_manager.normalize_folder_id("ABC_123") == "ABC_123" and backup_manager.normalize_folder_id("") == ""
    assert backup_manager.inside_backup_dir(config.backup_dir / "local" / "x.zip")
    assert not backup_manager.inside_backup_dir(Path("/etc/passwd"))
    target = backup_manager.upload_target("../../evil name.zip")
    assert target.parent == config.backup_dir / "_uploads" and ".." not in target.name and " " not in target.name
    assert backup_manager.readable_error(BusinessError("Règle métier")) == "Règle métier"
    assert backup_manager.readable_error(ConnectionError("coupure")).startswith("Erreur réseau")
    assert "403" in backup_manager.readable_error(_http_error(403, "Forbidden", "insufficientPermissions"))
    assert "404" in backup_manager.readable_error(_http_error(404, "Not Found", "File not found"))
    assert "refusée" in backup_manager.readable_error(RefreshError("invalid_grant"))
    assert backup_manager.readable_error(ValueError("x")).startswith("ValueError")
    assert set(backup_manager.credentials_info()) == {"kind", "label", "email", "file", "last_test"}


# ---------------------------------------------------------------- sauvegarde locale et rétention
async def test_run_backup_local_and_retention(db):
    hs = await load_hotel_settings(db)
    keep_all = hs.model_copy(update={"backup_retention_days": 3650})
    now = datetime.now().replace(microsecond=0)
    old_when = now - timedelta(days=hs.backup_retention_days + 3)
    with pytest.raises(BusinessError):
        await backup_manager.run_backup(db, keep_all, destinations=[])
    with pytest.raises(BusinessError):
        await backup_manager.run_backup(db, keep_all, trigger="inconnu", destinations=["local"])
    (old,) = await backup_manager.run_backup(db, keep_all, destinations=["local"], when=old_when)
    (old_safety,) = await backup_manager.run_backup(db, keep_all, trigger="avant_restauration", destinations=["local"],
                                                    when=old_when - timedelta(days=1))
    (recent,) = await backup_manager.run_backup(db, keep_all, destinations=["local"], when=now)
    await db.commit()
    for job in (old, old_safety, recent):
        assert job.status == "success" and job.finished_at is not None and Path(job.local_path).is_file()
        assert job.checksum == hashlib.sha256(Path(job.local_path).read_bytes()).hexdigest()
        assert job.size_bytes == Path(job.local_path).stat().st_size and job.error == ""
    assert Path(old.local_path).parent.name == old_when.strftime("%Y-%m-%d")
    assert recent.size_label.endswith(("Ko", "Mo"))
    old_path, old_day = Path(old.local_path), Path(old.local_path).parent

    removed = await backup_manager.apply_retention(db, hs, now)
    await db.commit()
    assert removed["local"] >= 1
    assert old.status == "deleted" and not old_path.exists() and not old_day.exists()
    # Jamais purgées : la sauvegarde de sécurité la plus récente et la dernière sauvegarde réussie
    assert old_safety.status == "success" and Path(old_safety.local_path).is_file()
    assert recent.status == "success" and Path(recent.local_path).is_file()
    # Téléchargement : chemin sûr sous backup_dir ; job supprimé introuvable
    path, name = await backup_manager.download_path(db, recent.id)
    assert path == Path(recent.local_path) and name == recent.archive_name
    with pytest.raises(BusinessError):
        await backup_manager.download_path(db, old.id)
    # Suppression manuelle
    deleted = await backup_manager.delete_job(db, recent.id)
    await db.commit()
    assert deleted.status == "deleted" and not Path(recent.local_path).exists()
    with pytest.raises(BusinessError, match="déjà supprimée"):
        await backup_manager.delete_job(db, recent.id)
    with pytest.raises(BusinessError, match="introuvable"):
        await backup_manager.delete_job(db, 999999)
    assert deleted in await backup_manager.recent_jobs(db)


# ---------------------------------------------------------------- restauration aller-retour
async def test_restore_roundtrip_keeps_history(db, tmp_dir):
    hs = await load_hotel_settings(db)
    original_name = hs.hotel_name
    (job,) = await backup_manager.run_backup(db, hs, destinations=["local"])
    await db.commit()
    job_id, archive_name = job.id, job.archive_name
    await save_settings_subset(db, {"hotel_name": "Hôtel Modifié"})
    await db.commit()
    assert (await load_hotel_settings(db)).hotel_name == "Hôtel Modifié"

    result = await backup_manager.restore_job(db, job_id, hs, None)
    await db.commit()
    assert (await load_hotel_settings(db)).hotel_name == original_name
    safety = result["safety"]
    assert safety.trigger == "avant_restauration" and safety.status == "success" and Path(safety.local_path).is_file()
    assert result["migration_error"] == "" and result["manifest"]["app_version"] == __version__
    # L'historique postérieur à l'instantané (dont la ligne de l'archive restaurée) est conservé
    restored_job = await db.get(BackupJob, job_id)
    assert restored_job is not None and restored_job.archive_name == archive_name
    assert await db.scalar(select(BackupJob.id).where(BackupJob.id == safety.id))
    assert await db.scalar(select(AuditLog.id).where(AuditLog.action == "backup_restored", AuditLog.username == "système"))

    # Erreurs métier
    with pytest.raises(BusinessError, match="introuvable"):
        await backup_manager.restore_job(db, 999999, hs)
    await backup_manager.delete_job(db, job_id)
    with pytest.raises(BusinessError, match="pas restaurable"):
        await backup_manager.restore_job(db, job_id, hs)
    ghost = BackupJob(id=0, archive_name="inexistante.zip", destination="local", status="success")
    with pytest.raises(BusinessError, match="aucune copie Google Drive"):
        await backup_manager._locate_archive(db, ghost)
    with pytest.raises(BusinessError, match="Chemin de restauration invalide"):
        await backup_manager.restore_archive(db, tmp_dir / "ailleurs.zip", hs)
    bogus = backup_manager.work_dir("_uploads") / "sans-base.zip"
    with zipfile.ZipFile(bogus, "w") as archive:
        archive.writestr("lisez-moi.txt", "rien")
    with pytest.raises(BusinessError, match="hotel.db"):
        await backup_manager.restore_upload(db, bogus, "sans-base.zip", hs)
    assert not bogus.exists()
    await db.rollback()


# ---------------------------------------------------------------- Google Drive simulé
async def test_drive_upload_restore_purge_and_failures(db, drive):
    hs = (await load_hotel_settings(db)).model_copy(update={
        "backup_drive_enabled": True, "backup_retention_days": 3650,
        "backup_drive_folder_id": "https://drive.google.com/drive/folders/dossier-ok?usp=sharing"})
    (job,) = await backup_manager.run_backup(db, hs, destinations=["drive"])
    await db.commit()
    assert job.status == "success" and job.remote_id in drive.store and job.remote_link.endswith(job.remote_id)
    stored = drive.store[job.remote_id]
    assert stored["name"] == job.archive_name and stored["parents"] == ["dossier-ok"]
    assert job.size_bytes == len(stored["content"]) and zipfile.is_zipfile(io.BytesIO(stored["content"]))
    assert job.local_path == ""  # archive locale non demandée : supprimée après l'envoi
    assert not list((config.backup_dir / "local").glob(f"*/{job.archive_name}"))

    # Téléchargement puis restauration en 1 clic : l'archive est rapatriée depuis Drive
    path, name = await backup_manager.download_path(db, job.id)
    assert name == job.archive_name and path.parent.name == "_restore" and zipfile.is_zipfile(path)
    path.unlink()
    result = await backup_manager.restore_job(db, job.id, hs, None)
    await db.commit()
    assert result["safety"].status == "success" and not (backup_manager.work_dir("_restore") / name).exists()

    # Double destination : une archive, deux lignes
    local_job, drive_job = await backup_manager.run_backup(db, hs, destinations=["local", "drive"])
    await db.commit()
    assert (local_job.status, drive_job.status) == ("success", "success")
    assert local_job.archive_name == drive_job.archive_name and Path(local_job.local_path).is_file()

    # Rétention : job Drive ancien et fichier orphelin hotel-backup-* ancien supprimés, récents conservés
    ancient = datetime.now() - timedelta(days=400)
    orphan = f"hotel-backup-{ancient:%Y-%m-%d_%H%M%S}.zip"
    drive.add_file(orphan, b"vieux")
    old_job = BackupJob(created_at=ancient, destination="drive", trigger="automatique", status="success",
                        archive_name="hotel-backup-ancien.zip", size_bytes=1,
                        remote_id=drive.add_file("hotel-backup-ancien.zip", b"x"))
    db.add(old_job)
    await db.flush()
    removed = await backup_manager.apply_retention(db, hs.model_copy(update={"backup_retention_days": 30}))
    await db.commit()
    assert removed["drive"] == 2 and old_job.status == "deleted" and old_job.remote_id not in drive.store
    assert orphan not in {f["name"] for f in drive.store.values()}
    assert job.remote_id in drive.store and drive_job.remote_id in drive.store

    # Suppression manuelle côté Drive (et tolérance d'un fichier déjà absent)
    deleted = await backup_manager.delete_job(db, drive_job.id)
    await db.commit()
    assert deleted.status == "deleted" and drive_job.remote_id not in drive.store
    backup_manager.delete_from_drive("deja-parti")

    # Échec réseau / HTTP : job en échec, message lisible, aucune exception
    drive.fail = ConnectionError("hôte injoignable")
    (failed,) = await backup_manager.run_backup(db, hs, destinations=["drive"])
    await db.commit()
    assert failed.status == "failed" and failed.error.startswith("Erreur réseau") and "injoignable" in failed.error
    drive.fail = _http_error(403, "Forbidden", "insufficientPermissions")
    (failed,) = await backup_manager.run_backup(db, hs, destinations=["drive"])
    assert failed.status == "failed" and "403" in failed.error and "partagez" in failed.error
    # Un échec Drive n'empêche pas la copie locale
    local_ok, drive_ko = await backup_manager.run_backup(db, hs, destinations=["local", "drive"])
    await db.commit()
    assert local_ok.status == "success" and drive_ko.status == "failed"
    ghost = BackupJob(id=0, archive_name="x.zip", destination="drive", status="success", remote_id="absent")
    with pytest.raises(BusinessError, match="Téléchargement depuis Google Drive impossible"):
        await backup_manager._locate_archive(db, ghost)
    drive.fail = None

    # Test de connexion (mémorisé) : dossier accessible, puis dossier inexistant
    result = await asyncio.to_thread(backup_manager.test_connection, hs.backup_drive_folder_id)
    assert result["ok"] and SERVICE_EMAIL in result["detail"] and "Sauvegardes hôtel" in result["detail"]
    assert backup_manager.credentials_info()["last_test"]["ok"] is True
    result = backup_manager.test_connection("dossier-inexistant")
    assert not result["ok"] and "404" in result["detail"]
    assert backup_manager.test_connection("")["detail"].endswith("racine du Drive")
    assert [f["name"] for f in backup_manager.list_drive_backups("dossier-ok")]
    (backup_manager.google_dir() / backup_manager.LAST_TEST_FILE).unlink()


# ---------------------------------------------------------------- identifiants Google (sans réseau)
def test_google_credentials_service_account_and_user(monkeypatch):
    backup_manager.unlink_credentials()
    assert backup_manager.credentials_info()["kind"] == "" and not backup_manager.has_credentials()
    with pytest.raises(BusinessError, match="Aucun compte Google"):
        backup_manager.load_credentials()
    rejected = [
        (b"", "vide"), (b"{pas du json", "JSON valide"), (b"[1, 2]", "objet"),
        (json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}).encode(), "non reconnu"),
        (json.dumps({"type": "service_account", "client_email": "a@b"}).encode(), "incomplet"),
        (json.dumps({"type": "service_account", "client_email": "a@b", "private_key": "pas-une-clé",
                     "token_uri": "https://oauth2.googleapis.com/token"}).encode(), "invalide"),
        (b"x" * (backup_manager.MAX_CREDENTIALS_SIZE + 1), "volumineux"),
    ]
    for payload, fragment in rejected:
        with pytest.raises(BusinessError, match=fragment):
            backup_manager.import_credentials(payload)
        assert not backup_manager.has_credentials()

    # Compte de service : seul le type et l'adresse sont exposés, jamais la clé
    info = backup_manager.import_credentials(json.dumps(service_account_json()).encode())
    assert info["kind"] == "service_account" and info["email"] == SERVICE_EMAIL and info["file"] == "credentials.json"
    assert "private_key" not in json.dumps(info)
    credentials = backup_manager.load_credentials()
    assert credentials.service_account_email == SERVICE_EMAIL
    assert set(credentials.scopes) == set(backup_manager.DRIVE_SCOPES)
    service = backup_manager.build_drive_service(credentials)  # document de découverte embarqué : hors ligne
    assert hasattr(service, "files") and hasattr(service, "about")

    # Jeton utilisateur expiré : rafraîchissement (simulé) puis réécriture du token.json
    refreshed = []

    def fake_refresh(creds):
        refreshed.append(creds.refresh_token)
        creds.token = "jeton-neuf"
        creds.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(backup_manager, "refresh_credentials", fake_refresh)
    info = backup_manager.import_credentials(json.dumps(user_token_json(expired=True)).encode())
    assert info["kind"] == "user" and info["email"] == USER_EMAIL and info["file"] == "token.json"
    assert not (backup_manager.google_dir() / "credentials.json").exists()  # un seul mode à la fois
    credentials = backup_manager.load_credentials()
    assert refreshed == ["refresh-xyz"] and credentials.token == "jeton-neuf" and credentials.valid
    saved = json.loads((backup_manager.google_dir() / "token.json").read_text(encoding="utf-8"))
    assert saved["token"] == "jeton-neuf" and saved["email"] == USER_EMAIL and saved["refresh_token"] == "refresh-xyz"

    # Rafraîchissement refusé par Google : erreur métier lisible, l'ancien fichier reste en place
    def failing_refresh(_creds):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(backup_manager, "refresh_credentials", failing_refresh)
    with pytest.raises(BusinessError, match="Authentification Google refusée"):
        backup_manager.import_credentials(json.dumps(user_token_json(expired=True)).encode())
    assert backup_manager.credentials_info()["kind"] == "user"
    assert json.loads((backup_manager.google_dir() / "token.json").read_text(encoding="utf-8"))["token"] == "jeton-neuf"
    token = user_token_json(expired=True)
    token.pop("refresh_token")
    with pytest.raises(BusinessError, match="non reconnu"):
        backup_manager.import_credentials(json.dumps(token).encode())
    assert backup_manager.import_credentials(json.dumps(user_token_json(expired=False)).encode())["kind"] == "user"
    backup_manager.unlink_credentials()
    assert backup_manager.credentials_info()["kind"] == "" and not backup_manager.has_credentials()


# ---------------------------------------------------------------- HTTP : page, commutateurs, actions
def test_backup_admin_http_flow(admin, reception, drive, tmp_dir):
    backup_manager.unlink_credentials()
    page = admin.get("/admin/backups")
    assert page.status_code == 200
    for label in ("Sauvegarde automatique sur disque local", "Sauvegarde automatique sur Google Drive",
                  "Lancer une sauvegarde immédiate", "Importer un fichier d'identification JSON", "Aucun compte associé"):
        assert label in page.text, label
    assert "Tester la connexion" not in page.text  # proposé seulement une fois un compte associé
    assert reception.form("/admin/backups/run", {"dest_local": "1"}).status_code == 403
    assert reception.get("/admin/backups/1/download").status_code == 403

    # Commutateurs et paramètres : seules les clés de sauvegarde changent
    before = run(_settings())
    assert admin.form("/admin/backups/settings", {
        "backup_drive_enabled": "1", "backup_hour": "4", "backup_retention_days": "45",
        "backup_drive_folder_id": "https://drive.google.com/drive/folders/ABC123?usp=sharing"}).status_code == 303
    after = run(_settings())
    assert (after.backup_local_enabled, after.backup_drive_enabled, after.backup_hour, after.backup_retention_days,
            after.backup_drive_folder_id) == (False, True, 4, 45, "ABC123")
    assert (after.tourist_tax_enabled, after.auto_no_show, after.hotel_name) == (
        before.tourist_tax_enabled, before.auto_no_show, before.hotel_name)
    page = admin.get("/admin/backups").text
    assert "Paramètres de sauvegarde enregistrés" in page and 'value="45"' in page
    assert admin.form("/admin/backups/settings", {"backup_local_enabled": "1", "backup_retention_days": "0"}).status_code == 303
    assert "Paramètres invalides" in admin.get("/admin/backups").text
    assert admin.form("/admin/backups/settings", {
        "backup_local_enabled": "1", "backup_hour": str(before.backup_hour),
        "backup_retention_days": str(before.backup_retention_days), "backup_drive_folder_id": ""}).status_code == 303
    restored = run(_settings())
    assert (restored.backup_local_enabled, restored.backup_drive_enabled, restored.backup_retention_days) == (
        True, False, before.backup_retention_days)

    # Sauvegarde immédiate
    assert admin.form("/admin/backups/run", {}).status_code == 303
    assert "au moins une destination" in admin.get("/admin/backups").text
    assert admin.form("/admin/backups/run", {"dest_local": "1"}).status_code == 303
    page = admin.get("/admin/backups").text
    assert "Sauvegarde terminée" in page and "Manuelle" in page and "Réussie" in page
    job_id = max(int(x) for x in re.findall(r"/admin/backups/(\d+)/download", page))
    download = admin.get(f"/admin/backups/{job_id}/download")
    assert download.status_code == 200 and download.content[:2] == b"PK"
    assert "hotel-backup-" in download.headers["content-disposition"]
    assert admin.get("/admin/backups/999999/download").status_code == 404
    assert run(_scalar(select(AuditLog.id).where(AuditLog.action == "backup_downloaded")))

    # Drive injoignable : échec propre (message lisible), historique conservé
    drive.fail = ConnectionError("hors ligne")
    assert admin.form("/admin/backups/run", {"dest_drive": "1"}).status_code == 303
    page = admin.get("/admin/backups").text
    assert "a échoué pour toutes les destinations" in page and "Échec" in page and "Erreur réseau" in page
    drive.fail = None

    # Restauration en 1 clic depuis l'historique
    assert admin.form(f"/admin/backups/{job_id}/restore").status_code == 303
    page = admin.get("/admin/backups").text
    assert "Base restaurée" in page and "Sauvegarde de sécurité créée avant restauration" in page
    assert "Avant restauration" in page

    # Import d'identifiants : JSON invalide refusé, jeton valide accepté sans jamais afficher les secrets
    bad = admin.post("/admin/backups/google/credentials", data={"csrf_token": admin.csrf},
                     files={"file": ("x.json", b'{"foo": 1}', "application/json")}, follow_redirects=False)
    assert bad.status_code == 303 and "non reconnu" in admin.get("/admin/backups").text
    bad = admin.post("/admin/backups/google/credentials", data={"csrf_token": admin.csrf},
                     files={"file": ("x.json", b"pas du json", "application/json")}, follow_redirects=False)
    assert bad.status_code == 303 and "JSON valide" in admin.get("/admin/backups").text
    good = admin.post("/admin/backups/google/credentials", data={"csrf_token": admin.csrf},
                      files={"file": ("token.json", json.dumps(user_token_json(expired=False)).encode(), "application/json")},
                      follow_redirects=False)
    assert good.status_code == 303
    page = admin.get("/admin/backups").text
    assert "Compte Google associé" in page and "Compte utilisateur (OAuth2)" in page and USER_EMAIL in page
    assert "Tester la connexion" in page and "Dissocier" in page
    assert "refresh-xyz" not in page and "secret-123" not in page and "jeton-ancien" not in page

    # Test de connexion (Drive simulé), réussite puis échec
    assert admin.form("/admin/backups/google/test").status_code == 303
    page = admin.get("/admin/backups").text
    assert "Connexion Google Drive réussie" in page and "Dernier test" in page
    drive.fail = ConnectionError("coupure")
    assert admin.form("/admin/backups/google/test").status_code == 303
    assert "Connexion Google Drive en échec" in admin.get("/admin/backups").text
    drive.fail = None

    # Sauvegarde immédiate vers les deux destinations, lien Drive affiché
    assert admin.form("/admin/backups/run", {"dest_local": "1", "dest_drive": "1"}).status_code == 303
    page = admin.get("/admin/backups").text
    assert "https://drive.example/" in page and "Google Drive" in page

    # Dissociation : la sauvegarde Drive automatique est désactivée
    assert admin.form("/admin/backups/settings", {"backup_local_enabled": "1", "backup_drive_enabled": "1"}).status_code == 303
    assert admin.form("/admin/backups/google/unlink").status_code == 303
    page = admin.get("/admin/backups").text
    assert "Compte Google dissocié" in page and "Aucun compte associé" in page
    assert run(_settings()).backup_drive_enabled is False
    assert admin.form("/admin/backups/google/unlink").status_code == 303
    assert "Aucun compte Google n'est associé" in html.unescape(admin.get("/admin/backups").text)

    # Import d'un fichier à restaurer : invalides refusés, archive .zip de l'application acceptée
    def upload(name, content):
        response = admin.post("/admin/backups/upload", data={"csrf_token": admin.csrf},
                              files={"file": (name, content, "application/octet-stream")}, follow_redirects=False)
        assert response.status_code == 303
        return admin.get("/admin/backups").text

    assert "pas une base de données SQLite valide" in upload("pas-une-base.db", b"bonjour")
    assert "Fichier vide" in upload("vide.db", b"")
    other = tmp_dir / "autre.db"
    connection = sqlite3.connect(other)
    connection.execute("create table t(x)")
    connection.commit()
    connection.close()
    assert "tables manquantes" in upload("autre.db", other.read_bytes())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("lisez-moi.txt", "rien")
    assert "ne contient pas de fichier hotel.db" in upload("sans-base.zip", buffer.getvalue())
    assert "Base restaurée depuis le fichier importé" in upload("ok.zip", download.content)
    assert not list(backup_manager.work_dir("_uploads").iterdir())

    # Suppression : archive effacée, téléchargement impossible, restauration refusée
    assert admin.form(f"/admin/backups/{job_id}/delete").status_code == 303
    page = admin.get("/admin/backups").text
    assert "Sauvegarde supprimée" in page and "Supprimée" in page
    assert admin.get(f"/admin/backups/{job_id}/download").status_code == 404
    assert admin.form(f"/admin/backups/{job_id}/delete").status_code == 303
    assert "déjà supprimée" in admin.get("/admin/backups").text
    assert admin.form(f"/admin/backups/{job_id}/restore").status_code == 303
    assert "pas restaurable" in admin.get("/admin/backups").text
    assert admin.form("/admin/backups/abc/restore").status_code == 422
    assert run(_scalar(select(AuditLog.id).where(AuditLog.action == "backup_deleted")))


def test_backup_drive_without_linked_account(admin):
    """Sans compte Google associé : aucun appel réseau, job Drive en échec et test de connexion refusé."""
    backup_manager.unlink_credentials()
    assert admin.form("/admin/backups/run", {"dest_local": "1", "dest_drive": "1"}).status_code == 303
    page = admin.get("/admin/backups").text
    assert "Sauvegarde terminée" in page and "Google Drive : Aucun compte Google associé" in page
    assert admin.form("/admin/backups/google/test").status_code == 303
    assert "Connexion Google Drive en échec : Aucun compte Google associé" in admin.get("/admin/backups").text


async def _scalar(stmt):
    async with SessionLocal() as db:
        return await db.scalar(stmt)


# ---------------------------------------------------------------- planificateur
async def test_scheduler_tick_triggers_enabled_destinations(db, drive, monkeypatch):
    hs = await load_hotel_settings(db)
    holder = {"hs": hs.model_copy(update={"backup_hour": 0, "backup_local_enabled": True, "backup_drive_enabled": True})}

    async def fake_settings(_db):
        return holder["hs"]

    async def no_audit(_db, _hs, _user):
        return []

    monkeypatch.setattr(scheduler, "load_hotel_settings", fake_settings)
    monkeypatch.setattr(scheduler.night_audit, "run_pending_audits", no_audit)
    today_start = datetime.combine(today_of(hs), time.min)  # journée de l'hôtel (fuseau Africa/Algiers)
    await db.execute(delete(BackupJob).where(BackupJob.trigger == "automatique", BackupJob.created_at >= today_start))
    await db.commit()

    summary = await scheduler.tick()
    assert summary["audits"] == 0
    assert summary["backup"]["destinations"] == {"local": "success", "drive": "success"}
    assert summary["backup"]["archive"].startswith("hotel-backup-")
    assert await db.scalar(select(AuditLog.id).where(AuditLog.action == "backup", AuditLog.entity_id == summary["backup"]["archive"]))
    # Déjà effectuée aujourd'hui pour les deux destinations : rien à faire
    assert (await scheduler.tick())["backup"] is None
    # Avant l'heure programmée : rien non plus
    holder["hs"] = holder["hs"].model_copy(update={"backup_hour": 23})
    assert (await scheduler.tick())["backup"] is None
    holder["hs"] = holder["hs"].model_copy(update={"backup_hour": 0})
    # Drive en échec : job en échec, puis pas de nouvel essai avant une heure (pas de tempête de réessais)
    await db.execute(delete(BackupJob).where(BackupJob.trigger == "automatique", BackupJob.destination == "drive",
                                             BackupJob.created_at >= today_start))
    await db.commit()
    drive.fail = ConnectionError("coupure réseau")
    summary = await scheduler.tick()
    assert summary["backup"]["destinations"] == {"drive": "failed"}
    assert (await scheduler.tick())["backup"] is None
    drive.fail = None
    # Un essai plus ancien qu'une heure est retenté
    await db.execute(delete(BackupJob).where(BackupJob.trigger == "automatique", BackupJob.destination == "drive",
                                             BackupJob.created_at >= today_start))
    db.add(BackupJob(created_at=datetime.now() - timedelta(hours=2), destination="drive", trigger="automatique",
                     status="failed", archive_name="hotel-backup-essai.zip", error="coupure"))
    await db.commit()
    assert (await scheduler.tick())["backup"]["destinations"] == {"drive": "success"}
    # Destinations toutes désactivées : aucune sauvegarde
    holder["hs"] = holder["hs"].model_copy(update={"backup_local_enabled": False, "backup_drive_enabled": False})
    await db.execute(delete(BackupJob).where(BackupJob.trigger == "automatique", BackupJob.created_at >= today_start))
    await db.commit()
    assert (await scheduler.tick())["backup"] is None
    assert await backup_manager.due_destinations(db, holder["hs"], datetime.now()) == []
