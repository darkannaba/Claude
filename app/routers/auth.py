"""Authentification : connexion (anti force brute), déconnexion, changement de mot de passe."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from sqlalchemy import select

from app.config import config
from app.deps import AnyUser, DbSession, redirect, safe_next
from app.models.user import User
from app.services.audit_log import log_action
from app.services.security import (
    LoginThrottle, hash_password, needs_rehash, password_problems, verify_password,
)
from app.services.settings import load_hotel_settings, now_of
from app.templating import flash, render

router = APIRouter()
throttle = LoginThrottle(config.login_max_attempts, config.login_lockout_seconds)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "inconnu"


@router.get("/login")
async def login_page(request: Request, db: DbSession, next: str = "/"):
    if request.session.get("uid"):
        return redirect(safe_next(next))
    return await render(request, db, "auth/login.html", next=safe_next(next))


@router.post("/login")
async def login(request: Request, db: DbSession, username: Annotated[str, Form()] = "",
                password: Annotated[str, Form()] = "", next: Annotated[str, Form()] = "/"):
    login_name = username.strip()
    keys = (f"user:{login_name.lower()}", f"ip:{_client_ip(request)}")
    if throttle.is_locked(*keys):
        minutes = config.login_lockout_seconds // 60
        return await render(request, db, "auth/login.html", status_code=429, next=safe_next(next),
                            error=f"Trop de tentatives. Réessayez dans {minutes} minute(s).")
    user = await db.scalar(select(User).where(User.username == login_name))
    if user is None or not user.active or not verify_password(password, user.password_hash):
        throttle.failure(*keys)
        await log_action(db, None, "login_failed", "user", login_name, f"IP {_client_ip(request)}")
        await db.commit()
        return await render(request, db, "auth/login.html", status_code=401, next=safe_next(next),
                            error="Identifiant ou mot de passe incorrect.")
    throttle.success(*keys)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    hs = await load_hotel_settings(db)
    user.last_login_at = now_of(hs)
    await log_action(db, user, "login", "user", user.username, f"IP {_client_ip(request)}", now_of(hs))
    await db.commit()
    csrf = request.session.get("csrf_token")
    request.session.clear()  # nouvelle session : protège contre la fixation de session
    request.session["uid"] = user.id
    if csrf:
        request.session["csrf_token"] = csrf
    if user.must_change_password:
        return redirect("/account/password")
    default = "/housekeeping" if user.role == "housekeeping" else "/"
    return redirect(safe_next(next, default) if next != "/" else default)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@router.get("/account/password")
async def password_page(request: Request, db: DbSession, user: AnyUser):
    return await render(request, db, "auth/password.html")


@router.post("/account/password")
async def change_password(request: Request, db: DbSession, user: AnyUser,
                          current: Annotated[str, Form()] = "", new: Annotated[str, Form()] = "",
                          confirm: Annotated[str, Form()] = ""):
    errors = []
    if not verify_password(current, user.password_hash):
        errors.append("Mot de passe actuel incorrect.")
    if new != confirm:
        errors.append("La confirmation ne correspond pas.")
    if new == current:
        errors.append("Le nouveau mot de passe doit être différent de l'ancien.")
    problems = password_problems(new)
    if problems:
        errors.append("Le mot de passe doit contenir " + ", ".join(problems) + ".")
    if errors:
        return await render(request, db, "auth/password.html", status_code=400, errors=errors)
    user.password_hash = hash_password(new)
    user.must_change_password = False
    hs = await load_hotel_settings(db)
    await log_action(db, user, "password_changed", "user", user.username, "", now_of(hs))
    await db.commit()
    flash(request, "Mot de passe modifié.")
    return redirect("/housekeeping" if user.role == "housekeeping" else "/")
