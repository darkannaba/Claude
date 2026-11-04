"""Dépendances FastAPI : session BD, utilisateur courant, rôles (RBAC), CSRF, actions métier."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.user import User
from app.services.common import BusinessError
from app.services.security import CSRF_FORM_FIELD, CSRF_HEADER, csrf_tokens_match
from app.templating import flash


class LoginRequired(Exception):
    def __init__(self, next_path: str = "/"):
        self.next_path = next_path


class PasswordChangeRequired(Exception):
    pass


class AccessDenied(Exception):
    pass


class CsrfError(Exception):
    pass


DbSession = Annotated[AsyncSession, Depends(get_session)]

PASSWORD_EXEMPT_PATHS = {"/account/password", "/logout"}


async def current_user(request: Request, db: DbSession) -> User:
    uid = request.session.get("uid")
    user = await db.get(User, uid) if uid else None
    if user is None or not user.active:
        request.session.pop("uid", None)
        raise LoginRequired(request.url.path)
    request.state.user = user
    if user.must_change_password and request.url.path not in PASSWORD_EXEMPT_PATHS:
        raise PasswordChangeRequired()
    return user


def require_roles(*roles: str):
    async def dependency(user: Annotated[User, Depends(current_user)]) -> User:
        if user.role not in roles:
            raise AccessDenied()
        return user

    return dependency


AnyUser = Annotated[User, Depends(current_user)]
StaffUser = Annotated[User, Depends(require_roles("admin", "reception"))]
AdminUser = Annotated[User, Depends(require_roles("admin"))]
HousekeepingUser = Annotated[User, Depends(require_roles("admin", "reception", "housekeeping"))]

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def csrf_protect(request: Request) -> None:
    """Toute requête modifiante doit porter le jeton CSRF de la session (champ de formulaire ou en-tête)."""
    if request.method not in UNSAFE_METHODS:
        return
    token = request.headers.get(CSRF_HEADER)
    if not token:
        content_type = request.headers.get("content-type", "")
        if "form" in content_type:
            form = await request.form()
            token = form.get(CSRF_FORM_FIELD)
    if not csrf_tokens_match(request.session, token if isinstance(token, str) else None):
        raise CsrfError()


async def attempt(request: Request, db: AsyncSession, action: Callable[[], Awaitable[object]], ok_message: str) -> bool:
    """Exécute une action métier : commit et message de succès, ou rollback et message d'erreur."""
    try:
        await action()
        await db.commit()
    except BusinessError as exc:
        await db.rollback()
        flash(request, str(exc), "error")
        return False
    flash(request, ok_message, "success")
    return True


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def safe_next(value: str | None, default: str = "/") -> str:
    """Évite les redirections ouvertes : seuls les chemins internes sont acceptés."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return default
