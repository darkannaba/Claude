"""Folio d'un séjour : écritures figées (folio_items) + nuitées prévisionnelles non encore auditées."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from app.models.booking import Reservation
from app.models.invoice import ITEM_KINDS
from app.services.common import split_vat
from app.services.pricing import RateCalendar
from app.services.settings import HotelSettings


def room_key(reservation_id: int, night: date) -> str:
    return f"{reservation_id}:{night.isoformat()}:room"


def tax_key(reservation_id: int, night: date) -> str:
    return f"{reservation_id}:{night.isoformat()}:tax"


def taxable_persons(res: Reservation, hs: HotelSettings) -> int:
    return res.adults + (res.children if hs.tourist_tax_include_children else 0)


@dataclass
class FolioLine:
    business_date: date
    kind: str
    description: str
    amount: int
    vat_rate: int
    posted: bool
    item_id: int | None = None
    invoiced: bool = False
    is_reversal: bool = False
    reversed: bool = False

    @property
    def kind_label(self) -> str:
        return ITEM_KINDS.get(self.kind, self.kind)


@dataclass
class Folio:
    lines: list[FolioLine] = field(default_factory=list)
    payments: list = field(default_factory=list)
    nights: int = 0
    room_total: int = 0
    tax_total: int = 0
    other_total: int = 0
    total_ht: int = 0
    total_vat: int = 0
    total_ttc: int = 0
    vat_breakdown: dict[int, list[int]] = field(default_factory=dict)  # taux -> [HT, TVA]
    paid: int = 0
    balance: int = 0
    projected_total: int = 0

    @property
    def has_projection(self) -> bool:
        return any(not line.posted for line in self.lines)


def build_folio(res: Reservation, hs: HotelSettings, calendar: RateCalendar | None = None) -> Folio:
    folio = Folio(nights=res.nights)
    reversed_ids = {i.reversal_of_id for i in res.folio_items if i.reversal_of_id}
    posted_keys = {i.night_key for i in res.folio_items if i.night_key}

    for item in res.folio_items:
        folio.lines.append(FolioLine(
            business_date=item.business_date, kind=item.kind, description=item.description, amount=item.amount,
            vat_rate=item.vat_rate, posted=True, item_id=item.id, invoiced=item.invoice_id is not None,
            is_reversal=item.reversal_of_id is not None, reversed=item.id in reversed_ids,
        ))

    if res.is_active and calendar is not None:
        persons = taxable_persons(res, hs)
        for seg in res.segments:
            night = seg.start_date
            while night < seg.end_date:
                if room_key(res.id, night) not in posted_keys:
                    price = calendar.price(seg.room.room_type, night, res.company)
                    folio.lines.append(FolioLine(
                        night, "room", f"Nuitée {night:%d/%m/%Y} – Chambre {seg.room.number} (prévisionnel)",
                        price, hs.vat_rate_accommodation, posted=False))
                if hs.tourist_tax_enabled and persons and tax_key(res.id, night) not in posted_keys:
                    folio.lines.append(FolioLine(
                        night, "tax", f"Taxe de séjour {night:%d/%m/%Y} – {persons} pers. (prévisionnel)",
                        hs.tourist_tax_per_person_night * persons, 0, posted=False))
                night += timedelta(days=1)

    folio.lines.sort(key=lambda line: (line.business_date, line.kind != "room", line.item_id or 10**9))
    for line in folio.lines:
        folio.total_ttc += line.amount
        if not line.posted:
            folio.projected_total += line.amount
        if line.kind == "room":
            folio.room_total += line.amount
        elif line.kind == "tax":
            folio.tax_total += line.amount
        else:
            folio.other_total += line.amount
        if line.kind != "tax":
            ht, vat = split_vat(line.amount, line.vat_rate)
            folio.total_ht += ht
            folio.total_vat += vat
            bucket = folio.vat_breakdown.setdefault(line.vat_rate, [0, 0])
            bucket[0] += ht
            bucket[1] += vat

    folio.payments = list(res.payments)
    folio.paid = sum(p.signed_amount for p in res.payments)
    folio.balance = folio.total_ttc - folio.paid
    return folio
