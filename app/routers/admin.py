from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import admin_only, flash, redirect_to, render
from app.models.admin import Company, FeatureFlag, ServiceItem, User
from app.models.property import RatePeriod, Room, RoomType
from app.models.reservations import StaySegment
from app.services.auth import hash_password
from app.services.common import BookingError, to_date, to_int
from app.services.settings import DEFAULTS, MODULES, save_setting, setting_groups

router = APIRouter(prefix="/admin")
ROLES = {"admin": "Administrateur", "reception": "Réception", "housekeeping": "Housekeeping (gouvernante)"}


def _run(request: Request, db: Session, action, ok: str) -> None:
    try:
        action()
        db.commit()
        flash(request, ok)
    except BookingError as exc:
        db.rollback()
        flash(request, str(exc), "err")


# ---------- Paramètres ----------
@router.get("")
def settings_page(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    return render(request, db, "admin/settings.html", groups=setting_groups(db))


@router.post("/settings")
async def save_settings(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    form = await request.form()
    for key, (_default, typ, _label, _group) in DEFAULTS.items():
        field = f"s_{key}"
        if typ == "bool":
            save_setting(db, key, "1" if field in form else "0")
        elif field in form:
            save_setting(db, key, str(form[field]))
    db.commit()
    flash(request, "Paramètres enregistrés.")
    return redirect_to("/admin")


# ---------- Chambres & types ----------
@router.get("/rooms")
def rooms_page(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    return render(
        request, db, "admin/rooms.html",
        room_types=db.scalars(select(RoomType).order_by(RoomType.code)).all(),
        rooms=db.scalars(select(Room).order_by(Room.number)).all(),
    )


@router.post("/room-types")
def create_room_type(request: Request, code: str = Form(""), name: str = Form(""), base_price_ttc: str = Form("0"),
                     max_occupancy: str = Form("2"), db: Session = Depends(get_db), user: User = Depends(admin_only)):
    def action():
        if not code.strip() or not name.strip():
            raise BookingError("Code et libellé requis.")
        db.add(RoomType(code=code.strip().upper(), name=name.strip(),
                        base_price_ttc=to_int(base_price_ttc, "Prix", minimum=0),
                        max_occupancy=to_int(max_occupancy, "Capacité", default=2, minimum=1)))
        db.flush()
    _run(request, db, action, "Type de chambre créé.")
    return redirect_to("/admin/rooms")


@router.post("/room-types/{type_id}")
def update_room_type(type_id: int, request: Request, name: str = Form(""), base_price_ttc: str = Form("0"),
                     max_occupancy: str = Form("2"), db: Session = Depends(get_db), user: User = Depends(admin_only)):
    def action():
        rt = db.get(RoomType, type_id)
        if rt is None:
            raise BookingError("Type introuvable.")
        rt.name = name.strip() or rt.name
        rt.base_price_ttc = to_int(base_price_ttc, "Prix", minimum=0)
        rt.max_occupancy = to_int(max_occupancy, "Capacité", default=2, minimum=1)
    _run(request, db, action, "Type de chambre mis à jour.")
    return redirect_to("/admin/rooms")


@router.post("/rooms")
def create_room(request: Request, number: str = Form(""), floor: str = Form("0"), room_type_id: str = Form(""),
                db: Session = Depends(get_db), user: User = Depends(admin_only)):
    def action():
        if not number.strip():
            raise BookingError("Numéro de chambre requis.")
        if db.scalar(select(func.count(Room.id)).where(Room.number == number.strip())):
            raise BookingError("Ce numéro de chambre existe déjà.")
        db.add(Room(number=number.strip(), floor=to_int(floor, "Étage", default=0),
                    room_type_id=to_int(room_type_id, "Type de chambre")))
        db.flush()
    _run(request, db, action, "Chambre ajoutée.")
    return redirect_to("/admin/rooms")


@router.post("/rooms/{room_id}")
def update_room(room_id: int, request: Request, number: str = Form(""), floor: str = Form("0"),
                room_type_id: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db),
                user: User = Depends(admin_only)):
    def action():
        room = db.get(Room, room_id)
        if room is None:
            raise BookingError("Chambre introuvable.")
        if number.strip() and number.strip() != room.number:
            if db.scalar(select(func.count(Room.id)).where(Room.number == number.strip())):
                raise BookingError("Ce numéro de chambre existe déjà.")
            room.number = number.strip()
        room.floor = to_int(floor, "Étage", default=room.floor)
        room.room_type_id = to_int(room_type_id, "Type", default=room.room_type_id)
        room.notes = notes.strip()
    _run(request, db, action, "Chambre mise à jour.")
    return redirect_to("/admin/rooms")


@router.post("/rooms/{room_id}/delete")
def delete_room(room_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    def action():
        room = db.get(Room, room_id)
        if room is None:
            raise BookingError("Chambre introuvable.")
        if db.scalar(select(func.count(StaySegment.id)).where(StaySegment.room_id == room_id)):
            raise BookingError("Chambre déjà utilisée dans des réservations : désactivez-la en « maintenance ».")
        db.delete(room)
    _run(request, db, action, "Chambre supprimée.")
    return redirect_to("/admin/rooms")


# ---------- Tarifs saisonniers ----------
@router.get("/rates")
def rates_page(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    return render(
        request, db, "admin/rates.html",
        periods=db.scalars(select(RatePeriod).order_by(RatePeriod.start_date)).all(),
        room_types=db.scalars(select(RoomType).order_by(RoomType.code)).all(),
    )


@router.post("/rates")
def create_rate(request: Request, name: str = Form(""), start_date: str = Form(""), end_date: str = Form(""),
                room_type_id: str = Form(""), price_ttc: str = Form(""), priority: str = Form("0"),
                db: Session = Depends(get_db), user: User = Depends(admin_only)):
    def action():
        start = to_date(start_date, "Début")
        end = to_date(end_date, "Fin")
        if end <= start:
            raise BookingError("La fin de période doit être postérieure au début.")
        db.add(RatePeriod(
            name=name.strip() or "Tarif saisonnier", start_date=start, end_date=end,
            room_type_id=to_int(room_type_id, "Type de chambre"),
            price_ttc=to_int(price_ttc, "Prix", minimum=0), priority=to_int(priority, "Priorité", default=0),
        ))
    _run(request, db, action, "Tarif ajouté.")
    return redirect_to("/admin/rates")


@router.post("/rates/{rate_id}/delete")
def delete_rate(rate_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    rate = db.get(RatePeriod, rate_id)
    if rate is not None:
        db.delete(rate)
        db.commit()
        flash(request, "Tarif supprimé.")
    return redirect_to("/admin/rates")


# ---------- Modules annexes (feature flags) ----------
@router.get("/modules")
def modules_page(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    flags = db.scalars(select(FeatureFlag).order_by(FeatureFlag.key)).all()
    return render(request, db, "admin/modules.html", flags=flags)


@router.post("/modules")
async def save_modules(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    form = await request.form()
    for flag in db.scalars(select(FeatureFlag)).all():
        flag.enabled = f"m_{flag.key}" in form
    db.commit()
    flash(request, "Modules mis à jour.")
    return redirect_to("/admin/modules")


# ---------- Catalogue des prestations annexes ----------
@router.get("/services")
def services_page(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    return render(
        request, db, "admin/services.html",
        items=db.scalars(select(ServiceItem).order_by(ServiceItem.category, ServiceItem.name)).all(),
        categories={k: v for k, v in MODULES.items()},
    )


@router.post("/services")
def create_service(request: Request, name: str = Form(""), category: str = Form("restaurant"),
                   price_ttc: str = Form("0"), db: Session = Depends(get_db), user: User = Depends(admin_only)):
    def action():
        if not name.strip():
            raise BookingError("Libellé requis.")
        if category not in MODULES:
            raise BookingError("Catégorie invalide.")
        db.add(ServiceItem(name=name.strip(), category=category, price_ttc=to_int(price_ttc, "Prix", minimum=0)))
    _run(request, db, action, "Prestation ajoutée.")
    return redirect_to("/admin/services")


@router.post("/services/{item_id}")
def update_service(item_id: int, request: Request, name: str = Form(""), price_ttc: str = Form("0"),
                   active: str = Form(""), db: Session = Depends(get_db), user: User = Depends(admin_only)):
    item = db.get(ServiceItem, item_id)
    if item is not None:
        item.name = name.strip() or item.name
        item.price_ttc = to_int(price_ttc, "Prix", default=item.price_ttc, minimum=0)
        item.active = bool(active)
        db.commit()
        flash(request, "Prestation mise à jour.")
    return redirect_to("/admin/services")


# ---------- Entreprises ----------
@router.get("/companies")
def companies_page(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    return render(request, db, "admin/companies.html",
                  companies=db.scalars(select(Company).order_by(Company.name)).all())


@router.post("/companies")
def create_company(request: Request, name: str = Form(""), tax_id: str = Form(""), contact: str = Form(""),
                   discount_percent: str = Form("0"), db: Session = Depends(get_db), user: User = Depends(admin_only)):
    def action():
        if not name.strip():
            raise BookingError("Nom de l'entreprise requis.")
        discount = to_int(discount_percent, "Remise", default=0, minimum=0)
        if discount > 100:
            raise BookingError("La remise ne peut pas dépasser 100 %.")
        db.add(Company(name=name.strip(), tax_id=tax_id.strip(), contact=contact.strip(), discount_percent=discount))
    _run(request, db, action, "Entreprise ajoutée.")
    return redirect_to("/admin/companies")


@router.post("/companies/{company_id}")
def update_company(company_id: int, request: Request, name: str = Form(""), tax_id: str = Form(""),
                   contact: str = Form(""), discount_percent: str = Form("0"), db: Session = Depends(get_db),
                   user: User = Depends(admin_only)):
    def action():
        company = db.get(Company, company_id)
        if company is None:
            raise BookingError("Entreprise introuvable.")
        company.name = name.strip() or company.name
        company.tax_id = tax_id.strip()
        company.contact = contact.strip()
        company.discount_percent = to_int(discount_percent, "Remise", default=0, minimum=0)
    _run(request, db, action, "Entreprise mise à jour.")
    return redirect_to("/admin/companies")


# ---------- Utilisateurs ----------
@router.get("/users")
def users_page(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    return render(request, db, "admin/users.html",
                  users=db.scalars(select(User).order_by(User.username)).all(), roles=ROLES)


@router.post("/users")
def create_user(request: Request, username: str = Form(""), full_name: str = Form(""), role: str = Form("reception"),
                password: str = Form(""), db: Session = Depends(get_db), user: User = Depends(admin_only)):
    def action():
        login = username.strip()
        if not login or len(password) < 8:
            raise BookingError("Identifiant requis et mot de passe d'au moins 8 caractères.")
        if role not in ROLES:
            raise BookingError("Rôle invalide.")
        if db.scalar(select(func.count(User.id)).where(User.username == login)):
            raise BookingError("Cet identifiant existe déjà.")
        db.add(User(username=login, full_name=full_name.strip(), role=role, password_hash=hash_password(password)))
    _run(request, db, action, "Utilisateur créé.")
    return redirect_to("/admin/users")


@router.post("/users/{user_id}")
def update_user(user_id: int, request: Request, full_name: str = Form(""), role: str = Form("reception"),
                active: str = Form(""), password: str = Form(""), db: Session = Depends(get_db),
                user: User = Depends(admin_only)):
    def action():
        target = db.get(User, user_id)
        if target is None:
            raise BookingError("Utilisateur introuvable.")
        if target.id == user.id and (not active or role != "admin"):
            raise BookingError("Vous ne pouvez pas retirer vos propres droits d'administration.")
        target.full_name = full_name.strip()
        target.role = role if role in ROLES else target.role
        target.active = bool(active)
        if password:
            if len(password) < 8:
                raise BookingError("Mot de passe : 8 caractères minimum.")
            target.password_hash = hash_password(password)
    _run(request, db, action, "Utilisateur mis à jour.")
    return redirect_to("/admin/users")
