"""Chargements explicites (eager) : en asynchrone, aucun chargement paresseux n'est autorisé."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.models.booking import Reservation, StaySegment
from app.models.pos import PosOrder
from app.models.room import Room


def reservation_options():
    return (
        joinedload(Reservation.guest),
        joinedload(Reservation.company),
        joinedload(Reservation.room_type),
        joinedload(Reservation.room).joinedload(Room.room_type),
        selectinload(Reservation.segments).joinedload(StaySegment.room).joinedload(Room.room_type),
        selectinload(Reservation.companions),
        selectinload(Reservation.folio_items),
        selectinload(Reservation.payments),
        selectinload(Reservation.invoices),
    )


async def load_reservation(db: AsyncSession, reservation_id: int) -> Reservation | None:
    stmt = (
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .options(*reservation_options())
        .execution_options(populate_existing=True)
    )
    return (await db.scalars(stmt)).unique().first()


async def load_reservations(db: AsyncSession, *criteria, order_by=None, limit: int | None = None) -> list[Reservation]:
    stmt = select(Reservation).where(*criteria).options(*reservation_options()).execution_options(populate_existing=True)
    if order_by is not None:
        stmt = stmt.order_by(*order_by) if isinstance(order_by, (list, tuple)) else stmt.order_by(order_by)
    if limit:
        stmt = stmt.limit(limit)
    return list((await db.scalars(stmt)).unique().all())


async def load_pos_order(db: AsyncSession, order_id: int) -> PosOrder | None:
    stmt = (
        select(PosOrder).where(PosOrder.id == order_id)
        .options(selectinload(PosOrder.lines), joinedload(PosOrder.reservation), joinedload(PosOrder.user))
        .execution_options(populate_existing=True)
    )
    return (await db.scalars(stmt)).unique().first()
