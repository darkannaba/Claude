"""Indicateurs hôteliers (TO, ADR, RevPAR), séries de chiffre d'affaires et écritures comptables.

Les chiffres sont calculés sur les écritures FIGÉES (nuits clôturées par l'audit) : ce
sont donc des données comptables définitives, insensibles aux changements de tarifs.
"""
from __future__ import annotations

import calendar as cal
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import NightAuditRun
from app.models.booking import Reservation
from app.models.invoice import ITEM_KINDS, PAYMENT_METHODS, FolioItem, Invoice, Payment
from app.models.pos import OUTLETS, PosOrder
from app.models.room import Room
from app.services.common import split_vat

MONTHS_FR = ["Janv.", "Févr.", "Mars", "Avr.", "Mai", "Juin", "Juil.", "Août", "Sept.", "Oct.", "Nov.", "Déc."]


@dataclass
class Kpi:
    start: date
    end: date
    available_nights: int
    sold_nights: int
    room_revenue: int
    other_revenue: int
    tourist_tax: int

    @property
    def occupancy(self) -> float:
        return round(100 * self.sold_nights / self.available_nights, 1) if self.available_nights else 0.0

    @property
    def adr(self) -> int:
        return round(self.room_revenue / self.sold_nights) if self.sold_nights else 0

    @property
    def revpar(self) -> int:
        return round(self.room_revenue / self.available_nights) if self.available_nights else 0

    @property
    def total_revenue(self) -> int:
        return self.room_revenue + self.other_revenue


async def active_room_count(db: AsyncSession) -> int:
    return await db.scalar(select(func.count(Room.id)).where(Room.active.is_(True))) or 0


async def _items(db: AsyncSession, start: date, end: date) -> list[FolioItem]:
    stmt = select(FolioItem).where(FolioItem.business_date >= start, FolioItem.business_date < end)
    return list((await db.scalars(stmt)).unique().all())


async def kpis(db: AsyncSession, start: date, end: date, room_count: int | None = None) -> Kpi:
    """Indicateurs sur [start, end)."""
    rooms = room_count if room_count is not None else await active_room_count(db)
    items = await _items(db, start, end)
    reversed_ids = {i.reversal_of_id for i in items if i.reversal_of_id}
    sold = sum(1 for i in items if i.kind == "room" and i.reversal_of_id is None and i.id not in reversed_ids)
    room_revenue = sum(i.amount for i in items if i.kind == "room")
    tax = sum(i.amount for i in items if i.kind == "tax")
    other = sum(i.amount for i in items if i.kind not in ("room", "tax"))
    pos_cash = await db.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0))
        .where(Payment.pos_order_id.is_not(None), Payment.business_date >= start, Payment.business_date < end))
    return Kpi(start, end, rooms * (end - start).days, sold, room_revenue, other + int(pos_cash or 0), tax)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    return start, start + timedelta(days=cal.monthrange(year, month)[1])


async def monthly_series(db: AsyncSession, year: int) -> list[dict]:
    rooms = await active_room_count(db)
    series = []
    for month in range(1, 13):
        start, end = month_bounds(year, month)
        k = await kpis(db, start, end, rooms)
        series.append({"label": MONTHS_FR[month - 1], "occupancy": k.occupancy, "adr": k.adr, "revpar": k.revpar,
                       "room_revenue": k.room_revenue, "other_revenue": k.other_revenue, "sold": k.sold_nights})
    return series


def _bucket(day: date, granularity: str) -> date:
    if granularity == "week":
        return day - timedelta(days=day.weekday())
    if granularity == "month":
        return day.replace(day=1)
    return day


def _bucket_label(day: date, granularity: str) -> str:
    if granularity == "week":
        return f"Sem. {day:%d/%m}"
    if granularity == "month":
        return f"{MONTHS_FR[day.month - 1]} {day.year}"
    return day.strftime("%d/%m")


async def revenue_series(db: AsyncSession, start: date, end: date, granularity: str = "day") -> dict:
    granularity = granularity if granularity in ("day", "week", "month") else "day"
    buckets: list[date] = []
    day = start
    while day < end:
        bucket = _bucket(day, granularity)
        if bucket not in buckets:
            buckets.append(bucket)
        day += timedelta(days=1)
    data = {b: {"room": 0, "services": 0, "tax": 0} for b in buckets}
    for item in await _items(db, start, end):
        key = "room" if item.kind == "room" else "tax" if item.kind == "tax" else "services"
        data[_bucket(item.business_date, granularity)][key] += item.amount
    pos_payments = (await db.scalars(select(Payment).where(
        Payment.pos_order_id.is_not(None), Payment.business_date >= start, Payment.business_date < end))).unique().all()
    for payment in pos_payments:
        data[_bucket(payment.business_date, granularity)]["services"] += payment.signed_amount
    return {
        "labels": [_bucket_label(b, granularity) for b in buckets],
        "room": [data[b]["room"] for b in buckets],
        "services": [data[b]["services"] for b in buckets],
        "tax": [data[b]["tax"] for b in buckets],
    }


async def payments_by_method(db: AsyncSession, start: date, end: date) -> dict[str, int]:
    totals = {method: 0 for method in PAYMENT_METHODS}
    payments = (await db.scalars(select(Payment).where(Payment.business_date >= start, Payment.business_date < end))).unique().all()
    for payment in payments:
        totals[payment.method] = totals.get(payment.method, 0) + payment.signed_amount
    return totals


async def daily_summary(db: AsyncSession, day: date) -> dict:
    """Synthèse de clôture journalière (rapport de l'audit de nuit)."""
    end = day + timedelta(days=1)
    items = await _items(db, day, end)
    by_kind = {kind: 0 for kind in ITEM_KINDS}
    for item in items:
        by_kind[item.kind] = by_kind.get(item.kind, 0) + item.amount
    arrivals = await db.scalar(select(func.count(Reservation.id)).where(
        func.date(Reservation.checked_in_at) == day.isoformat())) or 0
    departures = await db.scalar(select(func.count(Reservation.id)).where(
        func.date(Reservation.checked_out_at) == day.isoformat())) or 0
    run = await db.scalar(select(NightAuditRun).where(NightAuditRun.business_date == day))
    return {
        "day": day, "run": run, "by_kind": by_kind, "items_total": sum(by_kind.values()),
        "payments": await payments_by_method(db, day, end),
        "kpi": await kpis(db, day, end), "arrivals": arrivals, "departures": departures,
    }


async def accounting_entries(db: AsyncSession, start: date, end: date) -> list[dict]:
    """Écritures pour le cabinet comptable : ventes (folio) et encaissements, sur [start, end)."""
    rows: list[dict] = []
    items = (await db.scalars(
        select(FolioItem).where(FolioItem.business_date >= start, FolioItem.business_date < end)
        .order_by(FolioItem.business_date, FolioItem.id))).unique().all()
    reservations = {r.id: r for r in (await db.scalars(select(Reservation).where(
        Reservation.id.in_({i.reservation_id for i in items} or {-1})))).unique().all()}
    invoices = {i.id: i.number for i in (await db.scalars(select(Invoice))).unique().all()}
    for item in items:
        res = reservations.get(item.reservation_id)
        ht, vat = (item.amount, 0) if item.kind == "tax" else split_vat(item.amount, item.vat_rate)
        rows.append({
            "date": item.business_date, "journal": "VENTES", "type": ITEM_KINDS.get(item.kind, item.kind),
            "piece": invoices.get(item.invoice_id, "") or (res.code if res else ""),
            "reservation": res.code if res else "", "client": res.guest.full_name if res else "",
            "libelle": item.description, "taux_tva": item.vat_rate if item.kind != "tax" else 0,
            "ht": ht, "tva": vat, "ttc": item.amount, "mode": "",
        })
    pos_sales = (await db.scalars(select(PosOrder).where(
        PosOrder.status == "paid", func.date(PosOrder.closed_at) >= start.isoformat(),
        func.date(PosOrder.closed_at) < end.isoformat()))).unique().all()
    for order in pos_sales:
        for line in order.lines:
            ht, vat = split_vat(line.amount, line.vat_rate)
            rows.append({
                "date": order.closed_at.date(), "journal": "VENTES", "type": OUTLETS.get(order.outlet, order.outlet),
                "piece": order.number, "reservation": "", "client": "Comptant", "libelle": line.description,
                "taux_tva": line.vat_rate, "ht": ht, "tva": vat, "ttc": line.amount, "mode": "",
            })
    payments = (await db.scalars(select(Payment).where(Payment.business_date >= start, Payment.business_date < end)
                                 .order_by(Payment.business_date, Payment.id))).unique().all()
    for payment in payments:
        res = reservations.get(payment.reservation_id) if payment.reservation_id else None
        if payment.reservation_id and res is None:
            res = await db.get(Reservation, payment.reservation_id)
        rows.append({
            "date": payment.business_date, "journal": "CAISSE", "type": payment.kind_label,
            "piece": payment.reference or (res.code if res else ""), "reservation": res.code if res else "",
            "client": res.guest.full_name if res else "Comptant", "libelle": payment.note or payment.kind_label,
            "taux_tva": 0, "ht": 0, "tva": 0, "ttc": payment.signed_amount, "mode": payment.method_label,
        })
    rows.sort(key=lambda r: (r["date"], r["journal"]))
    return rows
