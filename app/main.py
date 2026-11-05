"""Point d'entrée FastAPI : middlewares, gestion des erreurs, planificateur, routeurs."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.config import config
from app.database import SessionLocal
from app.deps import AccessDenied, CsrfError, LoginRequired, PasswordChangeRequired, csrf_protect
from app.middlewares.language import LanguageMiddleware
from app.middlewares.license_checker import LicenseCheckerMiddleware
from app.routers import admin, auth, backup, crm, housekeeping, i18n, licensing, ota_channel, pms, pos, reports
from app.services import scheduler
from app.services.security import SecurityHeadersMiddleware
from app.services.seed import bootstrap, print_created_users
from app.templating import render_error

STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = logging.getLogger("app")


def configure_logging() -> None:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    file_handler = RotatingFileHandler(config.log_dir / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(console)
    root.setLevel(config.log_level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    created = await bootstrap()
    print_created_users(created)
    task = asyncio.create_task(scheduler.run_forever()) if config.scheduler_enabled else None
    logger.info("Application démarrée (v%s).", __version__)
    yield
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    app = FastAPI(
        title="PMS / CRM hôtelier – Annaba",
        version=__version__,
        description="Planning, réservations, audit de nuit, facturation DZD, CRM, POS, rapports, sauvegardes.",
        lifespan=lifespan,
        dependencies=[Depends(csrf_protect)],
        docs_url=None, redoc_url=None, openapi_url=None,  # documentation servie localement, réservée à l'admin
    )
    app.add_middleware(LanguageMiddleware)  # intérieur : langue courante (session, cookie, Accept-Language) via t()
    app.add_middleware(LicenseCheckerMiddleware)  # s'exécute après la session
    app.add_middleware(
        SessionMiddleware, secret_key=config.secret_key, session_cookie="hotel_session",
        max_age=config.session_max_age, same_site="lax", https_only=config.cookie_secure,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    for module in (auth, pms, crm, housekeeping, pos, reports, admin):
        app.include_router(module.router)
    app.include_router(licensing.router)
    app.include_router(backup.router)
    app.include_router(i18n.router)
    app.include_router(ota_channel.api)
    app.include_router(ota_channel.admin)

    @app.get("/health", include_in_schema=False)
    async def health():
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ok", "version": __version__}

    @app.exception_handler(LoginRequired)
    async def _login_required(_request: Request, exc: LoginRequired):
        target = "/login" if exc.next_path in ("/", "") else f"/login?next={quote(exc.next_path)}"
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(PasswordChangeRequired)
    async def _password_change(_request: Request, _exc: PasswordChangeRequired):
        return RedirectResponse("/account/password", status_code=303)

    @app.exception_handler(AccessDenied)
    async def _access_denied(request: Request, _exc: AccessDenied):
        return render_error(request, 403, "Accès refusé", "Votre rôle ne permet pas d'accéder à cette page.")

    @app.exception_handler(CsrfError)
    async def _csrf(request: Request, _exc: CsrfError):
        return render_error(request, 403, "Session expirée",
                            "Le formulaire a expiré ou provient d'une autre page. Rechargez la page et recommencez.")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        titles = {404: "Page introuvable", 405: "Méthode non autorisée", 400: "Requête invalide"}
        return render_error(request, exc.status_code, titles.get(exc.status_code, "Erreur"), str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api/"):
            # Seuls les champs sérialisables : le contexte pydantic peut contenir l'exception d'origine.
            errors = [{"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")}
                      for err in exc.errors()]
            return JSONResponse({"detail": errors}, status_code=422)
        return render_error(request, 422, "Données invalides", "Un champ du formulaire est manquant ou invalide.")

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        logger.exception("Erreur inattendue sur %s", request.url.path, exc_info=exc)
        return render_error(request, 500, "Erreur interne",
                            "Une erreur inattendue est survenue. Elle a été enregistrée dans le journal (logs/app.log).")

    return app


app = create_app()
