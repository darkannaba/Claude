"""Impression directe : tickets ESC/POS (imprimantes thermiques 80 mm) et PDF vers imprimante réseau (RAW 9100)."""
from __future__ import annotations

import asyncio
import socket
import unicodedata

from app.models.invoice import PAYMENT_KINDS
from app.models.pos import PosOrder
from app.services.common import BusinessError, format_money
from app.services.invoicing import InvoiceView
from app.services.settings import HotelSettings

ESC = b"\x1b"
GS = b"\x1d"


def _ascii(text: str) -> str:
    """Les imprimantes thermiques n'ont pas de police arabe : on conserve le latin, accents simplifiés."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if ord(ch) < 128 and not unicodedata.combining(ch))


class EscPos:
    """Constructeur minimal de flux ESC/POS (compatible Epson TM, Xprinter, Bixolon…)."""

    def __init__(self, width: int = 42):
        self.width = width
        self.buffer = bytearray(ESC + b"@")  # initialisation

    def align(self, mode: str) -> "EscPos":
        self.buffer += ESC + b"a" + {"left": b"\x00", "center": b"\x01", "right": b"\x02"}[mode]
        return self

    def bold(self, on: bool = True) -> "EscPos":
        self.buffer += ESC + b"E" + (b"\x01" if on else b"\x00")
        return self

    def size(self, double: bool = False) -> "EscPos":
        self.buffer += GS + b"!" + (b"\x11" if double else b"\x00")
        return self

    def text(self, line: str = "") -> "EscPos":
        self.buffer += _ascii(line).encode("ascii", "replace") + b"\n"
        return self

    def pair(self, left: str, right: str) -> "EscPos":
        left, right = _ascii(left), _ascii(right)
        space = self.width - len(right) - 1
        if len(left) > space:
            self.text(left)
            return self.text(right.rjust(self.width))
        return self.text(left.ljust(space) + " " + right)

    def rule(self, char: str = "-") -> "EscPos":
        return self.text(char * self.width)

    def feed(self, lines: int = 3) -> "EscPos":
        self.buffer += ESC + b"d" + bytes([lines])
        return self

    def cut(self) -> "EscPos":
        self.buffer += GS + b"V" + b"\x41" + b"\x03"
        return self

    def bytes(self) -> bytes:
        return bytes(self.buffer)


def _header(printer: EscPos, hs: HotelSettings) -> None:
    printer.align("center").bold().size(True).text(hs.hotel_name).size(False).bold(False)
    printer.text(hs.hotel_address)
    if hs.hotel_phone:
        printer.text(f"Tel. {hs.hotel_phone}")
    if hs.hotel_nif:
        printer.text(f"NIF {hs.hotel_nif}")
    printer.align("left").rule()


def invoice_receipt(view: InvoiceView, hs: HotelSettings) -> bytes:
    printer = EscPos(hs.receipt_width_chars)
    sym = hs.currency_symbol
    _header(printer, hs)
    res = view.reservation
    printer.align("center").bold().text(view.title).bold(False).text(f"N {view.number}").align("left")
    printer.text(f"Client : {view.customer_name}")
    printer.text(f"Chambre {res.room.number} - du {res.check_in:%d/%m/%Y} au {res.check_out:%d/%m/%Y}")
    printer.rule()
    for line in view.lines:
        printer.pair(line.description, format_money(line.amount, sym).replace(" ", " "))
    printer.rule()
    printer.pair("Total HT", format_money(view.total_ht, sym).replace(" ", " "))
    printer.pair("TVA", format_money(view.total_vat, sym).replace(" ", " "))
    if view.tourist_tax:
        printer.pair("Taxe de sejour", format_money(view.tourist_tax, sym).replace(" ", " "))
    printer.bold().pair("TOTAL TTC", format_money(view.total_ttc, sym).replace(" ", " ")).bold(False)
    for payment in view.payments:
        printer.pair(f"{PAYMENT_KINDS.get(payment.kind, payment.kind)} {payment.paid_at:%d/%m}",
                     format_money(payment.signed_amount, sym).replace(" ", " "))
    printer.bold().pair("SOLDE", format_money(view.balance, sym).replace(" ", " ")).bold(False)
    printer.rule().align("center").text(hs.invoice_footer).text(res.code).feed(4).cut()
    return printer.bytes()


def pos_receipt(order: PosOrder, hs: HotelSettings) -> bytes:
    printer = EscPos(hs.receipt_width_chars)
    sym = hs.currency_symbol
    _header(printer, hs)
    printer.align("center").bold().text(order.outlet_label).bold(False).text(f"Addition {order.number}").align("left")
    if order.label:
        printer.text(order.label)
    printer.text(f"{order.created_at:%d/%m/%Y %H:%M}").rule()
    for line in order.lines:
        printer.pair(f"{line.quantity} x {line.description}", format_money(line.amount, sym).replace(" ", " "))
    printer.rule().bold().pair("TOTAL TTC", format_money(order.total, sym).replace(" ", " ")).bold(False)
    if order.status == "room_charge" and order.reservation is not None:
        printer.text(f"Impute chambre {order.reservation.room.number} ({order.reservation.code})")
        printer.feed(1).text("Signature client :").feed(2)
    elif order.status == "paid":
        printer.text(f"Regle : {order.payment_method}")
    printer.align("center").text(hs.invoice_footer).feed(4).cut()
    return printer.bytes()


def send_raw(host: str, port: int, data: bytes, timeout: float = 8.0) -> None:
    if not host:
        raise BusinessError("Aucune imprimante réseau configurée (Administration > Paramètres > Impression).")
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.sendall(data)
    except OSError as exc:
        raise BusinessError(f"Imprimante injoignable ({host}:{port}) : {exc.strerror or exc}") from exc


async def print_raw(host: str, port: int, data: bytes) -> None:
    await asyncio.to_thread(send_raw, host, port, data)


def test_page(hs: HotelSettings) -> bytes:
    printer = EscPos(hs.receipt_width_chars)
    _header(printer, hs)
    printer.align("center").bold().text("TEST D'IMPRESSION").bold(False).text("Imprimante ticket operationnelle")
    printer.feed(4).cut()
    return printer.bytes()
