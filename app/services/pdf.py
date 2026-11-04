"""Génération PDF (ReportLab) : fiche de police, facture A4 et ticket 80 mm."""
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.models.guests import ID_TYPES
from app.models.reservations import CHARGE_CATEGORIES, PAYMENT_KINDS

_ss = getSampleStyleSheet()
_BASE = ParagraphStyle("base", parent=_ss["Normal"], fontName="Helvetica", fontSize=9, leading=12)
_SMALL = ParagraphStyle("small", parent=_BASE, fontSize=7.5, leading=9.5)
_TITLE = ParagraphStyle("title", parent=_BASE, fontName="Helvetica-Bold", fontSize=13, leading=16, alignment=1)
_BOLD = ParagraphStyle("bold", parent=_BASE, fontName="Helvetica-Bold")
_GRID = colors.HexColor("#94a3b8")
_HEAD_BG = colors.HexColor("#e2e8f0")


def _p(text, style=_BASE) -> Paragraph:
    value = "" if text is None else str(text)  # 0 doit rester affiché (ex. « Enfants : 0 »)
    return Paragraph(escape(value).replace("\n", "<br/>"), style)


def money(amount, symbol: str) -> str:
    return f"{int(round(amount or 0)):,}".replace(",", " ") + f" {symbol}"


def _date(d) -> str:
    return d.strftime("%d/%m/%Y") if d else ""


def police_form_pdf(settings: dict, res, guest, companions, room) -> bytes:
    """Fiche de police : identité du voyageur principal et des compagnons."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
        title=f"Fiche de police {res.code}", author=settings["hotel_name"],
    )
    story = [
        _p(settings["hotel_name"], _BOLD),
        _p(settings["hotel_address"], _SMALL),
        Spacer(1, 4 * mm),
        _p(settings["police_form_title"], _TITLE),
        Spacer(1, 2 * mm),
        _p(settings["police_form_notice"], _SMALL),
        Spacer(1, 4 * mm),
    ]

    info = Table(
        [
            [_p("N° de réservation", _BOLD), _p(res.code), _p("Chambre", _BOLD), _p(room.number)],
            [_p("Date d'arrivée", _BOLD), _p(_date(res.check_in)), _p("Date de départ", _BOLD), _p(_date(res.check_out))],
            [_p("Adultes", _BOLD), _p(res.adults), _p("Enfants", _BOLD), _p(res.children)],
        ],
        colWidths=[38 * mm, 52 * mm, 38 * mm, 52 * mm],
    )
    info.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, _GRID), ("BACKGROUND", (0, 0), (0, -1), _HEAD_BG),
                              ("BACKGROUND", (2, 0), (2, -1), _HEAD_BG), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [info, Spacer(1, 5 * mm)]

    header = ["N°", "Nom", "Prénom", "Date de naissance", "Nationalité", "Type de pièce", "N° de pièce"]
    rows = [[_p(h, _BOLD) for h in header]]
    persons = [(guest.last_name, guest.first_name, guest.birth_date, guest.nationality, guest.id_type, guest.id_number)]
    persons += [(c.last_name, c.first_name, c.birth_date, c.nationality, c.id_type, c.id_number) for c in companions]
    for idx, (last, first, birth, nat, id_type, id_number) in enumerate(persons, start=1):
        rows.append([_p(idx), _p(last), _p(first), _p(_date(birth)), _p(nat),
                     _p(ID_TYPES.get(id_type, id_type)), _p(id_number)])
    persons_table = Table(rows, colWidths=[8 * mm, 30 * mm, 28 * mm, 25 * mm, 26 * mm, 35 * mm, 38 * mm], repeatRows=1)
    persons_table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, _GRID), ("BACKGROUND", (0, 0), (-1, 0), _HEAD_BG),
                                       ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [persons_table, Spacer(1, 3 * mm), _p(f"Adresse du voyageur principal : {guest.address or '—'}"),
              _p(f"Téléphone : {guest.phone or '—'}"), Spacer(1, 12 * mm)]

    sign = Table([[_p("Signature du voyageur", _BOLD), _p("Visa de la réception", _BOLD)], ["", ""]],
                 colWidths=[88 * mm, 88 * mm], rowHeights=[8 * mm, 22 * mm])
    sign.setStyle(TableStyle([("LINEBELOW", (0, 1), (-1, 1), 0.5, _GRID), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [sign, Spacer(1, 6 * mm), _p(settings["police_form_footer"], _SMALL)]

    doc.build(story)
    return buf.getvalue()


def invoice_pdf(settings: dict, res, folio, invoice_number: str, fmt: str = "a4") -> bytes:
    """Facture au format A4 ou ticket de caisse 80 mm (fmt='ticket')."""
    sym = settings["currency_symbol"]
    is_final = res.status == "checked_out"
    title = "FACTURE" if is_final else "FACTURE PROVISOIRE"
    buf = BytesIO()

    if fmt == "ticket":
        width = 80 * mm
        rows_estimate = len(folio.room_lines) + len(folio.charges) + len(folio.payments) + 12
        height = max(120 * mm, (95 + rows_estimate * 6.5) * mm)
        doc = SimpleDocTemplate(buf, pagesize=(width, height), leftMargin=4 * mm, rightMargin=4 * mm,
                                topMargin=4 * mm, bottomMargin=4 * mm, title=invoice_number)
        body_w = 72 * mm
        base = ParagraphStyle("t_base", parent=_BASE, fontSize=7.5, leading=9.5)
        bold = ParagraphStyle("t_bold", parent=base, fontName="Helvetica-Bold")
        center = ParagraphStyle("t_center", parent=base, alignment=1)
        widths = [body_w * 0.68, body_w * 0.32]
    else:
        doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                                topMargin=14 * mm, bottomMargin=14 * mm, title=invoice_number)
        body_w = A4[0] - 30 * mm
        base, bold, center = _BASE, _BOLD, ParagraphStyle("c", parent=_BASE, alignment=1)
        widths = [body_w * 0.58, body_w * 0.12, body_w * 0.30]

    guest = res.guest
    story = [
        Paragraph(f"<b>{escape(settings['hotel_name'])}</b>", center),
        _p(settings["hotel_address"], center),
    ]
    if settings["hotel_nif"] or settings["hotel_rc"]:
        story.append(_p(f"NIF : {settings['hotel_nif']}   RC : {settings['hotel_rc']}", center))
    if settings["hotel_phone"]:
        story.append(_p(f"Tél. : {settings['hotel_phone']}", center))
    story += [Spacer(1, 3 * mm), _p(title, ParagraphStyle("ft", parent=bold, alignment=1, fontSize=11, leading=14)),
              _p(f"N° {invoice_number}", center), _p(f"Date : {_date(res.checked_out_at or res.check_out)}", center),
              Spacer(1, 2 * mm),
              _p(f"Client : {guest.full_name}", base), _p(f"Chambre : {res.room.number}", base),
              _p(f"Séjour : du {_date(res.check_in)} au {_date(res.check_out)} ({folio.nights} nuit(s))", base),
              Spacer(1, 3 * mm)]

    lines = [[_p("Désignation", bold), _p("Qté", bold) if fmt != "ticket" else _p("", bold), _p("Montant TTC", bold)]]
    if fmt == "ticket":
        lines = [[_p("Désignation", bold), _p("Montant", bold)]]
    for line in folio.room_lines:
        label = f"Chambre {line.room} ({line.room_type}) – {line.nights} nuit(s)"
        row = [_p(label, base), _p(str(line.nights), base), _p(money(line.amount, sym), base)]
        lines.append(row if fmt != "ticket" else [_p(label, base), _p(money(line.amount, sym), base)])
    for charge in folio.charges:
        cat = CHARGE_CATEGORIES.get(charge.category, "Extra")
        label = f"{charge.label} ({cat})"
        row = [_p(label, base), _p("1", base), _p(money(charge.amount_ttc, sym), base)]
        lines.append(row if fmt != "ticket" else [_p(label, base), _p(money(charge.amount_ttc, sym), base)])

    table = Table(lines, colWidths=widths if fmt == "ticket" else [body_w * 0.58, body_w * 0.12, body_w * 0.30],
                  repeatRows=1)
    table.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, _GRID), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"), ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 2),
    ]))
    story += [table, Spacer(1, 3 * mm)]

    totals = [
        ("Total prestations TTC", folio.taxable_ttc),
        ("Dont base HT", folio.taxable_ht),
        (f"TVA ({folio.vat_rate} %)", folio.vat_amount),
    ]
    if folio.tourist_tax:
        totals.append(("Taxe de séjour", folio.tourist_tax))
    totals.append(("TOTAL À PAYER", folio.total_due))
    for kind_label, amount in [("Versements", folio.paid)]:
        totals.append((kind_label, -amount))
    totals.append(("SOLDE", folio.balance))

    tot_rows = [[_p(label, bold if label.isupper() or label == "SOLDE" else base),
                 _p(money(amount, sym), bold if label.isupper() or label == "SOLDE" else base)]
                for label, amount in totals]
    tot_table = Table(tot_rows, colWidths=[widths[0], widths[-1]] if fmt == "ticket" else [body_w * 0.70, body_w * 0.30])
    tot_table.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("LINEABOVE", (0, -2), (-1, -2), 0.5, _GRID),
                                   ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    story.append(tot_table)

    if folio.payments:
        story += [Spacer(1, 3 * mm), _p("Détail des versements", bold)]
        for pay in folio.payments:
            story.append(_p(f"{_date(pay.paid_at.date())} – {PAYMENT_KINDS.get(pay.kind, pay.kind)} : "
                            f"{money(pay.amount, sym)}", base))

    story += [Spacer(1, 5 * mm), _p("Merci de votre visite et à bientôt.", center)]
    doc.build(story)
    return buf.getvalue()
