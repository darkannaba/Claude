"""Numérisation des pièces d'identité : douchette MRZ, téléversement / webcam (OCR), application à la fiche client,
consultation authentifiée des fichiers scannés.

Toutes les routes exigent un rôle d'accueil (administrateur ou réception) ; les requêtes POST passent par la
protection CSRF globale (en-tête ``X-CSRF-Token`` envoyé par app.js, ou champ ``csrf_token`` du formulaire
multipart) et chaque action est inscrite au journal d'activité. Les scans ne sont jamais servis par ``/static``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import update
from starlette.datastructures import UploadFile

from app.deps import DbSession, StaffUser, redirect
from app.models.booking import Reservation
from app.models.customer import Guest
from app.models.id_scan import ID_SCAN_SIDES, IdScan
from app.services import ocr_service
from app.services.audit_log import log_action
from app.services.common import BusinessError
from app.services.i18n import t
from app.services.loaders import load_reservation
from app.services.settings import load_hotel_settings, now_of, today_of
from app.templating import flash

router = APIRouter()

# Fichiers d'identité : jamais mis en cache, jamais interprétés autrement que selon leur type déclaré,
# et isolés (aucun script, aucune ressource externe) s'ils sont ouverts directement dans le navigateur.
FILE_HEADERS = {
    "Cache-Control": "no-store, private, max-age=0",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _error(message: str, status_code: int = 422, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": False, "message": t(message), **extra}, status_code=status_code,
                        headers={"Cache-Control": "no-store"})


def _translated(result: ocr_service.ExtractionResult) -> dict[str, Any]:
    data = result.to_dict()
    data["message"] = t(data["message"]) if data["message"] else ""
    data["warnings"] = [t(w) for w in data["warnings"]]
    data["notes"] = [t(n) for n in data["notes"]]
    data["check_labels"] = {key: t(label) for key, label in ocr_service.CHECK_LABELS.items() if key in data["checks"]}
    return data


def _int_or_none(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


async def _context(db, reservation_id: Any, guest_id: Any) -> tuple[Reservation | None, Guest | None]:
    reservation = guest = None
    res_id = _int_or_none(reservation_id)
    if res_id is not None:
        reservation = await load_reservation(db, res_id)
        if reservation is None:
            raise BusinessError("Réservation introuvable.")
    gid = _int_or_none(guest_id)
    if gid is not None:
        guest = await db.get(Guest, gid)
        if guest is None:
            raise BusinessError("Client introuvable.")
    return reservation, guest


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise BusinessError("Requête invalide (JSON attendu).") from None
    if not isinstance(data, dict):
        raise BusinessError("Requête invalide (JSON attendu).")
    return data


@router.get("/api/id-scan/status")
async def id_scan_status(user: StaffUser):
    available = await asyncio.to_thread(ocr_service.tesseract_available)
    status = ocr_service.tesseract_status()
    return JSONResponse({
        "tesseract": available, "tesseract_version": status["version"] if available else "",
        "message": "" if available else t(ocr_service.MSG_NO_TESSERACT),
        "pdf": ocr_service.pdf_engine_available(), "max_bytes": ocr_service.max_upload_bytes(),
    }, headers={"Cache-Control": "no-store"})


@router.post("/api/id-scan/mrz")
async def id_scan_mrz(request: Request, db: DbSession, user: StaffUser):
    """Texte brut d'une douchette (MRZ ou code-barres structuré) → champs proposés."""
    hs = await load_hotel_settings(db)
    try:
        body = await _json_body(request)
        text = str(body.get("text") or "")
        if len(text) > 5000:
            raise BusinessError("Saisie trop longue pour une pièce d'identité.")
        reservation, guest = await _context(db, body.get("reservation_id"), body.get("guest_id"))
    except BusinessError as exc:
        return _error(str(exc))
    result = ocr_service.parse_scanner_input(text, today_of(hs))
    scan = await ocr_service.record_scan(db, result, source="mrz_scanner", user=user, hs=hs, guest=guest,
                                         reservation=reservation)
    await db.commit()
    if result.status == "failed":
        return _error(result.message or ocr_service.MSG_UNRECOGNIZED, scan_id=scan.id, result=_translated(result))
    return JSONResponse({"ok": True, "scan_id": scan.id, "result": _translated(result)},
                        headers={"Cache-Control": "no-store"})


def _read_document(prepared: ocr_service.PreparedFile, today) -> ocr_service.ExtractionResult:
    """Lecture (bloquante, exécutée dans un thread) d'un fichier validé : OCR de l'image ou de la 1re page du PDF."""
    if prepared.is_pdf:
        if not prepared.pdf_readable:
            return ocr_service.ExtractionResult(engine="none", message=ocr_service.MSG_NO_PDF_ENGINE)
        if not ocr_service.tesseract_available():
            return ocr_service.ExtractionResult(engine="none", message=ocr_service.MSG_NO_TESSERACT)
        image = ocr_service.render_pdf_first_page(prepared.content)
        if image is None:
            return ocr_service.ExtractionResult(engine="none", message=ocr_service.MSG_NO_PDF_ENGINE)
        return ocr_service.ocr_image(image, today)
    if not ocr_service.tesseract_available():
        return ocr_service.ExtractionResult(engine="none", message=ocr_service.MSG_NO_TESSERACT)
    return ocr_service.ocr_image(prepared.image, today)


def _other_face_fields(raw) -> dict:
    """Champs déjà lus sur l'autre face (JSON envoyé par la fenêtre de scan) : seules des chaînes courtes."""
    if not isinstance(raw, str) or not raw or len(raw) > 4000:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    keys = ("id_number", "birth_date", "id_expiry_date")
    return {k: str(data[k])[:40] for k in keys if isinstance(data.get(k), str) and data.get(k)}


@router.post("/api/id-scan/upload")
async def id_scan_upload(request: Request, db: DbSession, user: StaffUser):
    """Image (PNG / JPEG), photo webcam ou PDF → fichier conservé + champs proposés si l'OCR est disponible."""
    hs = await load_hotel_settings(db)
    form = await request.form()
    upload = form.get("file")
    source = str(form.get("source") or "image_upload")
    side = str(form.get("side") or "recto")
    try:
        if not isinstance(upload, UploadFile):
            raise BusinessError("Aucun fichier reçu.")
        if source not in ("image_upload", "webcam"):
            raise BusinessError("Source de numérisation invalide.")
        if side not in ID_SCAN_SIDES:
            raise BusinessError("Face de la pièce invalide (recto ou verso).")
        reservation, guest = await _context(db, form.get("reservation_id"), form.get("guest_id"))
        data = await upload.read(ocr_service.max_upload_bytes() + 1)
        prepared = await asyncio.to_thread(ocr_service.prepare_file, data)
    except BusinessError as exc:
        return _error(str(exc))
    finally:
        if isinstance(upload, UploadFile):
            await upload.close()
    if prepared.is_pdf:
        source = "pdf"
    owner_id = guest.id if guest else (reservation.guest_id if reservation else None)
    await asyncio.to_thread(ocr_service.purge_pending)
    relative = await asyncio.to_thread(ocr_service.write_scan, prepared, owner_id)
    result = await asyncio.to_thread(_read_document, prepared, today_of(hs))
    other = _other_face_fields(form.get("other_fields"))
    if other:
        result = ocr_service.reconcile_with_other_face(result, other, today_of(hs))
    scan = await ocr_service.record_scan(
        db, result, source=source, user=user, hs=hs, side=side, guest=guest, reservation=reservation,
        file_path=relative, mime_type=prepared.mime_type, file_size=len(prepared.content))
    await db.commit()
    return JSONResponse({
        "ok": True, "scan_id": scan.id, "stored": True, "file_url": f"/id-scans/{scan.id}/file",
        "mime_type": prepared.mime_type, "tesseract": result.engine != "none" or ocr_service.tesseract_available(),
        "result": _translated(result),
    }, headers={"Cache-Control": "no-store"})


@router.post("/api/id-scan/apply")
async def id_scan_apply(request: Request, db: DbSession, user: StaffUser):
    """Champs vérifiés → création ou mise à jour de la fiche client (dédoublonnage par pièce), scans rattachés."""
    hs = await load_hotel_settings(db)
    try:
        body = await _json_body(request)
        fields = body.get("fields")
        if not isinstance(fields, dict):
            raise BusinessError("Champs de la pièce manquants.")
        reservation, guest = await _context(db, body.get("reservation_id"), body.get("guest_id"))
        raw_ids = body.get("scan_ids") or []
        if not isinstance(raw_ids, list) or len(raw_ids) > 4:
            raise BusinessError("Liste de numérisations invalide.")
        scans: list[IdScan] = []
        for raw in raw_ids:
            scan_id = _int_or_none(raw)
            scan = await db.get(IdScan, scan_id) if scan_id is not None else None
            if scan is None:
                raise BusinessError("Numérisation introuvable.")
            scans.append(scan)
        outcome = await ocr_service.apply_to_guest(db, fields, hs, user, scans=scans, guest=guest,
                                                   reservation=reservation)
        await db.commit()
    except BusinessError as exc:
        await db.rollback()
        return _error(str(exc))
    await asyncio.to_thread(ocr_service.delete_scan_files, outcome.files_to_delete)
    saved = outcome.guest
    payload: dict[str, Any] = {
        "ok": True, "created": outcome.created, "guest": ocr_service.guest_summary(saved, today_of(hs)),
        "guest_url": f"/guests/{saved.id}",
        "message": t("Fiche client créée à partir de la pièce d'identité." if outcome.created
                     else "Fiche client mise à jour à partir de la pièce d'identité."),
    }
    if reservation is not None:
        payload["police_url"] = f"/reservations/{reservation.id}/police.pdf"
        payload["reservation_url"] = f"/reservations/{reservation.id}"
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def _file_response(relative: str, download_name: str) -> FileResponse:
    path = ocr_service.resolve_scan(relative)
    if path is None:
        raise HTTPException(status_code=404, detail="Fichier introuvable.")
    mime = ocr_service.mime_of(relative)
    disposition = f'inline; filename="{download_name}.{relative.rsplit(".", 1)[-1]}"'
    return FileResponse(path, media_type=mime, headers={**FILE_HEADERS, "Content-Disposition": disposition})


@router.get("/id-scans/{scan_id}/file")
async def id_scan_file(db: DbSession, user: StaffUser, scan_id: int):
    scan = await db.get(IdScan, scan_id)
    if scan is None or not scan.file_path:
        raise HTTPException(status_code=404, detail="Fichier introuvable.")
    return _file_response(scan.file_path, f"piece-{scan.id}")


@router.get("/guests/{guest_id}/id-scan/{side}")
async def guest_id_scan_file(db: DbSession, user: StaffUser, guest_id: int, side: str):
    guest = await db.get(Guest, guest_id)
    if guest is None or side not in ID_SCAN_SIDES:
        raise HTTPException(status_code=404, detail="Fichier introuvable.")
    relative = guest.id_scan_back_path if side == "verso" else guest.id_scan_path
    if not relative:
        raise HTTPException(status_code=404, detail="Fichier introuvable.")
    return _file_response(relative, f"piece-client-{guest.id}-{side}")


@router.post("/guests/{guest_id}/id-scan/{side}/delete")
async def guest_id_scan_delete(request: Request, db: DbSession, user: StaffUser, guest_id: int, side: str):
    """Retire un scan de la fiche (scan erroné, droit à l'effacement) ; le fichier est supprimé du disque."""
    guest = await db.get(Guest, guest_id)
    if guest is None or side not in ID_SCAN_SIDES:
        raise HTTPException(status_code=404, detail="Client introuvable.")
    hs = await load_hotel_settings(db)
    attr = "id_scan_back_path" if side == "verso" else "id_scan_path"
    relative = getattr(guest, attr)
    if relative:
        setattr(guest, attr, "")
        await db.execute(update(IdScan).where(IdScan.file_path == relative).values(file_path=""))
        if not guest.id_scan_path and not guest.id_scan_back_path:
            guest.id_scanned_at = None
        await log_action(db, user, "id_scan_deleted", "guest", guest.id, f"{side} – {guest.full_name}", now_of(hs))
        await db.commit()
        await asyncio.to_thread(ocr_service.delete_scan_files, [relative])
        flash(request, "Scan de la pièce d'identité supprimé.")
    return redirect(f"/guests/{guest_id}")
