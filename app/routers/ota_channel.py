"""Channel manager : API REST pour OTA (Booking.com, Expedia…) et administration des canaux."""
from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.deps import AdminUser, DbSession, attempt, redirect
from app.models.ota import OTA_CHANNELS, OTA_PERMISSIONS, OtaApiKey, OtaBooking, OtaLog
from app.services import ota
from app.services.audit_log import log_action
from app.services.common import BusinessError, parse_date
from app.services.settings import load_hotel_settings, now_of, today_of
from app.templating import flash, render

api = APIRouter(prefix="/api/v1/ota", tags=["Channel manager OTA"])
admin = APIRouter(prefix="/admin/ota")


def require_permission(permission: str):
    """Authentification par clé d'API (en-tête X-OTA-API-KEY) et contrôle de la permission."""

    async def dependency(request: Request, db: DbSession,
                         x_ota_api_key: Annotated[str | None, Header(alias="X-OTA-API-KEY")] = None) -> OtaApiKey:
        hs = await load_hotel_settings(db)
        key = await ota.authenticate(db, x_ota_api_key or "")
        detail = None
        status_code = 200
        if key is None:
            status_code, detail = 401, "Clé API OTA absente, invalide ou révoquée."
        elif permission not in key.permission_list:
            status_code, detail = 403, f"Permission « {permission} » non accordée à cette clé."
        elif not key.sync_enabled:
            status_code, detail = 403, f"Synchronisation désactivée pour le canal {key.channel_label}."
        if detail:
            await ota.log_event(db, key_id=key.id if key else None, channel=key.channel if key else "",
                                event="auth_refused", endpoint=request.url.path, payload="",
                                response={"detail": detail}, http_status=status_code, when=now_of(hs))
            await db.commit()
            raise HTTPException(status_code=status_code, detail=detail)
        key.last_used_at = now_of(hs)
        return key

    return dependency


# ---------------------------------------------------------------- API ARI
@api.get("/inventory", summary="Disponibilités et tarifs (ARI) par type de chambre et par nuit")
async def get_inventory(request: Request, db: DbSession,
                        key: Annotated[OtaApiKey, Depends(require_permission("inventory:read"))],
                        start: str = "", end: str = ""):
    hs = await load_hotel_settings(db)
    key_id, channel = key.id, key.channel
    d_start = parse_date(start) or today_of(hs)
    d_end = parse_date(end) or d_start + timedelta(days=30)
    try:
        body = await ota.inventory(db, hs, d_start, d_end)
        status_code = 200
    except BusinessError as exc:
        body, status_code = {"detail": str(exc)}, 422
    await ota.log_event(db, key_id=key_id, channel=channel, event="inventory_read", endpoint=request.url.path,
                        payload={"start": d_start.isoformat(), "end": d_end.isoformat()},
                        response={"room_types": len(body.get("room_types", []))} if status_code == 200 else body,
                        http_status=status_code, when=now_of(hs))
    await db.commit()
    return JSONResponse(body, status_code=status_code)


@api.post("/rates", summary="Mise à jour des tarifs depuis un canal")
async def post_rates(request: Request, db: DbSession, payload: ota.OtaRatesIn,
                     key: Annotated[OtaApiKey, Depends(require_permission("rates:write"))]):
    hs = await load_hotel_settings(db)
    key_id, channel = key.id, key.channel
    try:
        body = await ota.apply_rates(db, payload, key)
        status_code = 200
    except BusinessError as exc:
        await db.rollback()
        body, status_code = {"detail": str(exc)}, 422
    await ota.log_event(db, key_id=key_id, channel=channel, event="rates_update", endpoint=request.url.path,
                        payload=payload.model_dump(mode="json"), response=body, http_status=status_code,
                        when=now_of(hs))
    await db.commit()
    return JSONResponse(body, status_code=status_code)


# ---------------------------------------------------------------- webhook réservations
@api.post("/reservations/webhook", summary="Réservations entrantes : création, modification, annulation")
async def reservation_webhook(request: Request, db: DbSession, payload: ota.OtaReservationIn,
                              key: Annotated[OtaApiKey, Depends(require_permission("reservations:write"))]):
    hs = await load_hotel_settings(db)
    key_id, channel = key.id, key.channel
    async with ota.reservation_lock:
        try:
            status_code, body = await ota.handle_reservation(db, payload, key, hs)
            if status_code >= 400:
                await db.rollback()
        except BusinessError as exc:
            await db.rollback()
            status_code, body = 409, {"status": "rejected", "detail": str(exc)}
        await ota.log_event(db, key_id=key_id, channel=channel, event=f"reservation_{payload.event}",
                            endpoint=request.url.path,
                            payload=payload.model_dump(mode="json"), response=body, http_status=status_code,
                            external_ref=payload.external_id, reservation_id=body.get("reservation_id"),
                            when=now_of(hs))
        await db.commit()
    return JSONResponse(body, status_code=status_code)


# ---------------------------------------------------------------- administration des canaux
async def _admin_context(db, channel: str = "", status: str = "") -> dict:
    keys = (await db.scalars(select(OtaApiKey).order_by(OtaApiKey.active.desc(), OtaApiKey.created_at.desc()))).all()
    stmt = select(OtaLog).order_by(OtaLog.created_at.desc(), OtaLog.id.desc()).limit(200)
    if channel in OTA_CHANNELS:
        stmt = stmt.where(OtaLog.channel == channel)
    if status == "errors":
        stmt = stmt.where(OtaLog.http_status >= 400)
    logs = (await db.scalars(stmt)).all()
    stats = {
        "bookings": await db.scalar(select(func.count(OtaBooking.id))) or 0,
        "requests": await db.scalar(select(func.count(OtaLog.id))) or 0,
        "errors": await db.scalar(select(func.count(OtaLog.id)).where(OtaLog.http_status >= 400)) or 0,
    }
    return {"keys": keys, "logs": logs, "channels": OTA_CHANNELS, "permissions": OTA_PERMISSIONS,
            "channel": channel, "status": status, "stats": stats, "key_names": {k.id: k.name for k in keys}}


@admin.get("")
async def ota_page(request: Request, db: DbSession, user: AdminUser, channel: str = "", status: str = ""):
    return await render(request, db, "admin/ota.html", **await _admin_context(db, channel, status))


@admin.post("/keys")
async def ota_key_create(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()
    hs = await load_hotel_settings(db)
    try:
        key, raw = await ota.create_api_key(
            db, name=str(form.get("name") or ""), channel=str(form.get("channel") or ""),
            permissions=[p for p in OTA_PERMISSIONS if form.get(f"perm_{p}") == "1"], hs=hs, user=user)
        await db.commit()
    except BusinessError as exc:
        await db.rollback()
        flash(request, str(exc), "error")
        return redirect("/admin/ota")
    # La clé en clair n'est affichée qu'ici, une seule fois (jamais stockée, jamais mise en session).
    return await render(request, db, "admin/ota.html", new_key=raw, new_key_name=key.name, **await _admin_context(db))


@admin.post("/keys/{key_id}/revoke")
async def ota_key_revoke(request: Request, db: DbSession, user: AdminUser, key_id: int):
    hs = await load_hotel_settings(db)

    async def action():
        key = await db.get(OtaApiKey, key_id)
        if key is None:
            raise BusinessError("Clé introuvable.")
        key.active, key.revoked_at = False, now_of(hs)
        await log_action(db, user, "ota_key_revoked", "ota_key", key.id, key.name, now_of(hs))

    await attempt(request, db, action, "Clé révoquée : le canal ne peut plus appeler l'API.")
    return redirect("/admin/ota")


@admin.post("/keys/{key_id}/sync")
async def ota_key_toggle_sync(request: Request, db: DbSession, user: AdminUser, key_id: int):
    hs = await load_hotel_settings(db)
    state = {}

    async def action():
        key = await db.get(OtaApiKey, key_id)
        if key is None or not key.active:
            raise BusinessError("Clé introuvable ou révoquée.")
        key.sync_enabled = not key.sync_enabled
        state["on"] = key.sync_enabled
        await log_action(db, user, "ota_sync_toggled", "ota_key", key.id,
                         f"{key.name} : {'activée' if key.sync_enabled else 'désactivée'}", now_of(hs))

    await attempt(request, db, action, "Synchronisation du canal mise à jour.")
    return redirect("/admin/ota")
