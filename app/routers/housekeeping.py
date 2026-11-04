"""Housekeeping : tableau de l'état des chambres, interface dédiée à la gouvernante."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from sqlalchemy import select

from app.deps import AccessDenied, DbSession, HousekeepingUser, attempt, redirect
from app.models.booking import Reservation
from app.models.room import ROOM_STATUSES, Room
from app.services.audit_log import log_action
from app.services.common import BusinessError
from app.services.loaders import load_reservations
from app.services.settings import load_hotel_settings, now_of, today_of
from app.templating import render

router = APIRouter()
SETTABLE = ("available", "dirty", "maintenance")


def _can_change(hs, user) -> bool:
    return not (hs.restrict_cleaning_to_reception and user.role == "housekeeping")


@router.get("/housekeeping")
async def board(request: Request, db: DbSession, user: HousekeepingUser, floor: str = ""):
    hs = await load_hotel_settings(db)
    today = today_of(hs)
    stmt = select(Room).where(Room.active.is_(True)).order_by(Room.number)
    if floor.lstrip("-").isdigit():
        stmt = stmt.where(Room.floor == int(floor))
    rooms = list((await db.scalars(stmt)).unique().all())
    in_house = await load_reservations(db, Reservation.status == "checked_in")
    occupant = {r.room_id: r for r in in_house}
    departures = {r.room_id for r in in_house if r.check_out <= today}
    arrivals = {r.room_id for r in await load_reservations(db, Reservation.status == "confirmed", Reservation.check_in == today)}
    columns = {key: [r for r in rooms if r.status == key] for key in ("dirty", "available", "occupied", "maintenance")}
    floors = sorted({r.floor for r in (await db.scalars(select(Room).where(Room.active.is_(True)))).unique().all()})
    return await render(request, db, "housekeeping/board.html", columns=columns, occupant=occupant,
                        departures=departures, arrivals=arrivals, can_change=_can_change(hs, user),
                        floors=floors, floor=floor)


@router.post("/housekeeping/rooms/{room_id}/status")
async def set_status(request: Request, db: DbSession, user: HousekeepingUser, room_id: int,
                     status: Annotated[str, Form()] = "", note: Annotated[str, Form()] = "",
                     floor: Annotated[str, Form()] = ""):
    hs = await load_hotel_settings(db)
    if not _can_change(hs, user):
        raise AccessDenied()
    room = await db.get(Room, room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Chambre introuvable.")

    async def action():
        if status not in SETTABLE:
            raise BusinessError("Statut non autorisé.")
        if room.status == "occupied":
            raise BusinessError(f"La chambre {room.number} est occupée : son statut suit le séjour.")
        previous = room.status
        room.status, room.status_changed_at = status, now_of(hs)
        if note.strip():
            room.notes = note.strip()[:500]
        await log_action(db, user, "room_status", "room", room.number, f"{previous} → {status}", now_of(hs))

    await attempt(request, db, action, f"Chambre {room.number} : {ROOM_STATUSES.get(status, status).lower()}.")
    return redirect(f"/housekeeping?floor={floor}" if floor.lstrip("-").isdigit() else "/housekeeping")
