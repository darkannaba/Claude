from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import flash, parse_date, redirect_to, render, staff
from app.models.admin import Company, ServiceItem, User
from app.models.guests import Guest
from app.models.property import Room, RoomType
from app.models.reservations import (
    ACTIVE_STATUSES,
    CHARGE_CATEGORIES,
    PAYMENT_KINDS,
    PAYMENT_METHODS,
    Charge,
    Companion,
    Invoice,
    Payment,
    Reservation,
)
from app.services import availability as av
from app.services.common import BookingError, to_date, to_int
from app.services.folio import compute_folio
from app.services.pdf import invoice_pdf, police_form_pdf
from app.services.settings import enabled_modules, load_settings, now_of, today_of

router = APIRouter()


def _load(db: Session, res_id: int) -> Reservation:
    res = db.get(Reservation, res_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Réservation introuvable.")
    return res


def _attempt(request: Request, db: Session, action, ok_message: str) -> None:
    """Exécute une action métier, commit si succès, rollback + message si règle violée."""
    try:
        action()
        db.commit()
        flash(request, ok_message)
    except BookingError as exc:
        db.rollback()
        flash(request, str(exc), "err")


def _back(res_id: int):
    return redirect_to(f"/reservations/{res_id}")


@router.get("/reservations")
def list_reservations(request: Request, q: str = "", status: str = "", db: Session = Depends(get_db),
                      user: User = Depends(staff)):
    stmt = (
        select(Reservation).join(Guest, Reservation.guest_id == Guest.id)
        .order_by(Reservation.check_in.desc(), Reservation.id.desc()).limit(300)
    )
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            Reservation.code.ilike(like), Guest.last_name.ilike(like), Guest.first_name.ilike(like),
            Guest.phone.ilike(like), Guest.id_number.ilike(like),
        ))
    if status:
        stmt = stmt.where(Reservation.status == status)
    rows = db.scalars(stmt).unique().all()
    return render(request, db, "reservations/list.html", rows=rows, q=q, status=status)


@router.get("/reservations/new")
def new_reservation_form(request: Request, check_in: str = "", check_out: str = "", room_type_id: str = "",
                         guest_id: str = "", db: Session = Depends(get_db), user: User = Depends(staff)):
    settings = load_settings(db)
    today = today_of(settings)
    ci = parse_date(check_in) or today
    co = parse_date(check_out) or ci + timedelta(days=1)
    rt_id = int(room_type_id) if room_type_id.isdigit() else None
    rooms = av.free_rooms(db, ci, co, rt_id) if co > ci else []
    guest = db.get(Guest, int(guest_id)) if guest_id.isdigit() else None
    return render(
        request, db, "reservations/new.html",
        ci=ci, co=co, rt_id=rt_id, rooms=rooms, guest=guest,
        room_types=db.scalars(select(RoomType).order_by(RoomType.code)).all(),
        companies=db.scalars(select(Company).order_by(Company.name)).all(),
    )


@router.post("/reservations")
def create_reservation(
    request: Request,
    guest_id: str = Form(""),
    first_name: str = Form(""),
    last_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    nationality: str = Form(""),
    id_type: str = Form("cni"),
    id_number: str = Form(""),
    room_id: str = Form(""),
    check_in: str = Form(""),
    check_out: str = Form(""),
    adults: str = Form("1"),
    children: str = Form("0"),
    company_id: str = Form(""),
    channel: str = Form("Direct"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(staff),
):
    settings = load_settings(db)
    back_query = urlencode({"check_in": check_in, "check_out": check_out})
    try:
        ci = to_date(check_in, "Date d'arrivée")
        co = to_date(check_out, "Date de départ")
        room = db.get(Room, to_int(room_id, "Chambre")) if room_id.strip() else None
        if room is None:
            raise BookingError("Choisissez une chambre disponible.")

        guest = db.get(Guest, int(guest_id)) if guest_id.strip().isdigit() else None
        if guest is None:
            if not first_name.strip() or not last_name.strip():
                raise BookingError("Sélectionnez un client existant ou saisissez son nom et prénom.")
            guest = Guest(
                first_name=first_name.strip(), last_name=last_name.strip(), phone=phone.strip(),
                email=email.strip(), nationality=nationality.strip(), id_type=id_type or "cni",
                id_number=id_number.strip(), created_at=now_of(settings),
            )
            db.add(guest)
            db.flush()

        company = db.get(Company, int(company_id)) if company_id.strip().isdigit() else None
        res = av.create_reservation(
            db, guest=guest, room=room, check_in=ci, check_out=co,
            adults=to_int(adults, "Nombre d'adultes", default=1, minimum=1),
            children=to_int(children, "Nombre d'enfants", default=0, minimum=0),
            company_id=company.id if company else None, channel=channel.strip(), notes=notes.strip(),
            now=now_of(settings),
        )
        db.commit()
        flash(request, f"Réservation {res.code} créée.")
        return redirect_to(f"/reservations/{res.id}")
    except BookingError as exc:
        db.rollback()
        flash(request, str(exc), "err")
        return redirect_to(f"/reservations/new?{back_query}")


@router.get("/reservations/{res_id}")
def reservation_detail(res_id: int, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(staff)):
    res = _load(db, res_id)
    settings = load_settings(db)
    today = today_of(settings)
    folio = compute_folio(db, res, settings)

    move_from = max(today, res.check_in)
    rooms_for_move = []
    if res.status in ACTIVE_STATUSES and move_from < res.check_out:
        candidates = db.scalars(select(Room).order_by(Room.number)).all()
        rooms_for_move = [
            r for r in candidates
            if r.id != res.room_id and av.room_is_free(db, r.id, move_from, res.check_out, res.id)
        ]

    modules = enabled_modules(db)
    service_items = []
    if modules:
        service_items = db.scalars(
            select(ServiceItem)
            .where(ServiceItem.active.is_(True), ServiceItem.category.in_(list(modules)))
            .order_by(ServiceItem.category, ServiceItem.name)
        ).all()

    guest_history = db.scalars(
        select(Reservation)
        .where(Reservation.guest_id == res.guest_id, Reservation.id != res.id)
        .order_by(Reservation.check_in.desc()).limit(10)
    ).all()

    return render(
        request, db, "reservations/detail.html",
        res=res, folio=folio, move_from=move_from, rooms_for_move=rooms_for_move,
        all_rooms=db.scalars(select(Room).order_by(Room.number)).all(),
        modules=modules, service_items=service_items, guest_history=guest_history,
    )


@router.post("/reservations/{res_id}/dates")
def change_dates(res_id: int, request: Request, check_in: str = Form(""), check_out: str = Form(""),
                 room_id: str = Form(""), db: Session = Depends(get_db), user: User = Depends(staff)):
    res = _load(db, res_id)

    def action():
        room = db.get(Room, to_int(room_id, "Chambre"))
        if room is None:
            raise BookingError("Chambre introuvable.")
        av.reassign_dates(db, res, room, to_date(check_in, "Arrivée"), to_date(check_out, "Départ"))

    _attempt(request, db, action, "Dates et chambre mises à jour.")
    return _back(res_id)


@router.post("/reservations/{res_id}/checkin")
def check_in(res_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(staff)):
    res = _load(db, res_id)
    settings = load_settings(db)
    _attempt(request, db, lambda: av.check_in(db, res, settings, now_of(settings)),
             f"Arrivée enregistrée pour {res.code}.")
    return _back(res_id)


@router.post("/reservations/{res_id}/checkout")
def check_out(res_id: int, request: Request, force: str = Form(""), db: Session = Depends(get_db),
              user: User = Depends(staff)):
    res = _load(db, res_id)
    settings = load_settings(db)
    _attempt(request, db, lambda: av.check_out(db, res, settings, now_of(settings), force=bool(force)),
             f"Départ enregistré pour {res.code}. Chambre passée à « À nettoyer ».")
    return _back(res_id)


@router.post("/reservations/{res_id}/room")
def move_room(res_id: int, request: Request, new_room_id: str = Form(""), from_date: str = Form(""),
              db: Session = Depends(get_db), user: User = Depends(staff)):
    res = _load(db, res_id)
    settings = load_settings(db)

    def action():
        room = db.get(Room, to_int(new_room_id, "Chambre"))
        if room is None:
            raise BookingError("Chambre introuvable.")
        start = to_date(from_date, "Date de changement", required=False) or max(today_of(settings), res.check_in)
        av.change_room(db, res, room, start, settings)

    _attempt(request, db, action, "Changement de chambre effectué.")
    return _back(res_id)


@router.post("/reservations/{res_id}/cancel")
def cancel(res_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(staff)):
    res = _load(db, res_id)
    settings = load_settings(db)
    _attempt(request, db, lambda: av.cancel(res, now_of(settings)), f"Réservation {res.code} annulée.")
    return _back(res_id)


@router.post("/reservations/{res_id}/noshow")
def no_show(res_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(staff)):
    res = _load(db, res_id)
    settings = load_settings(db)
    _attempt(request, db, lambda: av.mark_no_show(res, settings), f"{res.code} marquée no-show.")
    return _back(res_id)


@router.post("/reservations/{res_id}/payments")
def add_payment(res_id: int, request: Request, amount: str = Form(""), kind: str = Form("paiement"),
                method: str = Form("especes"), note: str = Form(""), db: Session = Depends(get_db),
                user: User = Depends(staff)):
    res = _load(db, res_id)
    settings = load_settings(db)

    def action():
        if res.status in ("cancelled", "no_show"):
            raise BookingError("Encaissement impossible sur une réservation annulée ou no-show.")
        value = to_int(amount, "Montant", minimum=1)
        if kind not in PAYMENT_KINDS:
            raise BookingError("Type de versement invalide.")
        if method not in PAYMENT_METHODS:
            raise BookingError("Mode de règlement invalide.")
        db.add(Payment(reservation_id=res.id, amount=value, kind=kind, method=method,
                       paid_at=now_of(settings), note=note.strip()[:200]))

    _attempt(request, db, action, "Versement enregistré.")
    return _back(res_id)


@router.post("/reservations/{res_id}/charges")
def add_charge(res_id: int, request: Request, label: str = Form(""), amount: str = Form(""),
               category: str = Form("autre"), service_item_id: str = Form(""),
               db: Session = Depends(get_db), user: User = Depends(staff)):
    res = _load(db, res_id)
    settings = load_settings(db)

    def action():
        if res.status not in ACTIVE_STATUSES:
            raise BookingError("Prestations possibles uniquement pour un séjour actif.")
        modules = enabled_modules(db)
        if service_item_id.strip().isdigit():
            item = db.get(ServiceItem, int(service_item_id))
            if item is None or not item.active:
                raise BookingError("Prestation introuvable ou inactive.")
            name, value, cat = item.name, item.price_ttc, item.category
        else:
            name = label.strip()
            if not name:
                raise BookingError("Libellé requis.")
            value = to_int(amount, "Montant", minimum=1)
            cat = category if category in CHARGE_CATEGORIES else "autre"
        if cat != "autre" and cat not in modules:
            raise BookingError(f"Le module « {CHARGE_CATEGORIES.get(cat, cat)} » est désactivé.")
        db.add(Charge(reservation_id=res.id, label=name[:160], category=cat, amount_ttc=value,
                      posted_at=now_of(settings)))

    _attempt(request, db, action, "Prestation ajoutée au folio.")
    return _back(res_id)


@router.post("/reservations/{res_id}/charges/{charge_id}/delete")
def delete_charge(res_id: int, charge_id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(staff)):
    res = _load(db, res_id)

    def action():
        charge = db.get(Charge, charge_id)
        if charge is None or charge.reservation_id != res.id:
            raise BookingError("Prestation introuvable.")
        if res.status not in ACTIVE_STATUSES:
            raise BookingError("Prestation non supprimable pour ce statut.")
        db.delete(charge)

    _attempt(request, db, action, "Prestation supprimée.")
    return _back(res_id)


@router.post("/reservations/{res_id}/companions")
def add_companion(res_id: int, request: Request, first_name: str = Form(""), last_name: str = Form(""),
                  birth_date: str = Form(""), nationality: str = Form(""), id_type: str = Form("cni"),
                  id_number: str = Form(""), db: Session = Depends(get_db), user: User = Depends(staff)):
    res = _load(db, res_id)

    def action():
        if not first_name.strip() or not last_name.strip():
            raise BookingError("Nom et prénom du compagnon requis.")
        db.add(Companion(
            reservation_id=res.id, first_name=first_name.strip(), last_name=last_name.strip(),
            birth_date=to_date(birth_date, "Date de naissance", required=False),
            nationality=nationality.strip(), id_type=id_type or "cni", id_number=id_number.strip(),
        ))

    _attempt(request, db, action, "Compagnon ajouté.")
    return _back(res_id)


@router.post("/reservations/{res_id}/companions/{companion_id}/delete")
def delete_companion(res_id: int, companion_id: int, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(staff)):
    res = _load(db, res_id)
    companion = db.get(Companion, companion_id)
    if companion is not None and companion.reservation_id == res.id:
        db.delete(companion)
        db.commit()
        flash(request, "Compagnon retiré.")
    return _back(res_id)


@router.get("/reservations/{res_id}/police.pdf")
def police_form(res_id: int, db: Session = Depends(get_db), user: User = Depends(staff)):
    res = _load(db, res_id)
    settings = load_settings(db)
    pdf = police_form_pdf(settings, res, res.guest, res.companions, res.room)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="fiche-police-{res.code}.pdf"'})


@router.get("/reservations/{res_id}/facture.pdf")
def invoice_document(res_id: int, format: str = "a4", db: Session = Depends(get_db),
                     user: User = Depends(staff)):
    res = _load(db, res_id)
    if res.status in ("cancelled", "no_show"):
        raise HTTPException(status_code=400, detail="Aucune facture pour une réservation annulée.")
    settings = load_settings(db)
    if res.invoices:
        number = res.invoices[-1].number
    else:
        invoice = Invoice(reservation_id=res.id, issued_at=now_of(settings))
        db.add(invoice)
        db.flush()
        invoice.number = f"{settings['invoice_prefix']}-{invoice.issued_at:%Y}-{invoice.id:05d}"
        db.commit()
        number = invoice.number
    folio = compute_folio(db, res, settings)
    fmt = "ticket" if format == "ticket" else "a4"
    pdf = invoice_pdf(settings, res, folio, number, fmt)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="facture-{number}-{fmt}.pdf"'})
