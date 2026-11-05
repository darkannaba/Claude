"""Middleware ASGI optionnel : positionne la langue courante dès l'entrée de la requête.

``render()`` et ``render_error()`` détectent déjà la langue de chaque page ; ce middleware la rend en plus
disponible via ``t()`` (sans argument ``lang``) dans les routeurs et les services, avant tout rendu.
Il doit s'exécuter À L'INTÉRIEUR de ``SessionMiddleware`` pour lire ``session["lang"]`` : dans ``create_app``,
l'ajouter AVANT ``SessionMiddleware`` (``add_middleware`` empile de l'intérieur vers l'extérieur) :

    app.add_middleware(LanguageMiddleware)
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.services.i18n import detect_lang, reset_lang, set_lang


class LanguageMiddleware:
    """Détecte la langue (session, cookie, Accept-Language) et la pose dans le contexte de la requête."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        lang = detect_lang(Request(scope))
        scope.setdefault("state", {})["lang"] = lang
        token = set_lang(lang)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_lang(token)
