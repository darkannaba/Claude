"""PMS : tableau de bord, planning, réservations, séjour, folio, documents, recherche, audit de nuit, factures."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload

from app.deps import AnyUser, DbSession, StaffUser, attempt, redirect, safe_next
from app.models.audit import AuditLog, NightAuditRun
from app.models.booking import RES_STATUSES, Reservation, StaySegment
from app.models.customer import ID_TYPES, Company, Guest
from app.models.invoice import Invoice
from app.models.pos import PosItem, PosOrder
from app.models.room import Room, RoomType
from app.services import booking, crm, night_audit, pdf, printing, reports, theme_manager
from app.services.common import BusinessError, like_pattern, parse_date, to_date, to_int
from app.services.folio import build_folio
from app.services.invoicing import invoice_view
from app.services.loaders import load_reservation, load_reservations
from app.services.pricing import load_calendar
from app.services.qr import qr_svg
from app.services.settings import load_hotel_settings, now_of, today_of
from app.templating import flash, render

router = APIRouter()


async def _reservation(db, res_id: int) -> Reservation:
    res = await load_reservation(db, res_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Réservation introuvable.")
    return res


async def _active_rooms(db) -> list[Room]:
    return list((await db.scalars(select(Room).where(Room.active.is_(True)).order_by(Room.number))).unique().all())


def _back(res_id: int):
    return redirect(f"/reservations/{res_id}")


def _guest_data(form, prefix: str = "") -> dict:
    get = lambda key: str(form.get(prefix + key) or "").strip()  # noqa: E731
    return {
        "first_name": get("first_name"), "last_name": get("last_name"),
        "first_name_ar": get("first_name_ar"), "last_name_ar": get("last_name_ar"),
        "phone": get("phone"), "email": get("email"), "nationality": get("nationality"),
        "id_type": get("id_type") or "cni", "id_number": get("id_number"),
        "birth_date": to_date(get("birth_date"), "Date de naissance", required=False),
        "birth_place": get("birth_place"), "profession": get("profession"), "address": get("address"),
        "gender": get("gender"), "id_issue_date": None, "id_issue_place": "", "company_id": None,
        "preferences": "", "notes": "",
    }


# ---------------------------------------------------------------- tableau de bord
@router.get("/")
async def dashboard(request: Request, db: DbSession, user: AnyUser):
    if user.role == "housekeeping":
        return redirect("/housekeeping")
    hs = await load_hotel_settings(db)
    today = today_of(hs)
    rooms = await _active_rooms(db)
    in_house = await load_reservations(db, Reservation.status == "checked_in", order_by=Reservation.check_out)
    occupant = {r.room_id: r for r in in_house}
    arrivals = await load_reservations(db, Reservation.status == "confirmed", Reservation.check_in <= today,
                                       order_by=Reservation.check_in)
    departures = [r for r in in_house if r.check_out <= today]
    counts = Counter(room.status for room in rooms)
    occupancy = round(100 * counts.get("occupied", 0) / len(rooms)) if rooms else 0
    month_kpi = await reports.kpis(db, today.replace(day=1), today + timedelta(days=1))
    payments_today = await reports.payments_by_method(db, today, today + timedelta(days=1))
    series = await reports.revenue_series(db, today - timedelta(days=13), today + timedelta(days=1), "day")
    pending = await night_audit.pending_audit_dates(db, hs)
    low_stock = list((await db.scalars(select(PosItem).where(
        PosItem.active.is_(True), PosItem.track_stock.is_(True), PosItem.stock_qty <= PosItem.low_stock_threshold))).all())
    return await render(
        request, db, "pms/dashboard.html", rooms=rooms, occupant=occupant, in_house=in_house, arrivals=arrivals,
        departures=departures, counts=counts, occupancy=occupancy, month_kpi=month_kpi,
        cash_today=sum(payments_today.values()), payments_today=payments_today, pending=pending,
        low_stock=low_stock, chart={"labels": series["labels"], "room": series["room"], "services": series["services"]},
    )


# ---------------------------------------------------------------- planning
@router.get("/planning")
async def planning(request: Request, db: DbSession, user: StaffUser, start: str = "", days: int = 14,
                   room_type_id: str = "", floor: str = ""):
    hs = await load_hotel_settings(db)
    today = today_of(hs)
    start_day = parse_date(start) or today - timedelta(days=2)
    days = days if days in (7, 14, 21, 31) else 14
    window = [start_day + timedelta(days=i) for i in range(days)]
    end_day = start_day + timedelta(days=days)
    stmt = select(Room).where(Room.active.is_(True)).order_by(Room.number)
    rt_id = int(room_type_id) if room_type_id.isdigit() else None
    floor_value = int(floor) if floor.lstrip("-").isdigit() else None
    if rt_id:
        stmt = stmt.where(Room.room_type_id == rt_id)
    if floor_value is not None:
        stmt = stmt.where(Room.floor == floor_value)
    rooms = list((await db.scalars(stmt)).unique().all())
    segments = (await db.scalars(
        select(StaySegment).join(Reservation, StaySegment.reservation_id == Reservation.id)
        .where(StaySegment.start_date < end_day, StaySegment.end_date > start_day,
               Reservation.status.in_(("confirmed", "checked_in", "checked_out")))
        .options(joinedload(StaySegment.reservation).joinedload(Reservation.guest), joinedload(StaySegment.room))
    )).unique().all()
    by_room: dict[int, list[StaySegment]] = defaultdict(list)
    for segment in segments:
        by_room[segment.room_id].append(segment)

    rows = []
    for room in rooms:
        room_segments = by_room.get(room.id, [])

        def on(day: date):
            return next((s for s in room_segments if s.start_date <= day < s.end_date), None)

        cells, i = [], 0
        while i < days:
            segment = on(window[i])
            if segment is None:
                cells.append({"kind": "free", "span": 1, "day": window[i], "next": window[i] + timedelta(days=1)})
                i += 1
                continue
            j = i + 1
            while j < days and on(window[j]) is segment:
                j += 1
            cells.append({"kind": "bar", "span": j - i, "seg": segment, "res": segment.reservation,
                          "starts": segment.start_date >= start_day, "ends": segment.end_date <= end_day})
            i = j
        rows.append({"room": room, "cells": cells})

    all_rooms = await _active_rooms(db)
    return await render(
        request, db, "pms/planning.html", rows=rows, window=window, days=days, start_day=start_day,
        prev_start=start_day - timedelta(days=days), next_start=start_day + timedelta(days=days),
        room_types=(await db.scalars(select(RoomType).order_by(RoomType.code))).all(),
        floors=sorted({r.floor for r in all_rooms}), rt_id=rt_id, floor_value=floor_value,
    )


# ---------------------------------------------------------------- réservations
@router.get("/reservations")
async def reservations_list(request: Request, db: DbSession, user: StaffUser, q: str = "", status: str = "",
                            start: str = "", end: str = ""):
    criteria = []
    if status in RES_STATUSES:
        criteria.append(Reservation.status == status)
    d_start, d_end = parse_date(start), parse_date(end)
    if d_start:
        criteria.append(Reservation.check_out > d_start)
    if d_end:
        criteria.append(Reservation.check_in <= d_end)
    if q.strip():
        pattern = like_pattern(q.strip())
        guest_ids = select(Guest.id).where(or_(
            Guest.last_name.ilike(pattern, escape="\\"), Guest.first_name.ilike(pattern, escape="\\"),
            Guest.phone.like(pattern, escape="\\"), Guest.id_number.ilike(pattern, escape="\\")))
        criteria.append(or_(Reservation.code.ilike(pattern, escape="\\"), Reservation.guest_id.in_(guest_ids)))
    rows = await load_reservations(db, *criteria, order_by=(Reservation.check_in.desc(), Reservation.id.desc()), limit=200)
    return await render(request, db, "pms/reservations.html", rows=rows, q=q, status=status, start=start, end=end)


@router.get("/reservations/new")
async def reservation_form(request: Request, db: DbSession, user: StaffUser, check_in: str = "", check_out: str = "",
                           room_type_id: str = "", guest_id: str = "", room_id: str = ""):
    hs = await load_hotel_settings(db)
    today = today_of(hs)
    ci = parse_date(check_in) or today
    co = parse_date(check_out) or ci + timedelta(days=1)
    if co <= ci:
        co = ci + timedelta(days=1)
    rt_id = int(room_type_id) if room_type_id.isdigit() else None
    rooms = await booking.free_rooms(db, ci, co, rt_id)
    calendar = await load_calendar(db, hs, ci, co)
    nights = booking.nights_between(ci, co)
    # Estimation par chambre : prix spécifique de la chambre, saisons, week-end et remise long séjour inclus.
    estimates = {r.id: calendar.quote(r, ci, co).total_ttc for r in rooms}
    guest = await db.get(Guest, int(guest_id)) if guest_id.isdigit() else None
    alerts = await crm.guest_alerts(db, guest, hs) if guest else []
    return await render(
        request, db, "pms/reservation_new.html", ci=ci, co=co, rt_id=rt_id, rooms=rooms, estimates=estimates,
        nights=len(nights), guest=guest, alerts=alerts, selected_room=int(room_id) if room_id.isdigit() else None,
        room_types=(await db.scalars(select(RoomType).order_by(RoomType.code))).all(),
        companies=(await db.scalars(select(Company).order_by(Company.name))).all(),
    )


@router.post("/reservations")
async def reservation_create(request: Request, db: DbSession, user: StaffUser):
    form = await request.form()
    hs = await load_hotel_settings(db)
    back = f"/reservations/new?check_in={form.get('check_in', '')}&check_out={form.get('check_out', '')}"
    try:
        ci = to_date(form.get("check_in"), "Date d'arrivée")
        co = to_date(form.get("check_out"), "Date de départ")
        room = await db.get(Room, to_int(form.get("room_id"), "Chambre"))
        if room is None:
            raise BusinessError("Choisissez une chambre disponible.")
        guest_id = str(form.get("guest_id") or "")
        guest = await db.get(Guest, int(guest_id)) if guest_id.isdigit() else None
        if guest is None:
            data = _guest_data(form, "g_")
            guest = await crm.find_duplicate(db, data["id_type"], data["id_number"])
            if guest is not None:
                flash(request, f"Client existant retrouvé par sa pièce d'identité : {guest.full_name}.", "info")
            else:
                guest = await crm.save_guest(db, None, data, hs, user)
        company_id = str(form.get("company_id") or "")
        company = await db.get(Company, int(company_id)) if company_id.isdigit() else None
        res = await booking.create_reservation(
            db, guest=guest, room=room, check_in=ci, check_out=co, hs=hs, user=user,
            adults=to_int(form.get("adults"), "Nombre d'adultes", default=1, minimum=1, maximum=20),
            children=to_int(form.get("children"), "Nombre d'enfants", default=0, minimum=0, maximum=20),
            company=company, channel=str(form.get("channel") or "Direct"),
            coming_from=str(form.get("coming_from") or "").strip(), going_to=str(form.get("going_to") or "").strip(),
            notes=str(form.get("notes") or "").strip(),
            allow_blacklisted=user.role == "admin" and form.get("override_blacklist") == "1",
        )
        deposit = to_int(form.get("deposit"), "Acompte", default=0, minimum=0)
        if deposit:
            await booking.add_payment(db, res, amount=deposit, kind="acompte", hs=hs, user=user,
                                      method=str(form.get("deposit_method") or "especes"))
        await db.commit()
    except BusinessError as exc:
        await db.rollback()
        flash(request, str(exc), "error")
        return redirect(back)
    flash(request, f"Réservation {res.code} créée pour {guest.full_name}.")
    return redirect(f"/reservations/{res.id}")


@router.get("/reservations/{res_id}")
async def reservation_detail(request: Request, db: DbSession, user: StaffUser, res_id: int):
    res = await _reservation(db, res_id)
    hs = await load_hotel_settings(db)
    folio = build_folio(res, hs, await load_calendar(db, hs))
    today = today_of(hs)
    # Solde dû si le client part aujourd'hui : on retire les nuits prévisionnelles à partir d'aujourd'hui.
    departure_balance = folio.balance - sum(
        line.amount for line in folio.lines if not line.posted and line.business_date >= today
    ) if res.status == "checked_in" else folio.balance
    move_from = booking.default_move_date(res, hs)
    rooms_for_move = []
    if res.is_active and move_from < res.check_out:
        rooms_for_move = [r for r in await booking.free_rooms(db, move_from, res.check_out, exclude_reservation_id=res.id)
                          if r.id != res.room_id]
    history = (await db.scalars(select(AuditLog).where(AuditLog.entity == "reservation", AuditLog.entity_id == res.code)
                                .order_by(AuditLog.created_at.desc()).limit(30))).all()
    orders = (await db.scalars(select(PosOrder).where(PosOrder.reservation_id == res.id))).unique().all()
    return await render(
        request, db, "pms/reservation_detail.html", res=res, folio=folio, departure_balance=departure_balance,
        alerts=await crm.guest_alerts(db, res.guest, hs),
        move_from=move_from, rooms_for_move=rooms_for_move, all_rooms=await _active_rooms(db),
        companies=(await db.scalars(select(Company).order_by(Company.name))).all(), history=history, orders=orders,
    )


@router.post("/reservations/{res_id}/update")
async def reservation_update(request: Request, db: DbSession, user: StaffUser, res_id: int,
                             room_id: Annotated[str, Form()] = "", check_in: Annotated[str, Form()] = "",
                             check_out: Annotated[str, Form()] = "", adults: Annotated[str, Form()] = "1",
                             children: Annotated[str, Form()] = "0", company_id: Annotated[str, Form()] = ""):
    res = await _reservation(db, res_id)
    hs = await load_hotel_settings(db)

    async def action():
        room = await db.get(Room, to_int(room_id, "Chambre"))
        if room is None:
            raise BusinessError("Chambre introuvable.")
        company = await db.get(Company, int(company_id)) if company_id.isdigit() else None
        await booking.update_reservation(
            db, res, room=room, check_in=to_date(check_in, "Arrivée"), check_out=to_date(check_out, "Départ"),
            adults=to_int(adults, "Adultes", minimum=1), children=to_int(children, "Enfants", default=0, minimum=0),
            company=company, hs=hs, user=user)

    await attempt(request, db, action, "Réservation mise à jour.")
    return _back(res_id)


@router.post("/reservations/{res_id}/details")
async def reservation_details(request: Request, db: DbSession, user: StaffUser, res_id: int,
                              channel: Annotated[str, Form()] = "Direct", coming_from: Annotated[str, Form()] = "",
                              going_to: Annotated[str, Form()] = "", notes: Annotated[str, Form()] = ""):
    res = await _reservation(db, res_id)

    async def action():
        res.channel, res.coming_from, res.going_to = channel[:40], coming_from.strip()[:120], going_to.strip()[:120]
        res.notes = notes.strip()

    await attempt(request, db, action, "Informations du séjour enregistrées.")
    return _back(res_id)


@router.post("/reservations/{res_id}/checkin")
async def reservation_checkin(request: Request, db: DbSession, user: StaffUser, res_id: int,
                              override: Annotated[str, Form()] = ""):
    res = await _reservation(db, res_id)
    hs = await load_hotel_settings(db)
    await attempt(request, db, lambda: booking.check_in(db, res, hs, user, allow_blacklisted=user.role == "admin" and bool(override)),
                  f"Arrivée enregistrée – chambre {res.room.number}.")
    return _back(res_id)


@router.post("/reservations/{res_id}/checkout")
async def reservation_checkout(request: Request, db: DbSession, user: StaffUser, res_id: int,
                               force: Annotated[str, Form()] = ""):
    res = await _reservation(db, res_id)
    hs = await load_hotel_settings(db)
    await attempt(request, db, lambda: booking.check_out(db, res, hs, user, force=bool(force)),
                  "Départ enregistré : facture définitive émise, chambre à nettoyer.")
    return _back(res_id)


@router.post("/reservations/{res_id}/room")
async def reservation_move(request: Request, db: DbSession, user: StaffUser, res_id: int,
                           new_room_id: Annotated[str, Form()] = "", from_date: Annotated[str, Form()] = ""):
    res = await _reservation(db, res_id)
    hs = await load_hotel_settings(db)

    async def action():
        room = await db.get(Room, to_int(new_room_id, "Chambre"))
        if room is None:
            raise BusinessError("Chambre introuvable.")
        start = to_date(from_date, "Date", required=False) or booking.default_move_date(res, hs)
        await booking.change_room(db, res, room, start, hs, user)

    await attempt(request, db, action, "Changement de chambre effectué.")
    return _back(res_id)


@router.post("/reservations/{res_id}/cancel")
async def reservation_cancel(request: Request, db: DbSession, user: StaffUser, res_id: int,
                             reason: Annotated[str, Form()] = ""):
    res = await _reservation(db, res_id)
    hs = await load_hotel_settings(db)
    await attempt(request, db, lambda: booking.cancel(db, res, hs, user, reason), f"Réservation {res.code} annulée.")
    return _back(res_id)


@router.post("/reservations/{res_id}/noshow")
async def reservation_no_show(request: Request, db: DbSession, user: StaffUser, res_id: int):
    res = await _reservation(db, res_id)
    hs = await load_hotel_settings(db)
    await attempt(request, db, lambda: booking.mark_no_show(db, res, hs, user), f"{res.code} déclarée no-show.")
    return _back(res_id)


@router.post("/reservations/{res_id}/payments")
async def reservation_payment(request: Request, db: DbSession, user: StaffUser, res_id: int,
                              amount: Annotated[str, Form()] = "", kind: Annotated[str, Form()] = "paiement",
                              method: Annotated[str, Form()] = "especes", reference: Annotated[str, Form()] = "",
                              note: Annotated[str, Form()] = ""):
    res = await _reservation(db, res_id)
    hs = await load_hotel_settings(db)

    async def action():
        await booking.add_payment(db, res, amount=to_int(amount, "Montant", minimum=1), kind=kind, method=method,
                                  hs=hs, user=user, reference=reference, note=note)

    await attempt(request, db, action, "Versement enregistré.")
    return _back(res_id)


@router.post("/reservations/{res_id}/extras")
async def reservation_extra(request: Request, db: DbSession, user: StaffUser, res_id: int,
                            description: Annotated[str, Form()] = "", amount: Annotated[str, Form()] = "",
                            vat: Annotated[str, Form()] = "services"):
    res = await _reservation(db, res_id)
    hs = await load_hotel_settings(db)
    rates = {"services": hs.vat_rate_services, "fnb": hs.vat_rate_fnb, "accommodation": hs.vat_rate_accommodation}

    async def action():
        await booking.add_extra(db, res, description=description, amount=to_int(amount, "Montant", minimum=1),
                                vat_rate=rates.get(vat, hs.vat_rate_services), hs=hs, user=user)

    await attempt(request, db, action, "Prestation ajoutée au folio.")
    return _back(res_id)


@router.post("/reservations/{res_id}/items/{item_id}/void")
async def reservation_void_item(request: Request, db: DbSession, user: StaffUser, res_id: int, item_id: int,
                                reason: Annotated[str, Form()] = ""):
    res = await _reservation(db, res_id)
    hs = await load_hotel_settings(db)
    await attempt(request, db, lambda: booking.void_item(db, res, item_id, hs, user, reason), "Écriture annulée.")
    return _back(res_id)


@router.post("/reservations/{res_id}/companions")
async def reservation_companion(request: Request, db: DbSession, user: StaffUser, res_id: int):
    res = await _reservation(db, res_id)
    hs = await load_hotel_settings(db)
    form = await request.form()

    async def action():
        get = lambda key: str(form.get(key) or "").strip()  # noqa: E731
        id_type = get("id_type") or "cni"
        if id_type not in ID_TYPES:
            raise BusinessError("Type de pièce invalide.")
        await booking.add_companion(db, res, {
            "first_name": get("first_name"), "last_name": get("last_name"),
            "first_name_ar": get("first_name_ar"), "last_name_ar": get("last_name_ar"),
            "birth_date": to_date(get("birth_date"), "Date de naissance", required=False),
            "birth_place": get("birth_place"), "nationality": get("nationality"),
            "relationship_label": get("relationship_label"), "id_type": id_type, "id_number": get("id_number"),
        }, hs, user)

    await attempt(request, db, action, "Accompagnateur ajouté.")
    return _back(res_id)


@router.post("/reservations/{res_id}/companions/{companion_id}/delete")
async def reservation_companion_delete(request: Request, db: DbSession, user: StaffUser, res_id: int, companion_id: int):
    res = await _reservation(db, res_id)

    async def action():
        companion = next((c for c in res.companions if c.id == companion_id), None)
        if companion is None:
            raise BusinessError("Accompagnateur introuvable.")
        res.companions.remove(companion)
        await db.delete(companion)

    await attempt(request, db, action, "Accompagnateur retiré.")
    return _back(res_id)


# ---------------------------------------------------------------- documents et impression
def _pdf(content: bytes, filename: str) -> Response:
    return Response(content, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "no-store"})


@router.get("/reservations/{res_id}/police.pdf")
async def police_form(db: DbSession, user: StaffUser, res_id: int, lang: str = "bi"):
    res = await _reservation(db, res_id)
    hs = await load_hotel_settings(db)
    theme = await theme_manager.load_theme(db)  # lecture seule : aucune écriture dans un GET
    return _pdf(pdf.police_form_pdf(hs, res, now_of(hs), lang=pdf.doc_lang(lang), theme=theme),
                f"fiche-police-{res.code}.pdf")


@router.get("/reservations/{res_id}/invoice.pdf")
async def invoice_document(db: DbSession, user: StaffUser, res_id: int, format: str = "a4", lang: str = "bi"):
    res = await _reservation(db, res_id)
    if res.status in ("cancelled", "no_show") and not res.invoices:
        raise HTTPException(status_code=400, detail="Aucune facture pour une réservation annulée ou no-show.")
    hs = await load_hotel_settings(db)
    view = invoice_view(res, hs, await load_calendar(db, hs), now_of(hs))
    fmt = "ticket" if format == "ticket" else "a4"
    theme = await theme_manager.load_theme(db)  # lecture seule : aucune écriture dans un GET
    return _pdf(pdf.invoice_pdf(hs, view, fmt, lang=pdf.doc_lang(lang), theme=theme), f"{view.number}-{fmt}.pdf")


@router.get("/reservations/{res_id}/qr.svg")
async def reservation_qr(db: DbSession, user: StaffUser, res_id: int):
    res = await _reservation(db, res_id)
    return Response(qr_svg(res.code), media_type="image/svg+xml")


@router.post("/reservations/{res_id}/print/{document}")
async def reservation_print(request: Request, db: DbSession, user: StaffUser, res_id: int, document: str):
    res = await _reservation(db, res_id)
    hs = await load_hotel_settings(db)
    form = await request.form()
    lang = pdf.doc_lang(form.get("lang"), "fr")  # champ de formulaire lang : fr (défaut) | ar | en | bi
    theme = await theme_manager.get_theme(db)

    async def action():
        if document == "ticket":
            view = invoice_view(res, hs, await load_calendar(db, hs), now_of(hs))
            await printing.print_raw(hs.thermal_printer_host, hs.thermal_printer_port,
                                     printing.invoice_receipt(view, hs, lang=lang, theme=theme))
        elif document == "invoice":
            view = invoice_view(res, hs, await load_calendar(db, hs), now_of(hs))
            await printing.print_raw(hs.a4_printer_host, hs.a4_printer_port,
                                     pdf.invoice_pdf(hs, view, "a4", lang=lang, theme=theme))
        elif document == "police":
            await printing.print_raw(hs.a4_printer_host, hs.a4_printer_port,
                                     pdf.police_form_pdf(hs, res, now_of(hs), lang=lang, theme=theme))
        else:
            raise BusinessError("Document inconnu.")

    await attempt(request, db, action, "Document envoyé à l'imprimante.")
    return _back(res_id)


@router.get("/print")
async def print_page(request: Request, db: DbSession, user: AnyUser, doc: str = "", back: str = "/"):
    target = safe_next(doc, "")
    if not target or not target.split("?")[0].endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Document à imprimer invalide.")
    return await render(request, db, "pms/print.html", doc=target, back=safe_next(back))


# ---------------------------------------------------------------- recherche (douchette / QR)
@router.get("/search")
async def search_page(request: Request, db: DbSession, user: StaffUser, q: str = ""):
    result = await crm.search(db, q)
    if result.redirect:
        return redirect(result.redirect)
    return await render(request, db, "pms/search.html", q=q, result=result)


@router.get("/api/search")
async def search_api(db: DbSession, user: StaffUser, q: str = ""):
    if len(q.strip()) < 2:
        return JSONResponse({"guests": []})
    result = await crm.search(db, q)
    guests = result.guests
    if result.redirect and result.redirect.startswith("/guests/"):
        guests = [await db.get(Guest, int(result.redirect.rsplit("/", 1)[1]))]
    return JSONResponse({"guests": [
        {"id": g.id, "name": g.full_name, "phone": g.phone, "id_number": g.id_number,
         "id_type": ID_TYPES.get(g.id_type, ""), "status": g.status, "nationality": g.nationality}
        for g in guests[:15]
    ], "redirect": result.redirect})


# ---------------------------------------------------------------- audit de nuit
@router.get("/night-audit")
async def night_audit_page(request: Request, db: DbSession, user: StaffUser):
    hs = await load_hotel_settings(db)
    runs = (await db.scalars(select(NightAuditRun).order_by(NightAuditRun.business_date.desc()).limit(60))).all()
    in_house = await load_reservations(db, Reservation.status == "checked_in")
    late_arrivals = await load_reservations(db, Reservation.status == "confirmed", Reservation.check_in < today_of(hs))
    return await render(request, db, "pms/night_audit.html", runs=runs, pending=await night_audit.pending_audit_dates(db, hs),
                        in_house=in_house, late_arrivals=late_arrivals)


@router.post("/night-audit/run")
async def night_audit_run(request: Request, db: DbSession, user: StaffUser, mode: Annotated[str, Form()] = "pending",
                          business_date: Annotated[str, Form()] = ""):
    hs = await load_hotel_settings(db)
    if mode == "date":
        async def action():
            await night_audit.run_night_audit(db, to_date(business_date, "Date à clôturer"), hs, user)
        message = f"Journée du {business_date} clôturée : nuitées figées."
    else:
        async def action():
            runs = await night_audit.run_pending_audits(db, hs, user)
            if not runs:
                raise BusinessError("Aucune journée en attente de clôture.")
        message = "Journées en attente clôturées : nuitées figées."
    await attempt(request, db, action, message)
    return redirect("/night-audit")


@router.get("/night-audit/{day}/report.pdf")
async def night_audit_report(db: DbSession, user: StaffUser, day: str, lang: str = "bi"):
    business_date = parse_date(day)
    if business_date is None:
        raise HTTPException(status_code=400, detail="Date invalide.")
    hs = await load_hotel_settings(db)
    theme = await theme_manager.load_theme(db)  # lecture seule : aucune écriture dans un GET
    return _pdf(pdf.daily_report_pdf(hs, await reports.daily_summary(db, business_date), lang=pdf.doc_lang(lang),
                                     theme=theme),
                f"cloture-{day}.pdf")


# ---------------------------------------------------------------- factures
@router.get("/invoices")
async def invoices_list(request: Request, db: DbSession, user: StaffUser, start: str = "", end: str = "", q: str = ""):
    hs = await load_hotel_settings(db)
    today = today_of(hs)
    d_start = parse_date(start) or today.replace(day=1)
    d_end = parse_date(end) or today
    stmt = (select(Invoice, Reservation).join(Reservation, Invoice.reservation_id == Reservation.id)
            .where(Invoice.issued_at >= d_start, Invoice.issued_at < d_end + timedelta(days=1))
            .order_by(Invoice.issued_at.desc()))
    if q.strip():
        pattern = like_pattern(q.strip())
        stmt = stmt.where(or_(Invoice.number.ilike(pattern, escape="\\"), Invoice.customer_name.ilike(pattern, escape="\\")))
    rows = (await db.execute(stmt)).unique().all()
    totals = {"ttc": sum(i.total_ttc for i, _ in rows), "ht": sum(i.total_ht for i, _ in rows),
              "vat": sum(i.total_vat for i, _ in rows), "tax": sum(i.tourist_tax for i, _ in rows)}
    return await render(request, db, "pms/invoices.html", rows=rows, start=d_start, end=d_end, q=q, totals=totals)
