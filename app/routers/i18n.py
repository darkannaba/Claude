"""Sélection de la langue d'interface (FR / AR / EN)."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request

from app.config import config
from app.deps import redirect, safe_next
from app.services.i18n import LANG_COOKIE, LANG_COOKIE_MAX_AGE, LANG_SESSION_KEY, normalize_lang

router = APIRouter()


@router.post("/lang")
async def set_language(request: Request, lang: Annotated[str, Form()] = "", next: Annotated[str, Form()] = "/"):
    """Mémorise la langue choisie (session + cookie) et revient à la page d'origine (chemins internes seulement)."""
    code = normalize_lang(lang)
    if code is None:
        raise HTTPException(status_code=400, detail="Langue inconnue.")
    request.session[LANG_SESSION_KEY] = code
    response = redirect(safe_next(next))
    response.set_cookie(
        LANG_COOKIE, code, max_age=LANG_COOKIE_MAX_AGE, path="/",
        httponly=True, samesite="lax", secure=config.cookie_secure,
    )
    return response
