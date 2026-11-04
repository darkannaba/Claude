"""Dépendances FastAPI communes : authentification, rôles, rendu des templates, flash messages."""
from datetime import date
from pathlib import Path

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin import User
from app.models.guests import ID_TYPES
from app.models.property import ROOM_STATUSES
from app.models.reservations import CHARGE_CATEGORIES, PAYMENT_KINDS, PAYMENT_METHODS, STATUS_LABELS
from app.services.settings import MODULES, load_settings, today_of

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


class LoginRequired(Exception):
    """Utilisateur non connecté : redirigé vers /login."""


class AccessDenied(Exception):
    """Rôle insuffisant : réponse 403."""


def _money(value, symbol: str = "DA") -> str:
    try:
        number = int(round(float(value or 0)))
    except (TypeError, ValueError):
        number = 0
    return f"{number:,}".replace(",", " ") + f" {symbol}"


def _date(value) -> str:
    return value.strftime("%d/%m/%Y") if value else "—"


def _dt(value) -> str:
    return value.strftime("%d/%m/%Y %H:%M") if value else "—"


templates.env.filters.update(money=_money, date=_date, dt=_dt)
templates.env.globals.update(
    ROOM_STATUSES=ROOM_STATUSES,
    STATUS_LABELS=STATUS_LABELS,
    PAYMENT_KINDS=PAYMENT_KINDS,
    PAYMENT_METHODS=PAYMENT_METHODS,
    CHARGE_CATEGORIES=CHARGE_CATEGORIES,
    ID_TYPES=ID_TYPES,
    MODULES=MODULES,
)


def parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        return None


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flashes", []).append({"kind": kind, "msg": message})


def get_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is None or not user.active:
        raise LoginRequired()
    request.state.user = user
    return user


def roles(*allowed: str):
    def dependency(user: User = Depends(get_user)) -> User:
        if user.role not in allowed:
            raise AccessDenied()
        return user

    return dependency


any_user = get_user
staff = roles("admin", "reception")
admin_only = roles("admin")
housekeeping_team = roles("admin", "reception", "housekeeping")


def render(request: Request, db: Session, name: str, status_code: int = 200, **ctx):
    settings = load_settings(db)
    ctx.update(
        s=settings,
        user=getattr(request.state, "user", None),
        flashes=request.session.pop("flashes", []),
        today=today_of(settings),
    )
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect_to(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)
