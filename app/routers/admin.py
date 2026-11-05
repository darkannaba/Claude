"""Administration : paramètres, chambres, tarifs, modules, utilisateurs, sauvegardes, journal, API."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.config import config
from app.deps import AdminUser, DbSession, attempt, redirect
from app.models.audit import AuditLog
from app.models.room import RatePeriod, Room, RoomType
from app.models.system import FeatureFlag
from app.models.user import ROLES, User
from app.database import SessionLocal
from app.services import licensing, printing, theme_manager
from app.services.audit_log import log_action
from app.services.common import BusinessError, to_date, to_int
from app.services.security import hash_password, password_problems
from app.services.settings import load_hotel_settings, save_hotel_settings, settings_form
from app.templating import render

router = APIRouter(prefix="/admin")


async def _check_room_limit(db, adding: int = 1) -> None:
    """Refuse de dépasser le nombre de chambres couvert par la licence."""
    if not config.license_enforced:
        return
    status = await licensing.current_status(SessionLocal)
    if status.data is None:
        return
    active = await db.scalar(select(func.count(Room.id)).where(Room.active.is_(True))) or 0
    if active + adding > status.data.max_rooms:
        raise BusinessError(f"La licence couvre {status.data.max_rooms} chambres actives : limite atteinte.")


# ---------------------------------------------------------------- paramètres
@router.get("")
async def settings_page(request: Request, db: DbSession, user: AdminUser):
    hs = await load_hotel_settings(db)
    return await render(request, db, "admin/settings.html", groups=settings_form(hs))


@router.post("/settings")
async def settings_save(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()

    async def action():
        await save_hotel_settings(db, {k: v for k, v in form.items() if isinstance(v, str)})
        await log_action(db, user, "settings_saved", "settings", "", "")

    await attempt(request, db, action, "Paramètres enregistrés.")
    return redirect("/admin")


@router.post("/printers/test")
async def printer_test(request: Request, db: DbSession, user: AdminUser):
    hs = await load_hotel_settings(db)
    theme = await theme_manager.get_theme(db)

    async def action():
        await printing.print_raw(hs.thermal_printer_host, hs.thermal_printer_port, printing.test_page(hs, theme))

    await attempt(request, db, action, "Page de test envoyée à l'imprimante ticket.")
    return redirect("/admin")


# ---------------------------------------------------------------- chambres
@router.get("/rooms")
async def rooms_page(request: Request, db: DbSession, user: AdminUser):
    room_types = (await db.scalars(select(RoomType).order_by(RoomType.code))).all()
    rooms = (await db.scalars(select(Room).order_by(Room.number))).unique().all()
    return await render(request, db, "admin/rooms.html", room_types=room_types, rooms=rooms)


def _room_type_values(form) -> dict:
    get = lambda key: str(form.get(key) or "").strip()  # noqa: E731
    if not get("name"):
        raise BusinessError("Libellé requis.")
    return {"name": get("name")[:80], "name_ar": get("name_ar")[:80], "description": get("description"),
            "base_price": to_int(get("base_price"), "Prix de base", minimum=0),
            "max_occupancy": to_int(get("max_occupancy"), "Capacité", default=2, minimum=1, maximum=12)}


@router.post("/room-types")
async def room_type_create(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()

    async def action():
        code = str(form.get("code") or "").strip().upper()
        if not code:
            raise BusinessError("Code requis.")
        if await db.scalar(select(func.count(RoomType.id)).where(RoomType.code == code)):
            raise BusinessError("Ce code existe déjà.")
        db.add(RoomType(code=code[:20], **_room_type_values(form)))
        await log_action(db, user, "room_type_created", "room_type", code, "")

    await attempt(request, db, action, "Type de chambre créé.")
    return redirect("/admin/rooms")


@router.post("/room-types/{type_id}")
async def room_type_update(request: Request, db: DbSession, user: AdminUser, type_id: int):
    form = await request.form()

    async def action():
        room_type = await db.get(RoomType, type_id)
        if room_type is None:
            raise BusinessError("Type introuvable.")
        values = _room_type_values(form)
        old_price = room_type.base_price
        for key, value in values.items():
            setattr(room_type, key, value)
        await log_action(db, user, "room_type_updated", "room_type", room_type.code,
                         f"prix {old_price} → {room_type.base_price}")

    await attempt(request, db, action, "Type mis à jour (les nuitées déjà clôturées ne changent pas).")
    return redirect("/admin/rooms")


@router.post("/rooms")
async def room_create(request: Request, db: DbSession, user: AdminUser, number: Annotated[str, Form()] = "",
                      floor: Annotated[str, Form()] = "0", room_type_id: Annotated[str, Form()] = ""):
    async def action():
        if not number.strip():
            raise BusinessError("Numéro requis.")
        if await db.scalar(select(func.count(Room.id)).where(Room.number == number.strip())):
            raise BusinessError("Ce numéro de chambre existe déjà.")
        type_id = to_int(room_type_id, "Type de chambre")
        if await db.get(RoomType, type_id) is None:
            raise BusinessError("Type de chambre introuvable.")
        await _check_room_limit(db)
        db.add(Room(number=number.strip()[:12], floor=to_int(floor, "Étage", default=0), room_type_id=type_id))
        await log_action(db, user, "room_created", "room", number.strip(), "")

    await attempt(request, db, action, "Chambre ajoutée.")
    return redirect("/admin/rooms")


@router.post("/rooms/{room_id}")
async def room_update(request: Request, db: DbSession, user: AdminUser, room_id: int,
                      number: Annotated[str, Form()] = "", floor: Annotated[str, Form()] = "0",
                      room_type_id: Annotated[str, Form()] = "", notes: Annotated[str, Form()] = "",
                      active: Annotated[str, Form()] = ""):
    async def action():
        room = await db.get(Room, room_id)
        if room is None:
            raise BusinessError("Chambre introuvable.")
        new_number = number.strip()
        if new_number and new_number != room.number:
            if await db.scalar(select(func.count(Room.id)).where(Room.number == new_number)):
                raise BusinessError("Ce numéro de chambre existe déjà.")
            room.number = new_number[:12]
        room.floor = to_int(floor, "Étage", default=room.floor)
        room.room_type_id = to_int(room_type_id, "Type", default=room.room_type_id)
        room.notes = notes.strip()
        if not active and room.status == "occupied":
            raise BusinessError("Une chambre occupée ne peut pas être désactivée.")
        if active and not room.active:
            await _check_room_limit(db)
        room.active = bool(active)
        await log_action(db, user, "room_updated", "room", room.number, "active" if room.active else "désactivée")

    await attempt(request, db, action, "Chambre mise à jour.")
    return redirect("/admin/rooms")


# ---------------------------------------------------------------- tarifs saisonniers
@router.get("/rates")
async def rates_page(request: Request, db: DbSession, user: AdminUser):
    periods = (await db.scalars(select(RatePeriod).order_by(RatePeriod.start_date))).unique().all()
    room_types = (await db.scalars(select(RoomType).order_by(RoomType.code))).all()
    return await render(request, db, "admin/rates.html", periods=periods, room_types=room_types)


@router.post("/rates")
async def rate_create(request: Request, db: DbSession, user: AdminUser, name: Annotated[str, Form()] = "",
                      room_type_id: Annotated[str, Form()] = "", start_date: Annotated[str, Form()] = "",
                      end_date: Annotated[str, Form()] = "", price: Annotated[str, Form()] = "",
                      priority: Annotated[str, Form()] = "0"):
    async def action():
        start, end = to_date(start_date, "Début"), to_date(end_date, "Fin")
        if end <= start:
            raise BusinessError("La fin doit être postérieure au début.")
        type_id = to_int(room_type_id, "Type de chambre")
        if await db.get(RoomType, type_id) is None:
            raise BusinessError("Type de chambre introuvable.")
        db.add(RatePeriod(name=name.strip() or "Tarif saisonnier", room_type_id=type_id, start_date=start,
                          end_date=end, price=to_int(price, "Prix", minimum=0),
                          priority=to_int(priority, "Priorité", default=0)))
        await log_action(db, user, "rate_created", "rate", name, f"{start} → {end} : {price}")

    await attempt(request, db, action, "Tarif saisonnier ajouté.")
    return redirect("/admin/rates")


@router.post("/rates/{rate_id}/delete")
async def rate_delete(request: Request, db: DbSession, user: AdminUser, rate_id: int):
    async def action():
        rate = await db.get(RatePeriod, rate_id)
        if rate is None:
            raise BusinessError("Tarif introuvable.")
        await db.delete(rate)
        await log_action(db, user, "rate_deleted", "rate", rate.name, "")

    await attempt(request, db, action, "Tarif supprimé.")
    return redirect("/admin/rates")


# ---------------------------------------------------------------- modules
@router.get("/modules")
async def modules_page(request: Request, db: DbSession, user: AdminUser):
    flags = (await db.scalars(select(FeatureFlag).order_by(FeatureFlag.key))).all()
    return await render(request, db, "admin/modules.html", flags=flags)


@router.post("/modules")
async def modules_save(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()

    async def action():
        states = []
        for flag in (await db.scalars(select(FeatureFlag))).all():
            flag.enabled = form.get(f"m_{flag.key}") == "1"
            states.append(f"{flag.key}={'on' if flag.enabled else 'off'}")
        await log_action(db, user, "modules_saved", "modules", "", ", ".join(states))

    await attempt(request, db, action, "Modules mis à jour.")
    return redirect("/admin/modules")


# ---------------------------------------------------------------- utilisateurs
@router.get("/users")
async def users_page(request: Request, db: DbSession, user: AdminUser):
    users = (await db.scalars(select(User).order_by(User.username))).all()
    return await render(request, db, "admin/users.html", users=users)


@router.post("/users")
async def user_create(request: Request, db: DbSession, user: AdminUser, username: Annotated[str, Form()] = "",
                      full_name: Annotated[str, Form()] = "", role: Annotated[str, Form()] = "reception",
                      password: Annotated[str, Form()] = ""):
    async def action():
        login = username.strip()
        if not login or not login.replace(".", "").replace("_", "").replace("-", "").isalnum():
            raise BusinessError("Identifiant invalide (lettres, chiffres, . _ -).")
        if role not in ROLES:
            raise BusinessError("Rôle invalide.")
        problems = password_problems(password)
        if problems:
            raise BusinessError("Mot de passe : " + ", ".join(problems) + ".")
        if await db.scalar(select(func.count(User.id)).where(User.username == login)):
            raise BusinessError("Cet identifiant existe déjà.")
        db.add(User(username=login[:64], full_name=full_name.strip()[:120], role=role,
                    password_hash=hash_password(password), must_change_password=True))
        await log_action(db, user, "user_created", "user", login, role)

    await attempt(request, db, action, "Utilisateur créé (changement de mot de passe demandé à la 1re connexion).")
    return redirect("/admin/users")


@router.post("/users/{user_id}")
async def user_update(request: Request, db: DbSession, user: AdminUser, user_id: int,
                      full_name: Annotated[str, Form()] = "", role: Annotated[str, Form()] = "reception",
                      active: Annotated[str, Form()] = "", password: Annotated[str, Form()] = ""):
    async def action():
        target = await db.get(User, user_id)
        if target is None:
            raise BusinessError("Utilisateur introuvable.")
        if target.id == user.id and (not active or role != "admin"):
            raise BusinessError("Vous ne pouvez pas retirer vos propres droits d'administration.")
        if role not in ROLES:
            raise BusinessError("Rôle invalide.")
        target.full_name, target.role, target.active = full_name.strip()[:120], role, bool(active)
        if password:
            problems = password_problems(password)
            if problems:
                raise BusinessError("Mot de passe : " + ", ".join(problems) + ".")
            target.password_hash = hash_password(password)
            target.must_change_password = target.id != user.id
        await log_action(db, user, "user_updated", "user", target.username, f"{role}, actif={bool(active)}")

    await attempt(request, db, action, "Utilisateur mis à jour.")
    return redirect("/admin/users")


# ---------------------------------------------------------------- journal et API
@router.get("/audit-log")
async def audit_log_page(request: Request, db: DbSession, user: AdminUser, action: str = "", username: str = "",
                         page: int = 1):
    page = max(1, page)
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if username:
        stmt = stmt.where(AuditLog.username == username)
    entries = (await db.scalars(stmt.offset((page - 1) * 100).limit(101))).all()
    actions = (await db.scalars(select(AuditLog.action).distinct().order_by(AuditLog.action))).all()
    return await render(request, db, "admin/audit_log.html", entries=entries[:100], has_next=len(entries) > 100,
                        page=page, actions=actions, action=action, username=username)


@router.get("/openapi.json")
async def openapi_schema(request: Request, user: AdminUser):
    return JSONResponse(request.app.openapi())
