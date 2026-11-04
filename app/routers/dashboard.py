from collections import Counter, defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import any_user, parse_date, render
from app.models.admin import User
from app.models.property import Room, RoomType
from app.models.reservations import Payment, Reservation, StaySegment
from app.services.settings import load_settings, today_of

router = APIRouter()
EXCLUDED_FROM_PLANNING = ("cancelled", "no_show")


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(any_user)):
    if user.role == "housekeeping":
        return RedirectResponse("/housekeeping", status_code=303)

    settings = load_settings(db)
    today = today_of(settings)
    rooms = list(db.scalars(select(Room).order_by(Room.number)).all())
    in_house = list(db.scalars(select(Reservation).where(Reservation.status == "checked_in")).all())
    occupant_by_room = {r.room_id: r for r in in_house}

    arrivals = list(db.scalars(select(Reservation).where(
        Reservation.check_in == today, Reservation.status == "confirmed")).all())
    departures = list(db.scalars(select(Reservation).where(
        Reservation.check_out == today, Reservation.status == "checked_in")).all())

    day_start = datetime.combine(today, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    payments_today = db.scalars(select(Payment).where(Payment.paid_at >= day_start, Payment.paid_at < day_end)).all()
    cash_today = sum(-p.amount if p.kind == "remboursement" else p.amount for p in payments_today)

    counts = Counter(r.status for r in rooms)
    occupancy = round(100 * counts.get("occupied", 0) / len(rooms)) if rooms else 0
    return render(
        request, db, "dashboard.html",
        rooms=rooms, counts=counts, occupancy=occupancy, occupant_by_room=occupant_by_room,
        arrivals=arrivals, departures=departures, in_house=in_house, cash_today=cash_today,
    )


@router.get("/planning")
def planning(
    request: Request,
    start: str = "",
    days: int = 14,
    room_type_id: int | None = None,
    floor: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(any_user),
):
    settings = load_settings(db)
    today = today_of(settings)
    if user.role == "housekeeping":
        return RedirectResponse("/housekeeping", status_code=303)
    start_day = parse_date(start) or today - timedelta(days=2)
    days = days if days in (7, 14, 30) else 14
    window = [start_day + timedelta(days=i) for i in range(days)]
    end_day = start_day + timedelta(days=days)

    room_stmt = select(Room).order_by(Room.number)
    if room_type_id:
        room_stmt = room_stmt.where(Room.room_type_id == room_type_id)
    if floor is not None:
        room_stmt = room_stmt.where(Room.floor == floor)
    rooms = list(db.scalars(room_stmt).all())

    segments = db.scalars(
        select(StaySegment)
        .join(Reservation, StaySegment.reservation_id == Reservation.id)
        .where(
            StaySegment.start_date < end_day,
            StaySegment.end_date > start_day,
            Reservation.status.not_in(EXCLUDED_FROM_PLANNING),
        )
    ).all()
    by_room: dict[int, list[StaySegment]] = defaultdict(list)
    for seg in segments:
        by_room[seg.room_id].append(seg)

    def segment_on(room_segments, day):
        return next((s for s in room_segments if s.start_date <= day < s.end_date), None)

    rows = []
    for room in rooms:
        room_segments = by_room.get(room.id, [])
        cells, i = [], 0
        while i < days:
            seg = segment_on(room_segments, window[i])
            if seg is None:
                cells.append({"kind": "free", "span": 1, "day": window[i], "next": window[i] + timedelta(days=1)})
                i += 1
                continue
            j = i + 1
            while j < days and segment_on(room_segments, window[j]) is seg:
                j += 1
            cells.append({"kind": "bar", "span": j - i, "seg": seg})
            i = j
        rows.append({"room": room, "cells": cells})

    return render(
        request, db, "planning.html",
        rows=rows, window=window, days=days, start_day=start_day, end_day=end_day,
        prev_start=start_day - timedelta(days=days), next_start=start_day + timedelta(days=days),
        room_type_id=room_type_id, floor=floor, room_types=db.scalars(select(RoomType)).all(),
        floors=sorted({r.floor for r in db.scalars(select(Room)).all()}),
    )
