"""Documents PDF trilingues (ReportLab + DejaVu Sans, mise en forme arabe et bidi).

- Fiche de police (voyageur principal, accompagnateurs, fiches individuelles)
- Facture A4 et ticket 80 mm
- Addition de point de vente 80 mm
- Rapport de clôture journalière (audit de nuit)

Chaque générateur accepte ``lang`` ∈ ``fr`` | ``ar`` | ``en`` | ``bi`` :

- ``bi`` (défaut) : rendu bilingue français + arabe (libellés « Français / عربي ») ;
- ``fr`` : français seul ; ``en`` : anglais seul ;
- ``ar`` : arabe seul, alignements à droite, colonnes des tableaux en miroir.

Tous les libellés sont des textes sources français passés par ``t(texte, lang)`` (app/services/i18n.py) ; les
traductions vivent dans ``app/locales/<lang>/pdf.json``. Lorsqu'un même texte français porte, dans un document,
une traduction différente de celle des écrans, la clé prend un suffixe de contexte (``"TVA|col"``) qui n'est
jamais affiché : seule la partie avant ``|`` est le texte français.

Charte de l'établissement (white-label) : chaque générateur accepte ``theme`` (``ThemeSettings`` ou ``None``).
``None`` conserve strictement le rendu historique. Avec une charte :

- logo raster (``theme_manager.logo_path_for_documents``) dans l'en-tête, mis à l'échelle sans déformation
  (≈ 18 mm de haut en A4, 14 mm sur les tickets) : à gauche en fr / en / bi, à droite en arabe (colonnes en miroir) ;
- slogan sous le nom de l'hôtel (``slogan_ar`` en arabe, les deux en bilingue) ;
- couleur d'accent (titres, intertitres, filets) = couleur primaire de la charte, assombrie si elle est trop claire
  pour rester lisible sur fond blanc (contraste < 3:1) ;
- ``document_header`` sous l'en-tête et ``document_footer`` en pied de chaque page A4 (en fin de ticket).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from io import BytesIO
from xml.sax.saxutils import escape

import arabic_reshaper
from bidi.algorithm import get_display  # implémentation Python : miroir correct des parenthèses en texte mixte
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image as PdfImage
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.config import config
from app.models.customer import GENDERS, ID_TYPES, ID_TYPES_AR
from app.models.invoice import ITEM_KINDS, PAYMENT_METHODS
from app.services.amount_words import amount_in_words
from app.services import theme_manager
from app.services.amount_words_en import amount_in_words_en
from app.services.common import BusinessError
from app.services.i18n import t
from app.services.invoicing import InvoiceView
from app.services.qr import qr_drawing
from app.services.settings import HotelSettings

FONT = "DejaVuSans"
FONT_BOLD = "DejaVuSans-Bold"

DOC_LANGS = ("fr", "ar", "en", "bi")


def doc_lang(value, default: str = "bi") -> str:
    """Langue de document validée strictement : valeur inconnue ⇒ ``default``."""
    code = (value or "").strip().lower() if isinstance(value, str) else ""
    return code if code in DOC_LANGS else default


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
    # variantes alignées à droite (mode arabe seul)
    "small_right": _style("small_right", 7.5, align=TA_RIGHT),
    "muted_right": _style("muted_right", 7.5, align=TA_RIGHT, color=colors.HexColor("#475569")),
    "h2_right": _style("h2_right", 10, True, TA_RIGHT, ACCENT),
    # charte de l'établissement : slogan et textes libres d'en-tête / pied de document
    "slogan": _style("slogan", 8.5, True, color=ACCENT),
    "slogan_right": _style("slogan_right", 8.5, True, TA_RIGHT, ACCENT),
    "doc_footer": _style("doc_footer", 7, align=TA_CENTER, color=colors.HexColor("#475569")),
}
# En mode arabe seul, les styles alignés à gauche deviennent alignés à droite.
_RTL_STYLES = {"base": "ar_right", "small": "small_right", "muted": "muted_right", "bold": "right_bold",
               "h2": "h2_right", "hotel": "hotel_ar", "slogan": "slogan_right"}
# Styles dont la couleur suit la couleur primaire de la charte.
_ACCENT_STYLES = ("title", "title_ar", "h2", "h2_right", "slogan", "slogan_right")
WHITE = "#ffffff"
MIN_ACCENT_CONTRAST = 3.0  # titres en gras sur fond blanc : seuil WCAG des textes larges / en gras
A4_LOGO_HEIGHT = 18 * mm
A4_LOGO_MAX_WIDTH = 45 * mm
TICKET_LOGO_HEIGHT = 14 * mm
TICKET_LOGO_MAX_WIDTH = 50 * mm
A4_BOTTOM_MARGIN = 12 * mm
# Hauteur maximale du pied de document de la charte : au-delà, il est réduit à l'échelle (jamais de page sans
# zone utile, quelle que soit la longueur du texte enregistré).
A4_FOOTER_MAX_HEIGHT = 40 * mm


def accent_hex(theme) -> str:
    """Couleur d'accent des documents (« #rrggbb ») : la couleur primaire de la charte, remplacée par sa nuance
    700 lorsqu'elle est trop claire pour être lisible sur papier blanc ; teal historique sans charte."""
    if theme is None:
        return ACCENT.hexval().replace("0x", "#")
    try:
        primary = theme_manager.normalize_hex(theme.primary_color)
    except BusinessError:
        primary = theme_manager.DEFAULT_THEME["primary_color"]
    if theme_manager.contrast_ratio(primary, WHITE) < MIN_ACCENT_CONTRAST:
        return theme_manager.palette(primary)[700]
    return primary


def _themed_styles(accent) -> dict[str, ParagraphStyle]:
    """Copie des styles dont les titres, intertitres et slogans prennent la couleur d'accent de la charte."""
    styles = dict(S)
    for name in _ACCENT_STYLES:
        styles[name] = ParagraphStyle(f"{name}_theme", parent=S[name], textColor=accent)
    return styles


def _logo_source(theme) -> tuple[str, int, int] | None:
    """(chemin, largeur px, hauteur px) du logo raster des documents, ou None (absent, désactivé, SVG sans rendu
    PNG, fichier illisible)."""
    if theme is None:
        return None
    path = theme_manager.logo_path_for_documents(theme)
    if path is None:
        return None
    try:
        width, height = ImageReader(str(path)).getSize()
    except Exception:  # fichier supprimé ou corrompu entre deux requêtes : le document reste imprimable
        return None
    if width <= 0 or height <= 0:
        return None
    return str(path), int(width), int(height)


def _source(text: str) -> str:
    """Texte français d'une clé de traduction (partie avant un éventuel suffixe de contexte « |… »)."""
    return text.split("|", 1)[0]


def money(amount, symbol: str) -> str:
    return f"{int(round(amount or 0)):,}".replace(",", " ") + f" {symbol}"


def d(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y")
    return value.strftime("%d/%m/%Y") if isinstance(value, date) else ""


def _mirror_commands(extra, ncols: int) -> list:
    """Transpose les commandes de style (coordonnées de colonnes) d'un tableau dont les colonnes sont inversées."""
    def col(c: int) -> int:
        c = c if c >= 0 else ncols + c
        return ncols - 1 - c

    mirrored = []
    for command in extra:
        if len(command) >= 3 and isinstance(command[1], tuple) and isinstance(command[2], tuple):
            (c0, r0), (c1, r1) = command[1], command[2]
            a, b = sorted((col(c0), col(c1)))
            mirrored.append((command[0], (a, r0), (b, r1), *command[3:]))
        else:
            mirrored.append(command)
    return mirrored


class _Doc:
    """Contexte de rendu d'un document : langue, traduction des libellés, styles et tableaux adaptés."""

    def __init__(self, lang: str, theme=None):
        self.lang = doc_lang(lang)
        self.rtl = self.lang == "ar"
        self.bilingual = self.lang == "bi"
        self.theme = theme
        if theme is None:
            self.accent = ACCENT
            self.S = S
        else:
            self.accent = colors.HexColor(accent_hex(theme))
            self.S = _themed_styles(self.accent)
        self._logo = _logo_source(theme)

    # ---- charte de l'établissement
    def _theme_text(self, field: str) -> str:
        return str(getattr(self.theme, field, "") or "").strip() if self.theme is not None else ""

    @property
    def has_logo(self) -> bool:
        return self._logo is not None

    def logo(self, max_height: float, max_width: float, align: str = "CENTER") -> PdfImage | None:
        """Logo de l'hôtel réduit pour tenir dans ``max_width`` × ``max_height`` (ratio conservé)."""
        if self._logo is None:
            return None
        path, px_width, px_height = self._logo
        scale = min(max_height / px_height, max_width / px_width)
        image = PdfImage(path, width=px_width * scale, height=px_height * scale)
        image.hAlign = align
        return image

    def slogan(self) -> str:
        """Slogan dans la langue du document (variante arabe en mode ar, repli sur le slogan latin)."""
        if self.rtl:
            return self._theme_text("slogan_ar") or self._theme_text("slogan")
        return self._theme_text("slogan")

    def slogan_ar(self) -> str:
        return self._theme_text("slogan_ar")

    @property
    def header_text(self) -> str:
        return self._theme_text("document_header")

    @property
    def footer_text(self) -> str:
        return self._theme_text("document_footer")

    # ---- libellés
    def t(self, text: str, **fmt) -> str:
        """Libellé dans la langue du document : fr / bi → texte source ; en / ar → traduction (repli : source)."""
        if self.lang in ("fr", "bi"):
            value = _source(text)
        else:
            value = t(text, self.lang)
            if value == text:
                value = _source(text)
        return value.format(**fmt) if fmt else value

    def dyn(self, label) -> str:
        """Libellé dynamique (dictionnaires des modèles, titres de vue) : traduit en en / ar, inchangé sinon."""
        label = "" if label is None else str(label)
        if self.lang in ("fr", "bi") or not label:
            return label
        return t(label, self.lang)

    def legal(self, fr_value: str, ar_value: str = "") -> str:
        """Mention paramétrée (titres, mentions légales) : variante arabe en mode ar, traduction en anglais si
        elle existe dans les catalogues, sinon le texte configuré."""
        if self.lang == "ar":
            return ar_value or t(fr_value, "ar")
        if self.lang == "en":
            return t(fr_value, "en")
        return fr_value

    def words(self, amount: int, symbol: str) -> str:
        """Mention du montant en toutes lettres selon la langue du document."""
        if self.lang == "en":
            return self.t("Arrêtée la présente facture à la somme de : {words} ({amount} TTC).",
                          words=amount_in_words_en(amount), amount=self.money(amount, symbol))
        if self.lang == "ar":
            # Montant chiffré, puis version française en toutes lettres entre parenthèses.
            return f"{self.t('Arrêtée la présente facture à la somme de :')} {self.money(amount, symbol)} ({amount_in_words(amount)})"
        return (f"Arrêtée la présente facture à la somme de : {amount_in_words(amount)} "
                f"({money(amount, symbol)} TTC).")

    # ---- paragraphes
    def style(self, style: str | ParagraphStyle) -> ParagraphStyle:
        if not isinstance(style, str):
            return style
        return self.S[_RTL_STYLES.get(style, style)] if self.rtl else self.S[style]

    def P(self, text, style: str | ParagraphStyle = "base") -> Paragraph:
        """Paragraphe de valeur (texte brut, chaque ligne mise en forme si elle contient de l'arabe)."""
        value = "" if text is None else str(text)
        return Paragraph("<br/>".join(escape(shape(line)) for line in value.split("\n")), self.style(style))

    def money(self, amount, symbol: str) -> str:
        """Montant formaté ; en arabe, marque gauche-droite initiale et séparateur de milliers insécable pour que
        l'algorithme bidi garde « 25 300 DA » d'un seul tenant dans une phrase arabe."""
        value = money(amount, symbol)
        return "\u200e" + value.replace(" ", "\u00a0") if self.rtl else value

    def L(self, text: str, style: str | ParagraphStyle = "base", **fmt) -> Paragraph:
        """Paragraphe d'un libellé traduit."""
        return self.P(self.t(text, **fmt), style)

    def bi(self, text: str, style: str = "bold", **fmt) -> Paragraph:
        """Libellé : « français / arabe » en mode bilingue, sinon libellé dans la langue du document."""
        if not self.bilingual:
            return self.L(text, style, **fmt)
        fr = _source(text)
        ar = t(text, "ar")
        if fmt:
            fr = fr.format(**fmt)
            ar = ar.format(**fmt)
        if ar == text or ar == fr:
            return Paragraph(escape(fr), self.S[style])
        return Paragraph(f"{escape(fr)} <font size='7.5' color='#475569'>/ {escape(shape(ar))}</font>", self.S[style])

    def titles(self, fr: str, ar: str, style: str = "title", style_ar: str = "title_ar") -> list:
        """Titre de document : français + arabe en bilingue, arabe seul en ar, traduction (ou texte) sinon."""
        if self.bilingual:
            return [self.P(fr, style), self.P(ar, style_ar)]
        if self.rtl:
            return [self.P(ar or fr, style_ar)]
        return [self.P(self.legal(fr), style)]

    # ---- tableaux
    def table(self, rows, widths, header: bool = False, grid: bool = True, extra=None, row_heights=None) -> Table:
        style = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 3),
                 ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if grid:
            style.append(("GRID", (0, 0), (-1, -1), 0.4, GRID))
        if header:
            style.append(("BACKGROUND", (0, 0), (-1, 0), HEAD_BG))
        style.extend(extra or [])
        if self.rtl:
            rows = [list(reversed(row)) for row in rows]
            widths = list(reversed(widths))
            style = _mirror_commands(style, len(widths))
        table = Table(rows, colWidths=widths, repeatRows=1 if header else 0, rowHeights=row_heights)
        table.setStyle(TableStyle(style))
        return table


def _hotel_header(doc: _Doc, hs: HotelSettings, width: float, qr_data: str | None = None) -> Table:
    legal = "   ".join(f"{label} {value}" for label, value in
                       (("NIF", hs.hotel_nif), ("NIS", hs.hotel_nis), ("RC", hs.hotel_rc), ("AI", hs.hotel_ai)) if value)
    contact = "   ".join(v for v in (hs.hotel_phone, hs.hotel_email) if v)
    if doc.rtl:
        block = [doc.P(hs.hotel_name_ar or hs.hotel_name, "hotel"), doc.P(hs.hotel_address_ar or hs.hotel_address, "small")]
    else:
        block = [doc.P(hs.hotel_name, "hotel"), doc.P(hs.hotel_address, "small")]
    slogan = doc.slogan()
    if slogan:
        block.insert(1, doc.P(slogan, "slogan"))
    if legal:
        block.append(doc.P(legal, "muted"))
    if contact:
        block.append(doc.P(contact, "muted"))
    extra = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, 0), 1, doc.accent)]
    if doc.bilingual:
        right = [doc.P(hs.hotel_name_ar, "hotel_ar"), doc.P(hs.hotel_address_ar, "ar_right")]
        if doc.slogan_ar():
            right.insert(1, doc.P(doc.slogan_ar(), "slogan_right"))
        cells = [block, right]
        widths = [width * 0.5, width * 0.5]
        if qr_data:
            cells.append(qr_drawing(qr_data, 22 * mm))
            widths = [width * 0.42, width * 0.4, width * 0.18]
            extra.append(("ALIGN", (-1, 0), (-1, 0), "RIGHT"))
    else:
        cells = [block]
        widths = [width]
        if qr_data:
            cells.append(qr_drawing(qr_data, 22 * mm))
            widths = [width * 0.82, width * 0.18]
            extra.append(("ALIGN", (-1, 0), (-1, 0), "LEFT" if doc.rtl else "RIGHT"))
    # Logo de la charte : première colonne logique (à gauche en fr / en / bi ; doc.table la place à droite en
    # arabe). La colonne du logo est prélevée sur les colonnes de texte, la colonne du QR code garde sa largeur.
    logo = doc.logo(A4_LOGO_HEIGHT, A4_LOGO_MAX_WIDTH, "RIGHT" if doc.rtl else "LEFT")
    if logo is not None:
        logo_width = logo.drawWidth + 6 * mm
        text_columns = len(widths) - (1 if qr_data else 0)
        text_width = sum(widths[:text_columns])
        ratio = (text_width - logo_width) / text_width
        widths = [logo_width] + [w * ratio for w in widths[:text_columns]] + widths[text_columns:]
        cells = [logo] + cells
        extra.append(("ALIGN", (0, 0), (0, 0), "RIGHT" if doc.rtl else "LEFT"))
    return doc.table([cells], widths, grid=False, extra=extra)


def _header_flowables(doc: _Doc, hs: HotelSettings, width: float, qr_data: str | None = None) -> list:
    """En-tête A4 complet : bloc de l'hôtel puis texte libre d'en-tête de la charte (s'il est renseigné)."""
    flowables = [_hotel_header(doc, hs, width, qr_data)]
    if doc.header_text:
        flowables += [Spacer(1, 2 * mm), doc.P(doc.header_text, "small")]
    return flowables


def _page_decorator(doc: _Doc, width: float, left_margin: float):
    """(callback de page, marge basse) : pied de document de la charte dessiné au bas de chaque page A4.

    Sans pied personnalisé, aucun callback et marge basse historique (pagination inchangée).
    """
    text = doc.footer_text
    if not text:
        return None, A4_BOTTOM_MARGIN

    def layout() -> tuple[Paragraph, float, float]:
        """(paragraphe mis en page, échelle, hauteur dessinée). Trop haut, le pied est réduit à l'échelle : il
        est recomposé sur une largeur élargie de 1/échelle puis dessiné réduit, jusqu'à tenir dans le plafond."""
        footer = doc.P(text, "doc_footer")
        _, natural = footer.wrap(width, A4[1])
        scale = 1.0
        for _ in range(40):
            if natural * scale <= A4_FOOTER_MAX_HEIGHT:
                break
            scale *= 0.97 * A4_FOOTER_MAX_HEIGHT / (natural * scale)
            footer = doc.P(text, "doc_footer")
            _, natural = footer.wrap(width / scale, 1e6)
        return footer, scale, min(natural * scale, A4_FOOTER_MAX_HEIGHT)

    _, _, height = layout()
    gap = 2.5 * mm

    def draw(canvas, _document) -> None:
        canvas.saveState()
        footer, scale, _ = layout()
        canvas.translate(left_margin, A4_BOTTOM_MARGIN)
        canvas.scale(scale, scale)
        footer.drawOn(canvas, 0, 0)
        canvas.restoreState()
        canvas.saveState()
        canvas.setStrokeColor(doc.accent)
        canvas.setLineWidth(0.6)
        line_y = A4_BOTTOM_MARGIN + height + gap / 2
        canvas.line(left_margin, line_y, left_margin + width, line_y)
        canvas.restoreState()

    return draw, A4_BOTTOM_MARGIN + height + gap


def _build(pdf: SimpleDocTemplate, story: list, decorator) -> None:
    if decorator is None:
        pdf.build(story)
    else:
        pdf.build(story, onFirstPage=decorator, onLaterPages=decorator)


def _ticket_branding_top(doc: _Doc, center: ParagraphStyle) -> list:
    """Haut de ticket 80 mm : logo centré (avant le nom de l'hôtel)."""
    logo = doc.logo(TICKET_LOGO_HEIGHT, TICKET_LOGO_MAX_WIDTH, "CENTER")
    return [logo, Spacer(1, 1.5 * mm)] if logo is not None else []


def _ticket_slogan(doc: _Doc) -> list:
    """Slogan(s) centrés sous le nom de l'hôtel sur un ticket (français et arabe en bilingue)."""
    style = ParagraphStyle("ticket_slogan", parent=doc.S["slogan"], alignment=TA_CENTER, fontSize=7.5, leading=10)
    out = []
    if doc.slogan():
        out.append(doc.P(doc.slogan(), style))
    if doc.bilingual and doc.slogan_ar():
        out.append(doc.P(doc.slogan_ar(), style))
    return out


def _ticket_text(doc: _Doc, text: str, style: ParagraphStyle) -> list:
    return [Spacer(1, 1.5 * mm), doc.P(text, style)] if text else []


def _ticket_branding(doc: _Doc, center: ParagraphStyle) -> dict[str, list]:
    """Éléments de charte d'un ticket 80 mm : logo, slogan(s), texte d'en-tête et pied de document."""
    return {"top": _ticket_branding_top(doc, center), "slogans": _ticket_slogan(doc),
            "header": _ticket_text(doc, doc.header_text, center), "footer": _ticket_text(doc, doc.footer_text, center)}


TICKET_MARGIN = 4 * mm
FRAME_PADDING = 6  # marges internes (points) du cadre par défaut de SimpleDocTemplate


def _ticket_height(story: list, width: float, estimate: float, theme) -> float:
    """Hauteur du rouleau d'un ticket.

    Le contenu (logo, slogans, textes libres, lignes bilingues) est réellement composé et mesuré pour que le
    ticket tienne sur une seule page ; l'estimation historique sert de hauteur minimale.
    """
    inner_width = width - 2 * FRAME_PADDING
    total = 0.0
    for flowable in story:
        _, height = flowable.wrap(inner_width, 10_000 * mm)
        total += height + flowable.getSpaceBefore() + flowable.getSpaceAfter()
    needed = total + 2 * TICKET_MARGIN + 2 * FRAME_PADDING + 6 * mm  # marge de sécurité (arrondis de composition)
    return max(estimate, needed)


def _ticket_document(buffer: BytesIO, height: float, title: str) -> SimpleDocTemplate:
    return SimpleDocTemplate(buffer, pagesize=(80 * mm, height), leftMargin=TICKET_MARGIN, rightMargin=TICKET_MARGIN,
                             topMargin=TICKET_MARGIN, bottomMargin=TICKET_MARGIN, title=title)


def _signatures(doc: _Doc, width: float, left: str = "Signature du voyageur", right: str = "Visa de la réception") -> Table:
    return doc.table([[doc.bi(left), doc.bi(right)], ["", ""]], [width / 2, width / 2], grid=False,
                     extra=[("LINEBELOW", (0, 1), (-1, 1), 0.5, GRID), ("VALIGN", (0, 0), (-1, -1), "TOP")],
                     row_heights=[None, 22 * mm])


# ======================================================================== Fiche de police
def _id_type_label(doc: _Doc, id_type: str) -> str:
    if doc.bilingual:
        return f"{ID_TYPES.get(id_type, id_type)} / {ID_TYPES_AR.get(id_type, '')}"
    if doc.rtl:
        return ID_TYPES_AR.get(id_type) or doc.dyn(ID_TYPES.get(id_type, id_type))
    return doc.dyn(ID_TYPES.get(id_type, id_type))


def _person_block(doc: _Doc, person, is_guest: bool, width: float) -> Table:
    birth = " – ".join(v for v in (d(person.birth_date), person.birth_place) if v)
    issue = " – ".join(v for v in (d(getattr(person, "id_issue_date", None)), getattr(person, "id_issue_place", "")) if v)
    rows = [
        [doc.bi("Nom|pdf"), doc.P(person.last_name.upper()), doc.bi("Prénom"), doc.P(person.first_name)],
        [doc.bi("Nom et prénom (arabe)"), doc.P(f"{person.last_name_ar} {person.first_name_ar}".strip(), "ar_right"),
         doc.bi("Date / lieu de naissance"), doc.P(birth)],
        [doc.bi("Nationalité"), doc.P(person.nationality),
         doc.bi("Pièce d'identité"), doc.P(_id_type_label(doc, person.id_type))],
        [doc.bi("N° de la pièce"), doc.P(person.id_number, "bold"), doc.bi("Délivrée le / à"), doc.P(issue)],
    ]
    # Date d'expiration de la pièce et mention de numérisation (lecture MRZ / OCR validée à l'accueil).
    expiry = getattr(person, "id_expiry_date", None)
    scanned_at = getattr(person, "id_scanned_at", None)
    if is_guest or expiry:
        scanned = ""
        if scanned_at:
            stamp = f"{scanned_at:%d/%m/%Y %H:%M}"
            scanned = doc.t("Pièce numérisée le {date}", date=stamp)
            if doc.bilingual:  # français puis arabe, une ligne chacun
                scanned += "\n" + t("Pièce numérisée le {date}", "ar").format(date=stamp)
        rows.append([doc.bi("Date d'expiration"), doc.P(d(expiry), "bold"),
                     doc.bi("Numérisation") if scanned_at else "", doc.P(scanned) if scanned else ""])
    if is_guest:
        rows += [
            [doc.bi("Sexe"), doc.P(doc.dyn(GENDERS.get(person.gender, ""))), doc.bi("Profession"), doc.P(person.profession)],
            [doc.bi("Domicile habituel"), doc.P(person.address), doc.bi("Téléphone"), doc.P(person.phone)],
        ]
    else:
        rows.append([doc.bi("Lien avec le voyageur"), doc.P(person.relationship_label), "", ""])
    w = width
    return doc.table(rows, [w * 0.2, w * 0.3, w * 0.2, w * 0.3],
                     extra=[("BACKGROUND", (0, 0), (0, -1), HEAD_BG), ("BACKGROUND", (2, 0), (2, -1), HEAD_BG)])


def _stay_block(doc: _Doc, res, width: float) -> Table:
    w = width
    rows = [
        [doc.bi("N° de réservation"), doc.P(res.code, "bold"), doc.bi("Chambre"), doc.P(res.room.number, "bold")],
        [doc.bi("Date d'arrivée"), doc.P(d(res.check_in)), doc.bi("Départ prévu"), doc.P(d(res.check_out))],
        [doc.bi("Venant de"), doc.P(res.coming_from), doc.bi("Se rendant à"), doc.P(res.going_to)],
        [doc.bi("Adultes"), doc.P(res.adults), doc.bi("Enfants"), doc.P(res.children)],
    ]
    return doc.table(rows, [w * 0.2, w * 0.3, w * 0.2, w * 0.3],
                     extra=[("BACKGROUND", (0, 0), (0, -1), HEAD_BG), ("BACKGROUND", (2, 0), (2, -1), HEAD_BG)])


def police_form_pdf(hs: HotelSettings, res, printed_at: datetime, lang: str = "bi", theme=None) -> bytes:
    doc = _Doc(lang, theme)
    buffer = BytesIO()
    width = A4[0] - 28 * mm
    decorator, bottom = _page_decorator(doc, width, 14 * mm)
    pdf = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm,
                            bottomMargin=bottom, title=f"Fiche de police {res.code}", author=hs.hotel_name)
    guest = res.guest
    individual = [c for c in res.companions if c.id_number]
    persons = [(guest, True)] + [(c, False) for c in individual]
    story = []
    for index, (person, is_guest) in enumerate(persons):
        if index:
            story.append(PageBreak())
        story += [*_header_flowables(doc, hs, width, res.code), Spacer(1, 4 * mm)]
        story += doc.titles(hs.police_form_title, hs.police_form_title_ar)
        story += [
            Spacer(1, 1 * mm), doc.P(doc.legal(hs.police_form_notice), "muted"), Spacer(1, 4 * mm),
            doc.bi("Voyageur principal" if is_guest else "Accompagnateur", "h2"),
            Spacer(1, 1.5 * mm), _person_block(doc, person, is_guest, width), Spacer(1, 4 * mm),
            doc.bi("Séjour|pdf", "h2"), Spacer(1, 1.5 * mm), _stay_block(doc, res, width), Spacer(1, 4 * mm),
        ]
        if not is_guest:
            story += [doc.L("Accompagne : {name} ({id_type} {id_number})", name=guest.full_name,
                            id_type=_id_type_label(doc, guest.id_type) if not doc.bilingual else ID_TYPES.get(guest.id_type, ""),
                            id_number=guest.id_number),
                      Spacer(1, 4 * mm)]
        if is_guest and res.companions:
            header = [doc.bi(text) for text in ("Nom et prénom", "Naissance|pdf", "Nationalité", "Lien|pdf", "Pièce|pdf")]
            rows = [header]
            for c in res.companions:
                name = c.full_name + (f"\n{c.full_name_ar}" if c.full_name_ar else "")
                piece = f"{_id_type_label(doc, c.id_type) if not doc.bilingual else ID_TYPES.get(c.id_type, c.id_type)}\n{c.id_number}"
                rows.append([doc.P(name), doc.P(" – ".join(v for v in (d(c.birth_date), c.birth_place) if v)),
                             doc.P(c.nationality), doc.P(c.relationship_label),
                             doc.P(piece if c.id_number else "—")])
            story += [doc.bi("Accompagnateurs ({count})", "h2", count=len(res.companions)), Spacer(1, 1.5 * mm),
                      doc.table(rows, [width * 0.28, width * 0.2, width * 0.16, width * 0.14, width * 0.22], header=True),
                      Spacer(1, 4 * mm)]
        story += [
            KeepTogether([_signatures(doc, width), Spacer(1, 3 * mm), doc.P(doc.legal(hs.police_form_footer), "small"),
                          doc.L("Fait à {place}, le {date} à {time}", "small", place=hs.hotel_address.split(",")[0],
                                date=f"{printed_at:%d/%m/%Y}", time=f"{printed_at:%H:%M}")]),
        ]
    _build(pdf, story, decorator)
    return buffer.getvalue()


# ======================================================================== Facture
def _totals_rows(doc: _Doc, view: InvoiceView, sym: str, small: bool = False) -> list[list]:
    lbl = "small" if small else "base"
    rows = [
        [doc.bi("Total HT|pdf", lbl), doc.P(doc.money(view.total_ht, sym), "right")],
        [doc.bi("TVA|pdf", lbl), doc.P(doc.money(view.total_vat, sym), "right")],
    ]
    if view.tourist_tax:
        rows.append([doc.bi("Taxe de séjour", lbl), doc.P(doc.money(view.tourist_tax, sym), "right")])
    rows += [
        [doc.bi("TOTAL TTC"), doc.P(doc.money(view.total_ttc, sym), "right_bold")],
        [doc.bi("Versements", lbl), doc.P(doc.money(-view.paid, sym), "right")],
        [doc.bi("SOLDE À PAYER"), doc.P(doc.money(view.balance, sym), "right_bold")],
    ]
    return rows


def invoice_pdf(hs: HotelSettings, view: InvoiceView, fmt: str = "a4", lang: str = "bi", theme=None) -> bytes:
    return _ticket_invoice(hs, view, lang, theme) if fmt == "ticket" else _a4_invoice(hs, view, lang, theme)


def _invoice_title(doc: _Doc, view: InvoiceView) -> str:
    """Titre de facture en une ligne (ticket) : « FACTURE / فاتورة », arabe seul, ou traduction."""
    if doc.bilingual:
        return f"{view.title} / {view.title_ar}"
    if doc.rtl:
        return view.title_ar or view.title
    return doc.dyn(view.title)


def _a4_invoice(hs: HotelSettings, view: InvoiceView, lang: str, theme=None) -> bytes:
    doc = _Doc(lang, theme)
    buffer = BytesIO()
    width = A4[0] - 28 * mm
    decorator, bottom = _page_decorator(doc, width, 14 * mm)
    pdf = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm,
                            bottomMargin=bottom, title=view.number, author=hs.hotel_name)
    sym = hs.currency_symbol
    res = view.reservation
    story = [*_header_flowables(doc, hs, width, view.number), Spacer(1, 5 * mm)]

    if doc.bilingual:
        heading = [doc.P(view.title, "title"), doc.P(view.title_ar, "title_ar")]
        heading_style = []
    else:
        heading = [doc.P(_invoice_title(doc, view), "title_ar" if doc.rtl else "title"), ""]
        if doc.rtl:
            heading.reverse()  # les colonnes sont inversées par doc.table : le titre doit rester en première cellule
        heading_style = [("SPAN", (0, 0), (1, 0))]
    title = doc.table([heading, [doc.L("N° {number}", "center", number=view.number),
                                 doc.L("Date : {date}", "center", date=d(view.issued_at))]],
                      [width / 2, width / 2], grid=False, extra=heading_style)
    customer = [doc.P(view.customer_name, "bold")] + [doc.P(line, "small") for line in view.customer_details.splitlines()]
    stay = [doc.L("Réservation {code} – Chambre {room}", "small", code=res.code, room=res.room.number),
            doc.L("Séjour du {start} au {end} ({nights} nuit(s))", "small", start=d(res.check_in), end=d(res.check_out),
                  nights=res.nights),
            doc.L("{adults} adulte(s), {children} enfant(s)", "small", adults=res.adults, children=res.children)]
    story += [title, Spacer(1, 3 * mm),
              doc.table([[doc.bi("Client|pdf"), doc.bi("Séjour|pdf")], [customer, stay]],
                        [width / 2, width / 2], header=True, extra=[("VALIGN", (0, 1), (-1, 1), "TOP")]),
              Spacer(1, 4 * mm)]

    rows = [[doc.bi("Date"), doc.bi("Désignation"), doc.bi("TVA|col"), doc.bi("Montant TTC|pdf")]]
    for line in view.lines:
        rows.append([doc.P(d(line.business_date), "small"), doc.P(doc.dyn(line.description), "small"),
                     doc.P(f"{line.vat_rate} %" if line.kind != "tax" else "—", "right"), doc.P(doc.money(line.amount, sym), "right")])
    story += [doc.table(rows, [width * 0.13, width * 0.57, width * 0.1, width * 0.2], header=True), Spacer(1, 3 * mm)]

    vat_rows = [[doc.bi("Taux"), doc.bi("Base HT"), doc.bi("TVA|taux")]]
    for rate, (ht, vat) in sorted(view.vat_breakdown.items()):
        vat_rows.append([doc.P(f"{rate} %"), doc.P(doc.money(ht, sym), "right"), doc.P(doc.money(vat, sym), "right")])
    vat_table = doc.table(vat_rows, [width * 0.12, width * 0.17, width * 0.15], header=True)
    totals_table = doc.table(_totals_rows(doc, view, sym), [width * 0.32, width * 0.2],
                             extra=[("BACKGROUND", (0, -1), (-1, -1), HEAD_BG), ("BACKGROUND", (0, -3), (-1, -3), HEAD_BG)])
    story += [doc.table([[vat_table, totals_table]], [width * 0.46, width * 0.54], grid=False,
                        extra=[("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                               ("ALIGN", (0, 0), (0, 0), "LEFT")]),
              Spacer(1, 4 * mm)]

    story.append(doc.P(doc.words(view.total_ttc, sym), "base"))
    if view.payments:
        story += [Spacer(1, 3 * mm), doc.bi("Règlements", "h2")]
        for payment in view.payments:
            text = (f"{payment.paid_at:%d/%m/%Y} – {doc.dyn(payment.kind_label)} – {doc.dyn(payment.method_label)} : "
                    f"{doc.money(payment.signed_amount, sym)}")
            if payment.reference:
                text += " " + doc.t("(réf. {reference})", reference=payment.reference)
            story.append(doc.P(text, "small"))
    if not view.is_final:
        story += [Spacer(1, 3 * mm), doc.L("Document provisoire : les nuitées marquées « prévisionnel » ne sont pas encore "
                                           "clôturées et peuvent évoluer jusqu'au départ.", "muted")]
    story += [Spacer(1, 6 * mm), doc.P(doc.legal(hs.invoice_footer), "center")]
    _build(pdf, story, decorator)
    return buffer.getvalue()


def _ticket_invoice(hs: HotelSettings, view: InvoiceView, lang: str, theme=None) -> bytes:
    doc = _Doc(lang, theme)
    buffer = BytesIO()
    width = 72 * mm
    small_center = _style("tc", 7.5, align=TA_CENTER)
    branding = _ticket_branding(doc, small_center)
    estimate = max(120 * mm, (115 + 10 * len(view.lines) + 6 * len(view.payments)) * mm)
    sym = hs.currency_symbol
    res = view.reservation
    story = branding["top"] + _ticket_hotel_lines(doc, hs, small_center, branding["slogans"])
    if hs.hotel_nif:
        story.append(doc.P(f"NIF {hs.hotel_nif}", small_center))
    story += branding["header"]
    story += [Spacer(1, 2 * mm), doc.P(_invoice_title(doc, view), _style("tt", 10, True, TA_CENTER)),
              doc.L("N° {number} – {date}", small_center, number=view.number, date=d(view.issued_at)), Spacer(1, 2 * mm),
              doc.L("Client : {name}", "small", name=view.customer_name),
              doc.L("Ch. {room} – du {start} au {end}", "small", room=res.room.number, start=d(res.check_in),
                    end=d(res.check_out)), Spacer(1, 2 * mm)]
    rows = [[doc.P(doc.dyn(line.description), "small"), doc.P(doc.money(line.amount, sym), _style("tr", 7.5, align=TA_RIGHT))]
            for line in view.lines]
    story += [doc.table(rows, [width * 0.66, width * 0.34], grid=False,
                        extra=[("LINEBELOW", (0, 0), (-1, -1), 0.25, GRID)]), Spacer(1, 2 * mm),
              doc.table(_totals_rows(doc, view, sym, small=True), [width * 0.6, width * 0.4], grid=False,
                        extra=[("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.black)]), Spacer(1, 3 * mm),
              qr_drawing(res.code, 22 * mm), doc.P(res.code, small_center), Spacer(1, 2 * mm),
              doc.P(doc.legal(hs.invoice_footer), small_center)]
    story += branding["footer"]
    pdf = _ticket_document(buffer, _ticket_height(story, width, estimate, theme), view.number)
    pdf.build(story)
    return buffer.getvalue()


def _ticket_hotel_lines(doc: _Doc, hs: HotelSettings, small_center: ParagraphStyle, slogans: list | None = None) -> list:
    """En-tête d'un ticket 80 mm : nom de l'hôtel (français et/ou arabe selon la langue), slogan(s) de la charte
    et adresse."""
    name_fr = _style("th", 11, True, TA_CENTER)
    name_ar = _style("tha", 10, True, TA_CENTER)
    slogans = slogans or []
    if doc.bilingual:
        return [doc.P(hs.hotel_name, name_fr), doc.P(hs.hotel_name_ar, name_ar), *slogans,
                doc.P(hs.hotel_address, small_center)]
    if doc.rtl:
        return [doc.P(hs.hotel_name_ar or hs.hotel_name, name_fr), *slogans,
                doc.P(hs.hotel_address_ar or hs.hotel_address, small_center)]
    return [doc.P(hs.hotel_name, name_fr), *slogans, doc.P(hs.hotel_address, small_center)]


# ======================================================================== Point de vente
def pos_ticket_pdf(hs: HotelSettings, order, lang: str = "bi", theme=None) -> bytes:
    doc = _Doc(lang, theme)
    buffer = BytesIO()
    width = 72 * mm
    center = _style("pc", 7.5, align=TA_CENTER)
    branding = _ticket_branding(doc, center)
    estimate = max(110 * mm, (85 + len(order.lines) * 7) * mm)
    sym = hs.currency_symbol
    name_fr = _style("ph", 11, True, TA_CENTER)
    name_ar = _style("pha", 10, True, TA_CENTER)
    story = list(branding["top"])
    if doc.bilingual:
        story += [doc.P(hs.hotel_name, name_fr), doc.P(hs.hotel_name_ar, name_ar)]
    elif doc.rtl:
        story += [doc.P(hs.hotel_name_ar or hs.hotel_name, name_fr)]
    else:
        story += [doc.P(hs.hotel_name, name_fr)]
    story += branding["slogans"] + branding["header"]
    story += [doc.P(doc.dyn(order.outlet_label), _style("po", 9, True, TA_CENTER)),
              doc.L("Addition {number} – {date}", center, number=order.number, date=f"{order.created_at:%d/%m/%Y %H:%M}")]
    if order.label:
        story.append(doc.P(order.label, center))
    rows = [[doc.P(f"{line.quantity} × {line.description}", "small"), doc.P(doc.money(line.amount, sym), _style("pr", 7.5, align=TA_RIGHT))]
            for line in order.lines]
    rows.append([doc.bi("TOTAL TTC|pos"), doc.P(doc.money(order.total, sym), "right_bold")])
    story += [Spacer(1, 2 * mm), doc.table(rows, [width * 0.64, width * 0.36], grid=False,
                                           extra=[("LINEABOVE", (0, -1), (-1, -1), 0.6, colors.black)]), Spacer(1, 2 * mm)]
    if order.status == "room_charge" and order.reservation is not None:
        story += [doc.L("Imputé sur la chambre {room} ({code})", "small", room=order.reservation.room.number,
                        code=order.reservation.code),
                  Spacer(1, 8 * mm), doc.L("Signature du client : ____________________", "small")]
    elif order.status == "paid":
        story.append(doc.L("Réglé : {method}", "small",
                           method=doc.dyn(PAYMENT_METHODS.get(order.payment_method, order.payment_method))))
    story += [Spacer(1, 2 * mm), qr_drawing(order.number, 18 * mm), doc.P(doc.legal(hs.invoice_footer), center)]
    story += branding["footer"]
    pdf = _ticket_document(buffer, _ticket_height(story, width, estimate, theme), order.number)
    pdf.build(story)
    return buffer.getvalue()


# ======================================================================== Clôture journalière
def daily_report_pdf(hs: HotelSettings, summary: dict, lang: str = "bi", theme=None) -> bytes:
    doc = _Doc(lang, theme)
    buffer = BytesIO()
    width = A4[0] - 28 * mm
    decorator, bottom = _page_decorator(doc, width, 14 * mm)
    pdf = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm,
                            bottomMargin=bottom, title=f"Clôture {summary['day']}")
    sym = hs.currency_symbol
    kpi = summary["kpi"]
    run = summary["run"]
    story = [*_header_flowables(doc, hs, width), Spacer(1, 5 * mm)]
    title_key = "RAPPORT DE CLÔTURE JOURNALIÈRE"
    if doc.bilingual:
        story += [doc.P(f"{title_key} – {d(summary['day'])}", "title"), doc.P(t(title_key, "ar"), "title_ar")]
    elif doc.rtl:
        story.append(doc.P(f"{doc.t(title_key)} – {d(summary['day'])}", "title_ar"))
    else:
        story.append(doc.P(f"{doc.t(title_key)} – {d(summary['day'])}", "title"))
    story.append(Spacer(1, 4 * mm))
    if run:
        status = doc.t("Audit de nuit exécuté le {date} à {time} par {user}", date=f"{run.run_at:%d/%m/%Y}",
                       time=f"{run.run_at:%H:%M}", user=run.run_by)
    else:
        status = doc.t("Journée non encore clôturée par l'audit de nuit (chiffres provisoires).")
    story += [doc.P(status, "muted"), Spacer(1, 4 * mm)]
    indicators = [
        [doc.bi("Indicateur"), doc.bi("Valeur")],
        [doc.L("Taux d'occupation (TO)"), doc.P(f"{kpi.occupancy} %", "right")],
        [doc.L("Chambres vendues / disponibles"), doc.P(f"{kpi.sold_nights} / {kpi.available_nights}", "right")],
        [doc.L("Prix moyen par chambre (ADR)"), doc.P(doc.money(kpi.adr, sym), "right")],
        [doc.L("RevPAR"), doc.P(doc.money(kpi.revpar, sym), "right")],
        [doc.L("Arrivées / départs"), doc.P(f"{summary['arrivals']} / {summary['departures']}", "right")],
    ]
    if run:
        indicators.append([doc.L("No-show déclarés"), doc.P(str(run.no_shows), "right")])
    revenue = [[doc.bi("Chiffre d'affaires"), doc.bi("Montant TTC|pdf")]]
    for kind, label in ITEM_KINDS.items():
        revenue.append([doc.P(doc.dyn(label)), doc.P(doc.money(summary["by_kind"].get(kind, 0), sym), "right")])
    revenue.append([doc.L("Total folio", "bold"), doc.P(doc.money(summary["items_total"], sym), "right_bold")])
    payments = [[doc.bi("Encaissements"), doc.bi("Montant")]]
    for method, label in PAYMENT_METHODS.items():
        payments.append([doc.P(doc.dyn(label)), doc.P(doc.money(summary["payments"].get(method, 0), sym), "right")])
    payments.append([doc.L("Total encaissé", "bold"), doc.P(doc.money(sum(summary["payments"].values()), sym), "right_bold")])
    half = width / 2 - 3 * mm
    story += [doc.table(indicators, [half * 0.6, half * 0.4], header=True), Spacer(1, 4 * mm),
              doc.table([[doc.table(revenue, [half * 0.6, half * 0.4], header=True),
                          doc.table(payments, [half * 0.6, half * 0.4], header=True)]],
                        [width / 2, width / 2], grid=False, extra=[("VALIGN", (0, 0), (-1, -1), "TOP")]),
              Spacer(1, 10 * mm),
              _signatures(doc, width, "Réceptionniste de nuit", "Direction")]
    _build(pdf, story, decorator)
    return buffer.getvalue()
