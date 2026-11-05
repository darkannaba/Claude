"""Page d'activation de la licence : ID matériel du serveur et saisie de la clé."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request

from app.deps import AdminUser, AnyUser, DbSession, redirect
from app.services import licensing
from app.services.audit_log import log_action
from app.services.settings import load_hotel_settings, now_of
from app.templating import flash, render

router = APIRouter()


@router.get("/activate")
async def activate_page(request: Request, db: DbSession, user: AnyUser):
    licensing.invalidate_cache()
    status = await licensing.evaluate(db, now_of(await load_hotel_settings(db)))
    return await render(request, db, "admin/activate.html", status=status, features=licensing.FEATURES,
                        public_key_present=licensing.load_public_key() is not None)


@router.post("/activate")
async def activate_submit(request: Request, db: DbSession, user: AdminUser,
                          license_key: Annotated[str, Form()] = ""):
    hs = await load_hotel_settings(db)
    now = now_of(hs)
    try:
        data = await licensing.activate(db, license_key, user, now)
        await log_action(db, user, "license_activated", "license", data.license_id,
                         f"{data.hotel_name} – {data.type_label} – modules : {', '.join(data.features)}", now)
        await db.commit()
    except licensing.LicenseError as exc:
        await db.rollback()
        flash(request, f"Activation refusée : {exc}", "error")
        return redirect("/activate")
    flash(request, f"Licence {data.license_id} activée pour « {data.hotel_name} ».")
    return redirect("/")
