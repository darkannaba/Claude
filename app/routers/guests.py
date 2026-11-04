from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import flash, redirect_to, render, staff
from app.models.admin import Company, User
from app.models.guests import ID_TYPES, Guest
from app.models.reservations import Reservation
from app.services.common import BookingError, to_date
from app.services.folio import compute_folio
from app.services.settings import load_settings, now_of

router = APIRouter()


def _form_values(first_name, last_name, phone, email, address, nationality, id_type, id_number,
                 birth_date, company_id, preferences, notes) -> dict:
    if not first_name.strip() or not last_name.strip():
        raise BookingError("Nom et prénom requis.")
    return dict(
        first_name=first_name.strip(), last_name=last_name.strip(), phone=phone.strip(),
        email=email.strip(), address=address.strip(), nationality=nationality.strip(),
        id_type=id_type if id_type in ID_TYPES else "cni", id_number=id_number.strip(),
        birth_date=to_date(birth_date, "Date de naissance", required=False),
        company_id=int(company_id) if company_id.strip().isdigit() else None,
        preferences=preferences.strip(), notes=notes.strip(),
    )


@router.get("/guests")
def list_guests(request: Request, q: str = "", db: Session = Depends(get_db), user: User = Depends(staff)):
    term = q.strip()
    if term:
        # Lecteur de code-barres / scanner USB : un numéro de pièce exact ouvre directement la fiche.
        exact = db.scalars(select(Guest).where(Guest.id_number == term)).all()
        if len(exact) == 1:
            return redirect_to(f"/guests/{exact[0].id}")
    stmt = select(Guest).order_by(Guest.last_name, Guest.first_name).limit(200)
    if term:
        like = f"%{term}%"
        stmt = stmt.where(or_(Guest.last_name.ilike(like), Guest.first_name.ilike(like), Guest.phone.ilike(like),
                              Guest.id_number.ilike(like), Guest.email.ilike(like)))
    return render(request, db, "guests/list.html", guests=db.scalars(stmt).unique().all(), q=q)


@router.get("/api/guests/search")
def api_search_guests(q: str = "", db: Session = Depends(get_db), user: User = Depends(staff)):
    term = q.strip()
    if len(term) < 1:
        return []
    like = f"%{term}%"
    matches = db.scalars(
        select(Guest).where(or_(Guest.id_number == term, Guest.phone.ilike(like), Guest.last_name.ilike(like),
                                Guest.first_name.ilike(like), Guest.id_number.ilike(like)))
        .order_by(Guest.last_name).limit(15)
    ).unique().all()
    matches.sort(key=lambda g: g.id_number != term)  # correspondance exacte en tête
    return [
        {"id": g.id, "label": f"{g.full_name} – {g.phone or 'sans tél.'} – {ID_TYPES.get(g.id_type, '')} {g.id_number}".strip(),
         "nationality": g.nationality}
        for g in matches
    ]


@router.get("/guests/new")
def new_guest_form(request: Request, db: Session = Depends(get_db), user: User = Depends(staff)):
    return render(request, db, "guests/form.html", guest=None, id_types=ID_TYPES,
                  companies=db.scalars(select(Company).order_by(Company.name)).all())


@router.post("/guests")
def create_guest(
    request: Request,
    first_name: str = Form(""), last_name: str = Form(""), phone: str = Form(""), email: str = Form(""),
    address: str = Form(""), nationality: str = Form(""), id_type: str = Form("cni"), id_number: str = Form(""),
    birth_date: str = Form(""), company_id: str = Form(""), preferences: str = Form(""), notes: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(staff),
):
    settings = load_settings(db)
    try:
        values = _form_values(first_name, last_name, phone, email, address, nationality, id_type, id_number,
                              birth_date, company_id, preferences, notes)
        guest = Guest(**values, created_at=now_of(settings))
        db.add(guest)
        db.commit()
        flash(request, "Fiche client créée.")
        return redirect_to(f"/guests/{guest.id}")
    except BookingError as exc:
        db.rollback()
        flash(request, str(exc), "err")
        return redirect_to("/guests/new")


@router.get("/guests/{guest_id}")
def guest_detail(guest_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(staff)):
    guest = db.get(Guest, guest_id)
    if guest is None:
        raise HTTPException(status_code=404, detail="Client introuvable.")
    settings = load_settings(db)
    stays = db.scalars(select(Reservation).where(Reservation.guest_id == guest.id)
                       .order_by(Reservation.check_in.desc())).unique().all()
    total_nights = sum(r.nights for r in stays if r.status in ("checked_in", "checked_out"))
    total_spent = 0
    for stay in stays:
        if stay.status in ("checked_in", "checked_out"):
            total_spent += compute_folio(db, stay, settings).total_due
    return render(request, db, "guests/detail.html", guest=guest, stays=stays,
                  total_nights=total_nights, total_spent=total_spent,
                  companies=db.scalars(select(Company).order_by(Company.name)).all(), id_types=ID_TYPES)


@router.post("/guests/{guest_id}")
def update_guest(
    guest_id: int, request: Request,
    first_name: str = Form(""), last_name: str = Form(""), phone: str = Form(""), email: str = Form(""),
    address: str = Form(""), nationality: str = Form(""), id_type: str = Form("cni"), id_number: str = Form(""),
    birth_date: str = Form(""), company_id: str = Form(""), preferences: str = Form(""), notes: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(staff),
):
    guest = db.get(Guest, guest_id)
    if guest is None:
        raise HTTPException(status_code=404, detail="Client introuvable.")
    try:
        for key, value in _form_values(first_name, last_name, phone, email, address, nationality, id_type,
                                       id_number, birth_date, company_id, preferences, notes).items():
            setattr(guest, key, value)
        db.commit()
        flash(request, "Fiche client mise à jour.")
    except BookingError as exc:
        db.rollback()
        flash(request, str(exc), "err")
    return redirect_to(f"/guests/{guest_id}")
