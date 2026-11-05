"""CRM : carnet clients, doublons, VIP / liste noire, impayés, entreprises."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select

from app.deps import AdminUser, DbSession, StaffUser, attempt, redirect
from app.models.booking import Reservation
from app.models.customer import GUEST_STATUSES, Company, Guest
from app.services import crm
from app.services.common import BusinessError, like_pattern, to_date, to_int
from app.services.folio import build_folio
from app.services.loaders import load_reservations
from app.services.settings import load_hotel_settings
from app.templating import flash, render

router = APIRouter()


def _guest_form(form) -> dict:
    get = lambda key: str(form.get(key) or "").strip()  # noqa: E731
    company_id = get("company_id")
    return {
        "first_name": get("first_name"), "last_name": get("last_name"),
        "first_name_ar": get("first_name_ar"), "last_name_ar": get("last_name_ar"),
        "gender": get("gender"), "birth_date": to_date(get("birth_date"), "Date de naissance", required=False),
        "birth_place": get("birth_place"), "nationality": get("nationality"), "profession": get("profession"),
        "phone": get("phone"), "email": get("email"), "address": get("address"),
        "id_type": get("id_type") or "cni", "id_number": get("id_number"),
        "id_issue_date": to_date(get("id_issue_date"), "Date de délivrance", required=False),
        "id_issue_place": get("id_issue_place"),
        "id_expiry_date": to_date(get("id_expiry_date"), "Date d'expiration", required=False),
        "company_id": int(company_id) if company_id.isdigit() else None,
        "preferences": get("preferences"), "notes": get("notes"),
    }


async def _guest(db, guest_id: int) -> Guest:
    guest = await db.get(Guest, guest_id)
    if guest is None:
        raise HTTPException(status_code=404, detail="Client introuvable.")
    return guest


async def _companies(db):
    return (await db.scalars(select(Company).order_by(Company.name))).all()


@router.get("/guests")
async def guests_list(request: Request, db: DbSession, user: StaffUser, q: str = "", status: str = ""):
    term = q.strip()
    if term:
        result = await crm.search(db, term)
        if result.redirect and result.redirect.startswith("/guests/"):
            return redirect(result.redirect)  # pièce scannée : ouverture directe de la fiche
    stmt = select(Guest).order_by(Guest.last_name, Guest.first_name).limit(300)
    if term:
        pattern = like_pattern(term)
        stmt = stmt.where(or_(
            Guest.last_name.ilike(pattern, escape="\\"), Guest.first_name.ilike(pattern, escape="\\"),
            Guest.last_name_ar.like(pattern, escape="\\"), Guest.first_name_ar.like(pattern, escape="\\"),
            Guest.phone.like(pattern, escape="\\"), Guest.id_number.ilike(pattern, escape="\\"),
            Guest.email.ilike(pattern, escape="\\")))
    if status in GUEST_STATUSES:
        stmt = stmt.where(Guest.status == status)
    guests = (await db.scalars(stmt)).unique().all()
    return await render(request, db, "crm/guests.html", guests=guests, q=q, status=status)


@router.get("/guests/new")
async def guest_new(request: Request, db: DbSession, user: StaffUser):
    return await render(request, db, "crm/guest_form.html", guest=None, companies=await _companies(db))


@router.post("/guests")
async def guest_create(request: Request, db: DbSession, user: StaffUser):
    form = await request.form()
    hs = await load_hotel_settings(db)
    try:
        guest = await crm.save_guest(db, None, _guest_form(form), hs, user)
        await db.commit()
    except BusinessError as exc:
        await db.rollback()
        flash(request, str(exc), "error")
        return redirect("/guests/new")
    flash(request, "Fiche client créée.")
    return redirect(f"/guests/{guest.id}")


@router.get("/api/guests/check-identity")
async def guest_check_identity(db: DbSession, user: StaffUser, id_type: str = "cni", id_number: str = "",
                               exclude: int | None = None):
    duplicate = await crm.find_duplicate(db, id_type, id_number, exclude_id=exclude)
    if duplicate is None:
        return JSONResponse({"duplicate": None})
    return JSONResponse({"duplicate": {"id": duplicate.id, "name": duplicate.full_name, "status": duplicate.status}})


@router.get("/guests/duplicates")
async def guest_duplicates(request: Request, db: DbSession, user: StaffUser):
    return await render(request, db, "crm/duplicates.html", groups=await crm.potential_duplicates(db))


@router.post("/guests/merge")
async def guest_merge(request: Request, db: DbSession, user: AdminUser, keep_id: Annotated[str, Form()] = "",
                      remove_id: Annotated[str, Form()] = ""):
    hs = await load_hotel_settings(db)

    async def action():
        keep = await db.get(Guest, to_int(keep_id, "Fiche conservée"))
        remove = await db.get(Guest, to_int(remove_id, "Fiche fusionnée"))
        if keep is None or remove is None:
            raise BusinessError("Fiche introuvable.")
        await crm.merge_guests(db, keep, remove, hs, user)

    if await attempt(request, db, action, "Fiches fusionnées."):
        return redirect(f"/guests/{keep_id}")
    return redirect("/guests/duplicates")


@router.get("/guests/unpaid")
async def guests_unpaid(request: Request, db: DbSession, user: StaffUser):
    hs = await load_hotel_settings(db)
    rows = await crm.unpaid_reservations(db, hs)
    return await render(request, db, "crm/unpaid.html", rows=rows, total=sum(b for _, b in rows))


@router.get("/guests/{guest_id}")
async def guest_detail(request: Request, db: DbSession, user: StaffUser, guest_id: int):
    guest = await _guest(db, guest_id)
    hs = await load_hotel_settings(db)
    stays = await load_reservations(db, Reservation.guest_id == guest.id, order_by=Reservation.check_in.desc())
    spent = nights = 0
    for stay in stays:
        if stay.status in ("checked_in", "checked_out"):
            folio = build_folio(stay, hs, None)
            spent += folio.total_ttc
            nights += stay.nights
    return await render(request, db, "crm/guest_detail.html", guest=guest, stays=stays, spent=spent, nights=nights,
                        alerts=await crm.guest_alerts(db, guest, hs), companies=await _companies(db))


@router.post("/guests/{guest_id}")
async def guest_update(request: Request, db: DbSession, user: StaffUser, guest_id: int):
    guest = await _guest(db, guest_id)
    hs = await load_hotel_settings(db)
    form = await request.form()

    async def action():
        await crm.save_guest(db, guest, _guest_form(form), hs, user)

    await attempt(request, db, action, "Fiche client mise à jour.")
    return redirect(f"/guests/{guest_id}")


@router.post("/guests/{guest_id}/status")
async def guest_status(request: Request, db: DbSession, user: StaffUser, guest_id: int,
                       status: Annotated[str, Form()] = "normal", reason: Annotated[str, Form()] = ""):
    guest = await _guest(db, guest_id)
    hs = await load_hotel_settings(db)
    await attempt(request, db, lambda: crm.set_guest_status(db, guest, status, reason, user, hs),
                  f"Statut client : {GUEST_STATUSES.get(status, status)}.")
    return redirect(f"/guests/{guest_id}")


# ---------------------------------------------------------------- entreprises
@router.get("/companies")
async def companies_page(request: Request, db: DbSession, user: StaffUser):
    return await render(request, db, "crm/companies.html", companies=await _companies(db))


def _company_values(form) -> dict:
    get = lambda key: str(form.get(key) or "").strip()  # noqa: E731
    if not get("name"):
        raise BusinessError("Raison sociale requise.")
    return {"name": get("name"), "nif": get("nif"), "rc": get("rc"), "address": get("address"), "phone": get("phone"),
            "email": get("email"),
            "discount_percent": to_int(get("discount_percent"), "Remise", default=0, minimum=0, maximum=100)}


@router.post("/companies")
async def company_create(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()

    async def action():
        db.add(Company(**_company_values(form)))

    await attempt(request, db, action, "Entreprise ajoutée.")
    return redirect("/companies")


@router.post("/companies/{company_id}")
async def company_update(request: Request, db: DbSession, user: AdminUser, company_id: int):
    form = await request.form()

    async def action():
        company = await db.get(Company, company_id)
        if company is None:
            raise BusinessError("Entreprise introuvable.")
        for key, value in _company_values(form).items():
            setattr(company, key, value)

    await attempt(request, db, action, "Entreprise mise à jour.")
    return redirect("/companies")
