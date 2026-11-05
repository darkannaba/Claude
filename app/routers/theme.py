"""Thématisation (white-label) : feuille CSS dynamique, fichiers de marque et administration de la charte.

Routes publiques (sans connexion, nécessaires dès la page de connexion) :
- ``GET /static/css/theme-custom.css`` : variables CSS de la charte (déclarée avant le montage ``/static``) ;
- ``GET /branding/{kind}`` : logo, logo pour le mode sombre, favicon (``?size=32`` : favicon 32 × 32).

Administration (rôle admin, jeton CSRF) : ``/admin/theme`` et ses actions POST.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from starlette.datastructures import UploadFile

from app.deps import AdminUser, DbSession, attempt, redirect
from app.models.theme import BRANDING_KINDS, THEME_DENSITIES
from app.services import theme_manager as tm
from app.services.audit_log import log_action
from app.services.common import BusinessError
from app.services.settings import HotelSettings, load_hotel_settings, save_settings_subset
from app.templating import flash, render

public = APIRouter()
router = APIRouter(prefix="/admin/theme")

SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'"


def _etag_matches(request: Request, etag: str) -> bool:
    header = request.headers.get("if-none-match", "")
    if not header:
        return False
    candidates = {item.strip().removeprefix("W/") for item in header.split(",")}
    return "*" in candidates or etag in candidates


# ---------------------------------------------------------------- routes publiques
@public.api_route("/static/css/theme-custom.css", methods=["GET", "HEAD"], include_in_schema=False)
async def theme_css(request: Request, db: DbSession):
    theme = await tm.get_theme(db)
    await db.commit()  # conserve la ligne par défaut si elle vient d'être créée
    css = tm.build_css(theme)
    etag = tm.css_etag(theme, css)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if _etag_matches(request, etag):
        return Response(status_code=304, headers=headers)
    return Response(content=css, media_type="text/css; charset=utf-8", headers=headers)


@public.api_route("/branding/{kind}", methods=["GET", "HEAD"], include_in_schema=False)
async def branding(kind: str, request: Request, db: DbSession, size: int | None = None):
    if kind not in BRANDING_KINDS:
        raise HTTPException(status_code=404, detail="Fichier introuvable.")
    theme = await tm.get_theme(db)
    await db.commit()
    path = tm.branding_file(theme, kind, size)
    if path is None:
        raise HTTPException(status_code=404, detail="Fichier introuvable.")
    etag = f'"{path.name}"'
    media_type = tm.media_type_for(path)
    headers = {"ETag": etag, "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}
    if media_type == "image/svg+xml":
        headers["Content-Security-Policy"] = SVG_CSP
    if _etag_matches(request, etag):
        return Response(status_code=304, headers=headers)
    return Response(content=path.read_bytes(), media_type=media_type, headers=headers)


# ---------------------------------------------------------------- administration
@router.get("")
async def theme_page(request: Request, db: DbSession, user: AdminUser):
    theme = await tm.get_theme(db)
    await db.commit()
    hs = await load_hotel_settings(db)
    return await render(
        request, db, "admin/theme.html",
        theme=theme, branding=tm.branding_urls(theme), color_fields=tm.color_fields(theme),
        defaults=tm.DEFAULT_THEME, fonts={key: font["label"] for key, font in tm.FONTS.items()},
        densities=THEME_DENSITIES, branding_kinds=BRANDING_KINDS, preview=tm.preview(theme),
        document_logo=tm.logo_path_for_documents(theme) is not None,
        identity_fields=[{"name": name, "label": HotelSettings.model_fields[name].title, "value": getattr(hs, name)}
                         for name in tm.IDENTITY_FIELDS],
        max_upload_mb=tm.MAX_UPLOAD_BYTES // (1024 * 1024),
    )


def _text_fields(form) -> dict[str, str]:
    return {key: value for key, value in form.items() if isinstance(value, str)}


@router.post("/colors")
async def theme_colors(request: Request, db: DbSession, user: AdminUser):
    values = _text_fields(await request.form())

    async def action():
        theme = await tm.update_colors(db, values)
        details = ", ".join(f"{field}={getattr(theme, field)}" for field in tm.COLOR_FIELDS)
        await log_action(db, user, "theme_colors_saved", "theme", tm.THEME_ID, details)

    if await attempt(request, db, action, "Couleurs de la charte enregistrées."):
        for warning in tm.color_warnings(await tm.get_theme(db)):
            flash(request, warning, "warning")
    return redirect("/admin/theme")


@router.post("/typography")
async def theme_typography(request: Request, db: DbSession, user: AdminUser):
    values = _text_fields(await request.form())

    async def action():
        theme = await tm.update_typography(db, values.get("font_family", ""), values.get("density", ""))
        await log_action(db, user, "theme_typography_saved", "theme", tm.THEME_ID,
                         f"police={theme.font_family}, densité={theme.density}")

    await attempt(request, db, action, "Typographie et densité enregistrées.")
    return redirect("/admin/theme")


@router.post("/documents")
async def theme_documents(request: Request, db: DbSession, user: AdminUser):
    values = _text_fields(await request.form())
    identity = {name: values[name] for name in tm.IDENTITY_FIELDS if name in values}

    async def action():
        await tm.update_documents(db, values)
        if identity:
            await save_settings_subset(db, identity)
        await log_action(db, user, "theme_documents_saved", "theme", tm.THEME_ID,
                         "identité : " + (", ".join(sorted(identity)) or "inchangée"))

    await attempt(request, db, action, "Personnalisation des documents enregistrée.")
    return redirect("/admin/theme")


@router.post("/upload/{kind}")
async def theme_upload(kind: str, request: Request, db: DbSession, user: AdminUser):
    form = await request.form()
    upload = form.get("file")

    async def action():
        if kind not in BRANDING_KINDS:
            raise BusinessError("Type de fichier de marque inconnu.")
        if not isinstance(upload, UploadFile):
            raise BusinessError("Aucun fichier sélectionné.")
        original = (upload.filename or "")[:120]
        theme = await tm.save_image(db, kind, upload)
        await log_action(db, user, "theme_file_uploaded", "theme", tm.THEME_ID,
                         f"{kind} : {original} → {getattr(theme, tm.KIND_FIELDS[kind])}")

    await attempt(request, db, action, "Fichier de marque enregistré.")
    return redirect("/admin/theme")


@router.post("/delete/{kind}")
async def theme_delete(kind: str, request: Request, db: DbSession, user: AdminUser):
    async def action():
        await tm.delete_image(db, kind)
        await log_action(db, user, "theme_file_deleted", "theme", tm.THEME_ID, kind)

    await attempt(request, db, action, "Fichier de marque supprimé.")
    return redirect("/admin/theme")


@router.post("/reset")
async def theme_reset(request: Request, db: DbSession, user: AdminUser):
    async def action():
        await tm.reset_colors(db)
        await log_action(db, user, "theme_colors_reset", "theme", tm.THEME_ID, "palette d'origine")

    await attempt(request, db, action, "Couleurs par défaut rétablies.")
    return redirect("/admin/theme")
