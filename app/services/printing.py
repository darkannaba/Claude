"""Impression directe : tickets ESC/POS (imprimantes thermiques 80 mm) et PDF vers imprimante réseau (RAW 9100).

Langues des tickets : français (``fr``, défaut) ou anglais (``en``). Les imprimantes thermiques n'ont pas de police
arabe (le flux est réduit à l'ASCII) : une demande en arabe (``ar``) ou bilingue (``bi``) est imprimée en anglais.
"""
from __future__ import annotations

import asyncio
import socket
import unicodedata

from app.models.invoice import PAYMENT_KINDS
from app.models.pos import PosOrder
from app.services.common import BusinessError, format_money
from app.services.i18n import t
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


def receipt_lang(value) -> str:
    """Langue d'un ticket ESC/POS : « fr » (défaut) ou « en » ; l'arabe et le bilingue retombent sur l'anglais."""
    code = (value or "").strip().lower() if isinstance(value, str) else ""
    if code in ("", "fr"):
        return "fr"
    return "en" if code in ("en", "ar", "bi") else "fr"


# Libellés des tickets : texte source français → anglais (les tickets sont imprimés sans accents, voir _ascii).
_RECEIPT_EN = {
    "Tel.": "Tel.",
    "Client :": "Customer:",
    "Chambre {room} - du {start} au {end}": "Room {room} - from {start} to {end}",
    "Total HT": "Total excl. tax",
    "TVA": "VAT",
    "Taxe de sejour": "Tourist tax",
    "TOTAL TTC": "TOTAL INCL. TAX",
    "SOLDE": "BALANCE",
    "Addition": "Bill",
    "Impute chambre": "Charged to room",
    "Signature client :": "Customer signature:",
    "Regle :": "Paid:",
}


def _label(text: str, lang: str) -> str:
    """Libellé de ticket dans la langue demandée (dictionnaire local, puis catalogue i18n, sinon texte source)."""
    if lang != "en":
        return text
    if text in _RECEIPT_EN:
        return _RECEIPT_EN[text]
    return t(text, "en")


def _amount(amount, sym: str) -> str:
    return format_money(amount, sym).replace(" ", " ")


def _header(printer: EscPos, hs: HotelSettings, lang: str = "fr") -> None:
    printer.align("center").bold().size(True).text(hs.hotel_name).size(False).bold(False)
    printer.text(hs.hotel_address)
    if hs.hotel_phone:
        printer.text(f"{_label('Tel.', lang)} {hs.hotel_phone}")
    if hs.hotel_nif:
        printer.text(f"NIF {hs.hotel_nif}")
    printer.align("left").rule()


def invoice_receipt(view: InvoiceView, hs: HotelSettings, lang: str = "fr") -> bytes:
    """Ticket 80 mm d'une facture / proforma, en français (défaut) ou en anglais.

    Les imprimantes thermiques n'ont pas de police arabe : ``lang="ar"`` (ou ``"bi"``) imprime en anglais.
    """
    lang = receipt_lang(lang)
    printer = EscPos(hs.receipt_width_chars)
    sym = hs.currency_symbol
    _header(printer, hs, lang)
    res = view.reservation
    printer.align("center").bold().text(_label(view.title, lang)).bold(False).text(f"N {view.number}").align("left")
    printer.text(f"{_label('Client :', lang)} {view.customer_name}")
    printer.text(_label("Chambre {room} - du {start} au {end}", lang).format(
        room=res.room.number, start=f"{res.check_in:%d/%m/%Y}", end=f"{res.check_out:%d/%m/%Y}"))
    printer.rule()
    for line in view.lines:
        printer.pair(_label(line.description, lang), _amount(line.amount, sym))
    printer.rule()
    printer.pair(_label("Total HT", lang), _amount(view.total_ht, sym))
    printer.pair(_label("TVA", lang), _amount(view.total_vat, sym))
    if view.tourist_tax:
        printer.pair(_label("Taxe de sejour", lang), _amount(view.tourist_tax, sym))
    printer.bold().pair(_label("TOTAL TTC", lang), _amount(view.total_ttc, sym)).bold(False)
    for payment in view.payments:
        printer.pair(f"{_label(PAYMENT_KINDS.get(payment.kind, payment.kind), lang)} {payment.paid_at:%d/%m}",
                     _amount(payment.signed_amount, sym))
    printer.bold().pair(_label("SOLDE", lang), _amount(view.balance, sym)).bold(False)
    printer.rule().align("center").text(_label(hs.invoice_footer, lang)).text(res.code).feed(4).cut()
    return printer.bytes()


def pos_receipt(order: PosOrder, hs: HotelSettings, lang: str = "fr") -> bytes:
    """Ticket 80 mm d'une addition de point de vente, en français (défaut) ou en anglais.

    Les imprimantes thermiques n'ont pas de police arabe : ``lang="ar"`` (ou ``"bi"``) imprime en anglais.
    """
    lang = receipt_lang(lang)
    printer = EscPos(hs.receipt_width_chars)
    sym = hs.currency_symbol
    _header(printer, hs, lang)
    printer.align("center").bold().text(_label(order.outlet_label, lang)).bold(False)
    printer.text(f"{_label('Addition', lang)} {order.number}").align("left")
    if order.label:
        printer.text(order.label)
    printer.text(f"{order.created_at:%d/%m/%Y %H:%M}").rule()
    for line in order.lines:
        printer.pair(f"{line.quantity} x {line.description}", _amount(line.amount, sym))
    printer.rule().bold().pair(_label("TOTAL TTC", lang), _amount(order.total, sym)).bold(False)
    if order.status == "room_charge" and order.reservation is not None:
        printer.text(f"{_label('Impute chambre', lang)} {order.reservation.room.number} ({order.reservation.code})")
        printer.feed(1).text(_label("Signature client :", lang)).feed(2)
    elif order.status == "paid":
        printer.text(f"{_label('Regle :', lang)} {order.payment_method}")
    printer.align("center").text(_label(hs.invoice_footer, lang)).feed(4).cut()
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
