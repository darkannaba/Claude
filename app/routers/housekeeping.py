from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import AccessDenied, flash, housekeeping_team, redirect_to, render
from app.models.admin import User
from app.models.property import ROOM_STATUSES, Room
from app.services.settings import load_settings
from app.services.availability import occupying_reservation
from app.services.settings import today_of

router = APIRouter()
SETTABLE = ("available", "dirty", "maintenance")


@router.get("/housekeeping")
def housekeeping_board(request: Request, db: Session = Depends(get_db), user: User = Depends(housekeeping_team)):
    settings = load_settings(db)
    today = today_of(settings)
    rooms = db.scalars(select(Room).order_by(Room.number)).all()
    occupant = {r.id: occupying_reservation(db, r.id, today) for r in rooms if r.status == "occupied"}
    columns = {key: [] for key in ROOM_STATUSES}
    for room in rooms:
        columns[room.status].append(room)
    can_change = not (settings["restrict_cleaning_to_reception"] and user.role == "housekeeping")
    return render(request, db, "housekeeping.html", columns=columns, occupant=occupant, can_change=can_change)


@router.post("/housekeeping/rooms/{room_id}/status")
def set_room_status(room_id: int, request: Request, status: str = Form(""), db: Session = Depends(get_db),
                    user: User = Depends(housekeeping_team)):
    settings = load_settings(db)
    if settings["restrict_cleaning_to_reception"] and user.role == "housekeeping":
        raise AccessDenied()
    room = db.get(Room, room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Chambre introuvable.")
    if status not in SETTABLE:
        flash(request, "Statut non autorisé.", "err")
    elif room.status == "occupied":
        flash(request, f"La chambre {room.number} est occupée : statut verrouillé.", "err")
    else:
        room.status = status
        db.commit()
        flash(request, f"Chambre {room.number} : {ROOM_STATUSES[status].lower()}.")
    return redirect_to("/housekeeping")
