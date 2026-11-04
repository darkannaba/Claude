from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_secret_key
from app.deps import AccessDenied, LoginRequired
from app.routers import admin, auth, dashboard, guests, housekeeping, reservations
from app.services.seed import run as seed_database

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    seed_database([])  # création des tables + données initiales (idempotent)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Gestion hôtelière – Annaba",
        version="1.0.0",
        description="PMS / CRM : planning, réservations, clients, facturation DZD, housekeeping.",
        lifespan=lifespan,
    )
    app.add_middleware(SessionMiddleware, secret_key=get_secret_key(), same_site="lax", max_age=60 * 60 * 12)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    for module in (auth, dashboard, reservations, guests, housekeeping, admin):
        app.include_router(module.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(_request, _exc):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(AccessDenied)
    async def _access_denied(_request, _exc):
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>Accès refusé</title>"
            "<body style='font-family:sans-serif;padding:2rem'><h1>403 – Accès refusé</h1>"
            "<p>Votre rôle ne permet pas cette action.</p><a href='/'>Retour à l'accueil</a></body>",
            status_code=403,
        )

    return app


app = create_app()
