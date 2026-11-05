"""Contrôle de licence sur chaque requête (sauf connexion, activation, ressources statiques)."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import config
from app.database import SessionLocal
from app.services import licensing
from app.templating import render_error

EXEMPT_PREFIXES = ("/static", "/login", "/logout", "/lang", "/activate", "/health", "/account/password")


def _exempt(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?")
               for prefix in EXEMPT_PREFIXES)


class LicenseCheckerMiddleware:
    """Licence absente, expirée ou émise pour une autre machine : redirection vers /activate.

    Les API (``/api/…``) reçoivent une réponse JSON 403. Un module non couvert par la licence
    (POS, housekeeping, OTA) est refusé avec une page explicative.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not config.license_enforced or _exempt(scope["path"]):
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        status = await licensing.current_status(SessionLocal)
        is_api = path.startswith("/api/")
        if not status.valid:
            if is_api:
                response = JSONResponse({"detail": f"Licence invalide : {status.reason}"}, status_code=403)
            else:
                response = RedirectResponse("/activate", status_code=303)
            await response(scope, receive, send)
            return
        feature = licensing.feature_for_path(path)
        if feature and not status.allows(feature):
            label = licensing.FEATURES[feature]
            message = f"Le module « {label} » n'est pas inclus dans la licence de cet établissement."
            if is_api:
                response = JSONResponse({"detail": message}, status_code=403)
            else:
                response = render_error(Request(scope), 403, "Module non licencié", message)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
