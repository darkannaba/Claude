"""Sauvegardes locales et Google Drive : administration (complété par le module backup_manager)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.deps import AdminUser, DbSession, attempt, redirect
from app.models.backup import BACKUP_DESTINATIONS, BACKUP_STATUSES
from app.services import backup_manager
from app.services.audit_log import log_action
from app.services.common import BusinessError
from app.services.settings import load_hotel_settings, save_settings_subset
from app.templating import flash, render

router = APIRouter(prefix="/admin/backups")
BACK = "/admin/backups"
SAFETY_MESSAGE = "Sauvegarde de sécurité créée avant restauration : "


def _on_off(value: bool) -> str:
    return "on" if value else "off"


# ---------------------------------------------------------------- page
@router.get("")
async def backups_page(request: Request, db: DbSession, user: AdminUser):
    jobs = await backup_manager.recent_jobs(db)
    google = await asyncio.to_thread(backup_manager.credentials_info)
    return await render(request, db, "admin/backup.html", jobs=jobs, google=google, drive_ready=bool(google["kind"]),
                        triggers=backup_manager.TRIGGERS, destinations=BACKUP_DESTINATIONS, statuses=BACKUP_STATUSES,
                        local_root=backup_manager.local_root())


# ---------------------------------------------------------------- paramètres (sous-ensemble des HotelSettings)
@router.post("/settings")
async def backup_settings_save(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()

    async def action():
        values: dict = {
            "backup_local_enabled": form.get("backup_local_enabled") == "1",
            "backup_drive_enabled": form.get("backup_drive_enabled") == "1",
        }
        for name in ("backup_hour", "backup_retention_days"):
            if name in form:
                values[name] = str(form.get(name) or "")
        if "backup_drive_folder_id" in form:
            values["backup_drive_folder_id"] = backup_manager.normalize_folder_id(str(form.get("backup_drive_folder_id") or ""))
        saved = await save_settings_subset(db, values)
        await log_action(db, user, "backup_settings_saved", "settings", "",
                         f"local={_on_off(saved.backup_local_enabled)}, drive={_on_off(saved.backup_drive_enabled)}, "
                         f"heure={saved.backup_hour}, rétention={saved.backup_retention_days} j, "
                         f"dossier={saved.backup_drive_folder_id or 'racine'}")

    await attempt(request, db, action, "Paramètres de sauvegarde enregistrés.")
    return redirect(BACK)


# ---------------------------------------------------------------- association Google
@router.post("/google/credentials")
async def google_credentials_import(request: Request, db: DbSession, user: AdminUser,
                                    file: Annotated[UploadFile, File()]):
    content = await file.read(backup_manager.MAX_CREDENTIALS_SIZE + 1)

    async def action():
        info = await asyncio.to_thread(backup_manager.import_credentials, content)
        await log_action(db, user, "backup_google_linked", "backup", "", f"{info['label']} {info['email']}".strip())

    await attempt(request, db, action, "Compte Google associé. Testez la connexion pour vérifier l'accès au dossier.")
    return redirect(BACK)


@router.post("/google/test")
async def google_test(request: Request, db: DbSession, user: AdminUser):
    hs = await load_hotel_settings(db)

    async def action():
        result = await asyncio.to_thread(backup_manager.test_connection, hs.backup_drive_folder_id)
        await log_action(db, user, "backup_google_test", "backup", "",
                         ("réussi – " if result["ok"] else "échec – ") + result["detail"])
        if not result["ok"]:
            await db.commit()  # le journal garde la trace de l'échec malgré le message d'erreur
            raise BusinessError("Connexion Google Drive en échec : " + result["detail"])
        flash(request, result["detail"], "info")

    await attempt(request, db, action, "Connexion Google Drive réussie.")
    return redirect(BACK)


@router.post("/google/unlink")
async def google_unlink(request: Request, db: DbSession, user: AdminUser):
    async def action():
        info = await asyncio.to_thread(backup_manager.credentials_info)
        if not info["kind"]:
            raise BusinessError("Aucun compte Google n'est associé.")
        await asyncio.to_thread(backup_manager.unlink_credentials)
        await save_settings_subset(db, {"backup_drive_enabled": False})
        await log_action(db, user, "backup_google_unlinked", "backup", "", f"{info['label']} {info['email']}".strip())

    await attempt(request, db, action, "Compte Google dissocié ; la sauvegarde automatique sur Google Drive est désactivée.")
    return redirect(BACK)


# ---------------------------------------------------------------- sauvegarde immédiate
@router.post("/run")
async def backup_run(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()
    hs = await load_hotel_settings(db)
    destinations = [name for name in BACKUP_DESTINATIONS if form.get(f"dest_{name}") == "1"]

    async def action():
        jobs = await backup_manager.run_backup(db, hs, trigger="manuelle", destinations=destinations, user=user)
        results = [(job.destination_label, job.status, job.error) for job in jobs]
        archive = jobs[0].archive_name
        await log_action(db, user, "backup", "backup", archive, "Sauvegarde manuelle : " + ", ".join(
            f"{label} {'réussie' if status == 'success' else 'en échec'}" for label, status, _error in results))
        failed = [(label, error) for label, status, error in results if status != "success"]
        for label, error in failed:
            flash(request, f"{label} : {error}", "warning")
        if len(failed) == len(results):
            await db.commit()  # l'historique conserve les jobs en échec
            raise BusinessError("La sauvegarde a échoué pour toutes les destinations demandées.")

    await attempt(request, db, action, "Sauvegarde terminée.")
    return redirect(BACK)


# ---------------------------------------------------------------- archives : téléchargement, restauration, suppression
@router.get("/{job_id}/download")
async def backup_download(db: DbSession, user: AdminUser, job_id: int):
    try:
        path, name = await backup_manager.download_path(db, job_id)
    except BusinessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await log_action(db, user, "backup_downloaded", "backup", job_id, name)
    await db.commit()
    # Une archive rapatriée depuis Drive n'est qu'un fichier temporaire : effacée une fois servie.
    cleanup = BackgroundTask(path.unlink, missing_ok=True) if path.parent == backup_manager.work_dir("_restore") else None
    media_type = "application/zip" if name.endswith(".zip") else "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=name, background=cleanup)


@router.post("/{job_id}/restore")
async def backup_restore(request: Request, db: DbSession, user: AdminUser, job_id: int):
    hs = await load_hotel_settings(db)

    async def action():
        result = await backup_manager.restore_job(db, job_id, hs, user)
        flash(request, SAFETY_MESSAGE + result["safety"].archive_name, "info")
        if result["migration_error"]:
            flash(request, "Migrations après restauration : " + result["migration_error"], "warning")

    await attempt(request, db, action, "Base restaurée. Reconnectez-vous si nécessaire.")
    return redirect(BACK)


@router.post("/{job_id}/delete")
async def backup_delete(request: Request, db: DbSession, user: AdminUser, job_id: int):
    async def action():
        job = await backup_manager.delete_job(db, job_id)
        await log_action(db, user, "backup_deleted", "backup", job.id, f"{job.archive_name} ({job.destination_label})")

    await attempt(request, db, action, "Sauvegarde supprimée.")
    return redirect(BACK)


async def _save_upload(file: UploadFile, target: Path) -> int:
    size = 0
    with target.open("wb") as handle:
        while chunk := await file.read(1024 * 1024):
            handle.write(chunk)
            size += len(chunk)
    return size


@router.post("/upload")
async def backup_upload(request: Request, db: DbSession, user: AdminUser, file: Annotated[UploadFile, File()]):
    hs = await load_hotel_settings(db)
    filename = file.filename or "import.zip"
    target = backup_manager.upload_target(filename)
    size = await _save_upload(file, target)

    async def action():
        if not size:
            target.unlink(missing_ok=True)
            raise BusinessError("Fichier vide.")
        result = await backup_manager.restore_upload(db, target, filename, hs, user)
        flash(request, SAFETY_MESSAGE + result["safety"].archive_name, "info")
        if result["migration_error"]:
            flash(request, "Migrations après restauration : " + result["migration_error"], "warning")

    await attempt(request, db, action, "Base restaurée depuis le fichier importé. Reconnectez-vous si nécessaire.")
    return redirect(BACK)
