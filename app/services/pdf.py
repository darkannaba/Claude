"""Documents PDF bilingues français / arabe (ReportLab + DejaVu Sans, mise en forme arabe et bidi).

- Fiche de police (voyageur principal, accompagnateurs, fiches individuelles)
- Facture A4 et ticket 80 mm
- Addition de point de vente 80 mm
- Rapport de clôture journalière (audit de nuit)
"""
from __future__ import annotations

import re
from datetime import date, datetime
from io import BytesIO
from xml.sax.saxutils import escape

import arabic_reshaper
from bidi import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.config import config
from app.models.customer import GENDERS, ID_TYPES, ID_TYPES_AR
from app.models.invoice import ITEM_KINDS, PAYMENT_METHODS
from app.services.amount_words import amount_in_words
from app.services.invoicing import InvoiceView
from app.services.qr import qr_drawing
from app.services.settings import HotelSettings

FONT = "DejaVuSans"
FONT_BOLD = "DejaVuSans-Bold"


def _register_fonts() -> None:
    if FONT in pdfmetrics.getRegisteredFontNames():
        return
    pdfmetrics.registerFont(TTFont(FONT, str(config.fonts_dir / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(config.fonts_dir / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT, boldItalic=FONT_BOLD)


_register_fonts()

ARABIC = re.compile("[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
ACCENT = colors.HexColor("#0f766e")
GRID = colors.HexColor("#cbd5e1")
HEAD_BG = colors.HexColor("#f1f5f9")


def shape(text) -> str:
    """Mise en forme arabe (lettres liées) puis ordre d'affichage bidirectionnel."""
    value = "" if text is None else str(text)
    if ARABIC.search(value):
        return get_display(arabic_reshaper.reshape(value))
    return value


def _style(name: str, size: float = 9, bold: bool = False, align: int = TA_LEFT, color=colors.black,
           leading: float | None = None) -> ParagraphStyle:
    return ParagraphStyle(name, fontName=FONT_BOLD if bold else FONT, fontSize=size,
                          leading=leading or size * 1.35, alignment=align, textColor=color)


S = {
    "base": _style("base"),
    "small": _style("small", 7.5),
    "muted": _style("muted", 7.5, color=colors.HexColor("#475569")),
    "bold": _style("bold", 9, True),
    "right": _style("right", 9, align=TA_RIGHT),
    "right_bold": _style("right_bold", 9, True, TA_RIGHT),
    "center": _style("center", 9, align=TA_CENTER),
    "title": _style("title", 14, True, TA_CENTER, ACCENT),
    "title_ar": _style("title_ar", 13, True, TA_CENTER, ACCENT),
    "h2": _style("h2", 10, True, color=ACCENT),
    "hotel": _style("hotel", 12, True),
    "hotel_ar": _style("hotel_ar", 12, True, TA_RIGHT),
    "ar_right": _style("ar_right", 9, align=TA_RIGHT),
}


def P(text, style: str | ParagraphStyle = "base") -> Paragraph:
    st = S[style] if isinstance(style, str) else style
    return Paragraph(escape(shape(text)).replace("\n", "<br/>"), st)


def bi(fr: str, ar: str, style: str = "bold") -> Paragraph:
    """Libellé bilingue : français à gauche, arabe ensuite (mis en forme)."""
    return Paragraph(f"{escape(fr)} <font size='7.5' color='#475569'>/ {escape(shape(ar))}</font>", S[style])


def money(amount, symbol: str) -> str:
    return f"{int(round(amount or 0)):,}".replace(",", " ") + f" {symbol}"


def d(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y")
    return value.strftime("%d/%m/%Y") if isinstance(value, date) else ""


def _table(rows, widths, header: bool = False, grid: bool = True, extra=None) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 3),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if grid:
        style.append(("GRID", (0, 0), (-1, -1), 0.4, GRID))
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), HEAD_BG))
    style.extend(extra or [])
    table.setStyle(TableStyle(style))
    return table


def _hotel_header(hs: HotelSettings, width: float, qr_data: str | None = None) -> Table:
    left = [P(hs.hotel_name, "hotel"), P(hs.hotel_address, "small")]
    legal = "   ".join(f"{label} {value}" for label, value in
                       (("NIF", hs.hotel_nif), ("NIS", hs.hotel_nis), ("RC", hs.hotel_rc), ("AI", hs.hotel_ai)) if value)
    if legal:
        left.append(P(legal, "muted"))
    contact = "   ".join(v for v in (hs.hotel_phone, hs.hotel_email) if v)
    if contact:
        left.append(P(contact, "muted"))
    right = [P(hs.hotel_name_ar, "hotel_ar"), P(hs.hotel_address_ar, "ar_right")]
    cells = [left, right]
    widths = [width * 0.5, width * 0.5]
    if qr_data:
        cells.append(qr_drawing(qr_data, 22 * mm))
        widths = [width * 0.42, width * 0.4, width * 0.18]
    return _table([cells], widths, grid=False, extra=[("VALIGN", (0, 0), (-1, -1), "TOP"),
                                                      ("ALIGN", (-1, 0), (-1, 0), "RIGHT"),
                                                      ("LINEBELOW", (0, 0), (-1, 0), 1, ACCENT)])


# ======================================================================== Fiche de police
def _person_block(person, is_guest: bool, width: float) -> Table:
    birth = " – ".join(v for v in (d(person.birth_date), person.birth_place) if v)
    id_type = person.id_type
    rows = [
        [bi("Nom", "اللقب"), P(person.last_name.upper()), bi("Prénom", "الاسم"), P(person.first_name)],
        [bi("Nom et prénom (arabe)", "الاسم واللقب"), P(f"{person.last_name_ar} {person.first_name_ar}".strip(), "ar_right"),
         bi("Date / lieu de naissance", "تاريخ ومكان الميلاد"), P(birth)],
        [bi("Nationalité", "الجنسية"), P(person.nationality),
         bi("Pièce d'identité", "وثيقة الهوية"), P(f"{ID_TYPES.get(id_type, id_type)} / {ID_TYPES_AR.get(id_type, '')}")],
        [bi("N° de la pièce", "رقم الوثيقة"), P(person.id_number, "bold"),
         bi("Délivrée le / à", "تاريخ ومكان الإصدار"),
         P(" – ".join(v for v in (d(getattr(person, "id_issue_date", None)), getattr(person, "id_issue_place", "")) if v))],
    ]
    if is_guest:
        rows += [
            [bi("Sexe", "الجنس"), P(GENDERS.get(person.gender, "")), bi("Profession", "المهنة"), P(person.profession)],
            [bi("Domicile habituel", "العنوان المعتاد"), P(person.address), bi("Téléphone", "الهاتف"), P(person.phone)],
        ]
    else:
        rows.append([bi("Lien avec le voyageur", "صلة القرابة"), P(person.relationship_label), "", ""])
    w = width
    return _table(rows, [w * 0.2, w * 0.3, w * 0.2, w * 0.3],
                  extra=[("BACKGROUND", (0, 0), (0, -1), HEAD_BG), ("BACKGROUND", (2, 0), (2, -1), HEAD_BG)])


def _stay_block(res, width: float) -> Table:
    w = width
    rows = [
        [bi("N° de réservation", "رقم الحجز"), P(res.code, "bold"), bi("Chambre", "الغرفة"), P(res.room.number, "bold")],
        [bi("Date d'arrivée", "تاريخ الوصول"), P(d(res.check_in)), bi("Départ prévu", "تاريخ المغادرة"), P(d(res.check_out))],
        [bi("Venant de", "قادم من"), P(res.coming_from), bi("Se rendant à", "متوجه إلى"), P(res.going_to)],
        [bi("Adultes", "البالغون"), P(res.adults), bi("Enfants", "الأطفال"), P(res.children)],
    ]
    return _table(rows, [w * 0.2, w * 0.3, w * 0.2, w * 0.3],
                  extra=[("BACKGROUND", (0, 0), (0, -1), HEAD_BG), ("BACKGROUND", (2, 0), (2, -1), HEAD_BG)])


def _signatures(width: float, left: tuple[str, str] = ("Signature du voyageur", "إمضاء المسافر"),
                right: tuple[str, str] = ("Visa de la réception", "تأشيرة الاستقبال")) -> Table:
    table = Table(
        [[bi(*left), bi(*right)], ["", ""]],
        colWidths=[width / 2, width / 2], rowHeights=[None, 22 * mm])
    table.setStyle(TableStyle([("LINEBELOW", (0, 1), (-1, 1), 0.5, GRID), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return table


def police_form_pdf(hs: HotelSettings, res, printed_at: datetime) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm,
                            bottomMargin=12 * mm, title=f"Fiche de police {res.code}", author=hs.hotel_name)
    width = A4[0] - 28 * mm
    guest = res.guest
    individual = [c for c in res.companions if c.id_number]
    persons = [(guest, True)] + [(c, False) for c in individual]
    story = []
    for index, (person, is_guest) in enumerate(persons):
        if index:
            story.append(PageBreak())
        story += [
            _hotel_header(hs, width, res.code), Spacer(1, 4 * mm),
            P(hs.police_form_title, "title"), P(hs.police_form_title_ar, "title_ar"), Spacer(1, 1 * mm),
            P(hs.police_form_notice, "muted"), Spacer(1, 4 * mm),
            bi("Voyageur principal" if is_guest else "Accompagnateur",
               "المسافر الرئيسي" if is_guest else "المرافق", "h2"),
            Spacer(1, 1.5 * mm), _person_block(person, is_guest, width), Spacer(1, 4 * mm),
            bi("Séjour", "الإقامة", "h2"), Spacer(1, 1.5 * mm), _stay_block(res, width), Spacer(1, 4 * mm),
        ]
        if not is_guest:
            story += [P(f"Accompagne : {guest.full_name} ({ID_TYPES.get(guest.id_type, '')} {guest.id_number})"),
                      Spacer(1, 4 * mm)]
        if is_guest and res.companions:
            header = [bi(*pair, "bold") for pair in (("Nom et prénom", "الاسم واللقب"), ("Naissance", "الميلاد"),
                                                       ("Nationalité", "الجنسية"), ("Lien", "الصلة"),
                                                       ("Pièce", "الوثيقة"))]
            rows = [header]
            for c in res.companions:
                name = c.full_name + (f"\n{c.full_name_ar}" if c.full_name_ar else "")
                rows.append([P(name), P(" – ".join(v for v in (d(c.birth_date), c.birth_place) if v)),
                             P(c.nationality), P(c.relationship_label),
                             P(f"{ID_TYPES.get(c.id_type, c.id_type)}\n{c.id_number}" if c.id_number else "—")])
            story += [bi(f"Accompagnateurs ({len(res.companions)})", "المرافقون", "h2"), Spacer(1, 1.5 * mm),
                      _table(rows, [width * 0.28, width * 0.2, width * 0.16, width * 0.14, width * 0.22], header=True),
                      Spacer(1, 4 * mm)]
        story += [
            KeepTogether([_signatures(width), Spacer(1, 3 * mm), P(hs.police_form_footer, "small"),
                          P(f"Fait à {hs.hotel_address.split(',')[0]}, le {printed_at:%d/%m/%Y à %H:%M}", "small")]),
        ]
    doc.build(story)
    return buffer.getvalue()


# ======================================================================== Facture
def _totals_rows(view: InvoiceView, sym: str, small: bool = False) -> list[list]:
    lbl = "small" if small else "base"
    rows = [
        [bi("Total HT", "المجموع دون رسوم", lbl), P(money(view.total_ht, sym), "right")],
        [bi("TVA", "الرسم على القيمة المضافة", lbl), P(money(view.total_vat, sym), "right")],
    ]
    if view.tourist_tax:
        rows.append([bi("Taxe de séjour", "رسم الإقامة", lbl), P(money(view.tourist_tax, sym), "right")])
    rows += [
        [bi("TOTAL TTC", "المجموع الكلي", "bold"), P(money(view.total_ttc, sym), "right_bold")],
        [bi("Versements", "المدفوعات", lbl), P(money(-view.paid, sym), "right")],
        [bi("SOLDE À PAYER", "المبلغ المتبقي", "bold"), P(money(view.balance, sym), "right_bold")],
    ]
    return rows


def invoice_pdf(hs: HotelSettings, view: InvoiceView, fmt: str = "a4") -> bytes:
    return _ticket_invoice(hs, view) if fmt == "ticket" else _a4_invoice(hs, view)


def _a4_invoice(hs: HotelSettings, view: InvoiceView) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm,
                            bottomMargin=12 * mm, title=view.number, author=hs.hotel_name)
    width = A4[0] - 28 * mm
    sym = hs.currency_symbol
    res = view.reservation
    story = [_hotel_header(hs, width, view.number), Spacer(1, 5 * mm)]

    title = _table([[P(view.title, "title"), P(view.title_ar, "title_ar")],
                    [P(f"N° {view.number}", "center"), P(f"Date : {d(view.issued_at)}", "center")]],
                   [width / 2, width / 2], grid=False)
    customer = [P(view.customer_name, "bold")] + [P(line, "small") for line in view.customer_details.splitlines()]
    stay = [P(f"Réservation {res.code} – Chambre {res.room.number}", "small"),
            P(f"Séjour du {d(res.check_in)} au {d(res.check_out)} ({res.nights} nuit(s))", "small"),
            P(f"{res.adults} adulte(s), {res.children} enfant(s)", "small")]
    story += [title, Spacer(1, 3 * mm),
              _table([[bi("Client", "الزبون", "bold"), bi("Séjour", "الإقامة", "bold")], [customer, stay]],
                     [width / 2, width / 2], header=True, extra=[("VALIGN", (0, 1), (-1, 1), "TOP")]),
              Spacer(1, 4 * mm)]

    rows = [[bi("Date", "التاريخ"), bi("Désignation", "البيان"), bi("TVA", "ر.ق.م"), bi("Montant TTC", "المبلغ")]]
    for line in view.lines:
        rows.append([P(d(line.business_date), "small"), P(line.description, "small"),
                     P(f"{line.vat_rate} %" if line.kind != "tax" else "—", "right"), P(money(line.amount, sym), "right")])
    story += [_table(rows, [width * 0.13, width * 0.57, width * 0.1, width * 0.2], header=True), Spacer(1, 3 * mm)]

    vat_rows = [[bi("Taux", "النسبة"), bi("Base HT", "الأساس"), bi("TVA", "الرسم")]]
    for rate, (ht, vat) in sorted(view.vat_breakdown.items()):
        vat_rows.append([P(f"{rate} %"), P(money(ht, sym), "right"), P(money(vat, sym), "right")])
    vat_table = _table(vat_rows, [width * 0.12, width * 0.17, width * 0.15], header=True)
    totals_table = _table(_totals_rows(view, sym), [width * 0.32, width * 0.2],
                          extra=[("BACKGROUND", (0, -1), (-1, -1), HEAD_BG), ("BACKGROUND", (0, -3), (-1, -3), HEAD_BG)])
    story += [_table([[vat_table, totals_table]], [width * 0.46, width * 0.54], grid=False,
                     extra=[("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]),
              Spacer(1, 4 * mm)]

    story.append(P(f"Arrêtée la présente facture à la somme de : {amount_in_words(view.total_ttc)} "
                   f"({money(view.total_ttc, sym)} TTC).", "base"))
    if view.payments:
        story += [Spacer(1, 3 * mm), bi("Règlements", "المدفوعات", "h2")]
        for payment in view.payments:
            story.append(P(f"{payment.paid_at:%d/%m/%Y} – {payment.kind_label} – {payment.method_label} : "
                           f"{money(payment.signed_amount, sym)}" + (f" (réf. {payment.reference})" if payment.reference else ""),
                           "small"))
    if not view.is_final:
        story += [Spacer(1, 3 * mm), P("Document provisoire : les nuitées marquées « prévisionnel » ne sont pas encore "
                                       "clôturées et peuvent évoluer jusqu'au départ.", "muted")]
    story += [Spacer(1, 6 * mm), P(hs.invoice_footer, "center")]
    doc.build(story)
    return buffer.getvalue()


def _ticket_invoice(hs: HotelSettings, view: InvoiceView) -> bytes:
    buffer = BytesIO()
    height = max(120 * mm, (115 + 10 * len(view.lines) + 6 * len(view.payments)) * mm)
    width = 72 * mm
    doc = SimpleDocTemplate(buffer, pagesize=(80 * mm, height), leftMargin=4 * mm, rightMargin=4 * mm,
                            topMargin=4 * mm, bottomMargin=4 * mm, title=view.number)
    sym = hs.currency_symbol
    res = view.reservation
    small_center = _style("tc", 7.5, align=TA_CENTER)
    story = [P(hs.hotel_name, _style("th", 11, True, TA_CENTER)), P(hs.hotel_name_ar, _style("tha", 10, True, TA_CENTER)),
             P(hs.hotel_address, small_center)]
    if hs.hotel_nif:
        story.append(P(f"NIF {hs.hotel_nif}", small_center))
    story += [Spacer(1, 2 * mm), P(f"{view.title} / {view.title_ar}", _style("tt", 10, True, TA_CENTER)),
              P(f"N° {view.number} – {d(view.issued_at)}", small_center), Spacer(1, 2 * mm),
              P(f"Client : {view.customer_name}", "small"),
              P(f"Ch. {res.room.number} – du {d(res.check_in)} au {d(res.check_out)}", "small"), Spacer(1, 2 * mm)]
    rows = [[P(line.description, "small"), P(money(line.amount, sym), _style("tr", 7.5, align=TA_RIGHT))]
            for line in view.lines]
    story += [_table(rows, [width * 0.66, width * 0.34], grid=False,
                     extra=[("LINEBELOW", (0, 0), (-1, -1), 0.25, GRID)]), Spacer(1, 2 * mm),
              _table(_totals_rows(view, sym, small=True), [width * 0.6, width * 0.4], grid=False,
                     extra=[("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.black)]), Spacer(1, 3 * mm),
              qr_drawing(res.code, 22 * mm), P(res.code, small_center), Spacer(1, 2 * mm),
              P(hs.invoice_footer, small_center)]
    doc.build(story)
    return buffer.getvalue()


# ======================================================================== Point de vente
def pos_ticket_pdf(hs: HotelSettings, order) -> bytes:
    buffer = BytesIO()
    height = max(110 * mm, (85 + len(order.lines) * 7) * mm)
    width = 72 * mm
    doc = SimpleDocTemplate(buffer, pagesize=(80 * mm, height), leftMargin=4 * mm, rightMargin=4 * mm,
                            topMargin=4 * mm, bottomMargin=4 * mm, title=order.number)
    sym = hs.currency_symbol
    center = _style("pc", 7.5, align=TA_CENTER)
    story = [P(hs.hotel_name, _style("ph", 11, True, TA_CENTER)), P(hs.hotel_name_ar, _style("pha", 10, True, TA_CENTER)),
             P(order.outlet_label, _style("po", 9, True, TA_CENTER)),
             P(f"Addition {order.number} – {order.created_at:%d/%m/%Y %H:%M}", center)]
    if order.label:
        story.append(P(order.label, center))
    rows = [[P(f"{line.quantity} × {line.description}", "small"), P(money(line.amount, sym), _style("pr", 7.5, align=TA_RIGHT))]
            for line in order.lines]
    rows.append([bi("TOTAL TTC", "المجموع", "bold"), P(money(order.total, sym), "right_bold")])
    story += [Spacer(1, 2 * mm), _table(rows, [width * 0.64, width * 0.36], grid=False,
                                        extra=[("LINEABOVE", (0, -1), (-1, -1), 0.6, colors.black)]), Spacer(1, 2 * mm)]
    if order.status == "room_charge" and order.reservation is not None:
        story += [P(f"Imputé sur la chambre {order.reservation.room.number} ({order.reservation.code})", "small"),
                  Spacer(1, 8 * mm), P("Signature du client : ____________________", "small")]
    elif order.status == "paid":
        story.append(P(f"Réglé : {PAYMENT_METHODS.get(order.payment_method, order.payment_method)}", "small"))
    story += [Spacer(1, 2 * mm), qr_drawing(order.number, 18 * mm), P(hs.invoice_footer, center)]
    doc.build(story)
    return buffer.getvalue()


# ======================================================================== Clôture journalière
def daily_report_pdf(hs: HotelSettings, summary: dict) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm,
                            bottomMargin=12 * mm, title=f"Clôture {summary['day']}")
    width = A4[0] - 28 * mm
    sym = hs.currency_symbol
    kpi = summary["kpi"]
    run = summary["run"]
    story = [_hotel_header(hs, width), Spacer(1, 5 * mm),
             P(f"RAPPORT DE CLÔTURE JOURNALIÈRE – {d(summary['day'])}", "title"),
             P("تقرير الإقفال اليومي", "title_ar"), Spacer(1, 4 * mm)]
    status = (f"Audit de nuit exécuté le {run.run_at:%d/%m/%Y à %H:%M} par {run.run_by}" if run
              else "Journée non encore clôturée par l'audit de nuit (chiffres provisoires).")
    story += [P(status, "muted"), Spacer(1, 4 * mm)]
    indicators = [
        [bi("Indicateur", "المؤشر"), bi("Valeur", "القيمة")],
        [P("Taux d'occupation (TO)"), P(f"{kpi.occupancy} %", "right")],
        [P("Chambres vendues / disponibles"), P(f"{kpi.sold_nights} / {kpi.available_nights}", "right")],
        [P("Prix moyen par chambre (ADR)"), P(money(kpi.adr, sym), "right")],
        [P("RevPAR"), P(money(kpi.revpar, sym), "right")],
        [P("Arrivées / départs"), P(f"{summary['arrivals']} / {summary['departures']}", "right")],
    ]
    if run:
        indicators.append([P("No-show déclarés"), P(str(run.no_shows), "right")])
    revenue = [[bi("Chiffre d'affaires", "رقم الأعمال"), bi("Montant TTC", "المبلغ")]]
    for kind, label in ITEM_KINDS.items():
        revenue.append([P(label), P(money(summary["by_kind"].get(kind, 0), sym), "right")])
    revenue.append([P("Total folio", "bold"), P(money(summary["items_total"], sym), "right_bold")])
    payments = [[bi("Encaissements", "المقبوضات"), bi("Montant", "المبلغ")]]
    for method, label in PAYMENT_METHODS.items():
        payments.append([P(label), P(money(summary["payments"].get(method, 0), sym), "right")])
    payments.append([P("Total encaissé", "bold"), P(money(sum(summary["payments"].values()), sym), "right_bold")])
    half = width / 2 - 3 * mm
    story += [_table(indicators, [half * 0.6, half * 0.4], header=True), Spacer(1, 4 * mm),
              _table([[_table(revenue, [half * 0.6, half * 0.4], header=True),
                       _table(payments, [half * 0.6, half * 0.4], header=True)]],
                     [width / 2, width / 2], grid=False, extra=[("VALIGN", (0, 0), (-1, -1), "TOP")]),
              Spacer(1, 10 * mm),
              _signatures(width, ("Réceptionniste de nuit", "موظف الاستقبال الليلي"), ("Direction", "الإدارة"))]
    doc.build(story)
    return buffer.getvalue()
