"""Factures : numérotation séquentielle annuelle, émission définitive au départ, vue proforma."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Reservation
from app.models.invoice import Invoice
from app.models.user import User
from app.services.audit_log import log_action
from app.services.folio import Folio, FolioLine, build_folio
from app.services.pricing import RateCalendar
from app.services.settings import HotelSettings


async def next_sequence_number(db: AsyncSession, column, prefix: str, year: int) -> str:
    """Prochain numéro « PRÉFIXE-AAAA-00001 » (séquence continue par année, sans trou)."""
    numbers = (await db.scalars(select(column).where(column.like(f"{prefix}-{year}-%")))).all()
    sequences = [int(n.rsplit("-", 1)[1]) for n in numbers if n.rsplit("-", 1)[1].isdigit()]
    return f"{prefix}-{year}-{(max(sequences, default=0) + 1):05d}"


def customer_block(res: Reservation) -> tuple[str, str]:
    guest = res.guest
    if res.company is not None:
        company = res.company
        details = [f"Client hébergé : {guest.full_name}"]
        if company.nif:
            details.append(f"NIF : {company.nif}")
        if company.rc:
            details.append(f"RC : {company.rc}")
        if company.address:
            details.append(company.address)
        return company.name, "\n".join(details)
    details = []
    if guest.address:
        details.append(guest.address)
    if guest.phone:
        details.append(f"Tél. : {guest.phone}")
    return guest.full_name, "\n".join(details)


async def issue_invoice(db: AsyncSession, res: Reservation, hs: HotelSettings, user: User | None,
                        now: datetime) -> Invoice:
    """Émet la facture définitive : totaux figés et écritures verrouillées (invoice_id)."""
    if res.invoices:
        return res.invoices[-1]
    folio = build_folio(res, hs, None)
    name, details = customer_block(res)
    invoice = Invoice(
        number=await next_sequence_number(db, Invoice.number, hs.invoice_prefix, now.year),
        issued_at=now, customer_name=name, customer_details=details,
        total_ht=folio.total_ht, total_vat=folio.total_vat, tourist_tax=folio.tax_total,
        total_ttc=folio.total_ttc, paid=folio.paid, balance=folio.balance,
        issued_by_id=user.id if user else None,
    )
    res.invoices.append(invoice)
    await db.flush()
    for item in res.folio_items:
        if item.invoice_id is None:
            item.invoice_id = invoice.id
    await log_action(db, user, "invoice_issued", "invoice", invoice.number,
                     f"Réservation {res.code} – {invoice.total_ttc} DA", now)
    return invoice


@dataclass
class InvoiceView:
    number: str
    title: str
    title_ar: str
    is_final: bool
    issued_at: datetime | None
    customer_name: str
    customer_details: str
    reservation: Reservation
    lines: list[FolioLine] = field(default_factory=list)
    vat_breakdown: dict[int, list[int]] = field(default_factory=dict)
    total_ht: int = 0
    total_vat: int = 0
    tourist_tax: int = 0
    total_ttc: int = 0
    paid: int = 0
    balance: int = 0
    payments: list = field(default_factory=list)


def invoice_view(res: Reservation, hs: HotelSettings, calendar: RateCalendar | None,
                 now: datetime | None = None) -> InvoiceView:
    invoice = res.final_invoice
    if invoice is not None:
        folio: Folio = build_folio(res, hs, None)
        return InvoiceView(
            number=invoice.number, title="FACTURE", title_ar="فاتورة", is_final=True, issued_at=invoice.issued_at,
            customer_name=invoice.customer_name, customer_details=invoice.customer_details, reservation=res,
            lines=[line for line in folio.lines if line.invoiced], vat_breakdown=folio.vat_breakdown,
            total_ht=invoice.total_ht, total_vat=invoice.total_vat, tourist_tax=invoice.tourist_tax,
            total_ttc=invoice.total_ttc, paid=invoice.paid, balance=invoice.balance, payments=folio.payments,
        )
    folio = build_folio(res, hs, calendar)
    name, details = customer_block(res)
    return InvoiceView(
        number=f"PROFORMA-{res.code}", title="FACTURE PROFORMA", title_ar="فاتورة شكلية", is_final=False,
        issued_at=now, customer_name=name, customer_details=details, reservation=res, lines=folio.lines,
        vat_breakdown=folio.vat_breakdown, total_ht=folio.total_ht, total_vat=folio.total_vat,
        tourist_tax=folio.tax_total, total_ttc=folio.total_ttc, paid=folio.paid, balance=folio.balance,
        payments=folio.payments,
    )
