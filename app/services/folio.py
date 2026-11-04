"""Calcul du folio d'un séjour : nuitées, extras, TVA, taxe de séjour, paiements, solde."""
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.services.pricing import nightly_price


@dataclass
class RoomLine:
    room: str
    room_type: str
    start: date
    end: date
    nights: int
    amount: int  # TTC


@dataclass
class Folio:
    room_lines: list[RoomLine] = field(default_factory=list)
    charges: list = field(default_factory=list)
    payments: list = field(default_factory=list)
    nights: int = 0
    room_total: int = 0
    charges_total: int = 0
    taxable_ttc: int = 0
    vat_rate: int = 0
    vat_amount: int = 0
    taxable_ht: int = 0
    tourist_tax: int = 0
    total_due: int = 0
    paid: int = 0
    balance: int = 0


def compute_folio(db: Session, res, settings: dict) -> Folio:
    """Tarifs TTC saisis ; la TVA est extraite (HT = TTC / (1 + taux))."""
    folio = Folio()
    for seg in sorted(res.segments, key=lambda s: s.start_date):
        amount, nights, day = 0, 0, seg.start_date
        while day < seg.end_date:
            amount += nightly_price(db, seg.room.room_type, day, res.company, settings)
            nights += 1
            day += timedelta(days=1)
        folio.room_lines.append(
            RoomLine(seg.room.number, seg.room.room_type.name, seg.start_date, seg.end_date, nights, amount)
        )
        folio.room_total += amount

    folio.charges = list(res.charges)
    folio.charges_total = sum(c.amount_ttc for c in folio.charges)
    folio.payments = list(res.payments)
    folio.nights = res.nights

    folio.taxable_ttc = folio.room_total + folio.charges_total
    folio.vat_rate = int(settings["vat_rate"])
    folio.taxable_ht = round(folio.taxable_ttc / (1 + folio.vat_rate / 100))
    folio.vat_amount = folio.taxable_ttc - folio.taxable_ht

    if settings["tourist_tax_enabled"]:
        folio.tourist_tax = int(settings["tourist_tax_per_person_night"]) * res.adults * folio.nights

    folio.total_due = folio.taxable_ttc + folio.tourist_tax
    folio.paid = sum(-p.amount if p.kind == "remboursement" else p.amount for p in folio.payments)
    folio.balance = folio.total_due - folio.paid
    return folio
