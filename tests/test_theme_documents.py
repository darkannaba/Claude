"""Documents émis à la charte de l'établissement : logo, slogan, couleur d'accent, en-tête et pied personnalisés.

Couvre les PDF (fiche de police, facture A4, ticket de facture, addition POS, rapport de clôture) dans les quatre
langues, les routes qui les servent ou les impriment, et les tickets ESC/POS.
"""
import asyncio
import base64
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zlib
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.rl_accel import fp_str
from sqlalchemy import select
from starlette.datastructures import UploadFile

from app.database import SessionLocal
from app.models import PosItem, Room
from app.services import pdf, printing, reports
from app.services import theme_manager as tm
from app.services.invoicing import invoice_view
from app.services.loaders import load_pos_order, load_reservation
from app.services.pricing import load_calendar
from app.services.settings import load_hotel_settings, now_of, today_of

PDFTOTEXT = shutil.which("pdftotext")
LANGS = ("fr", "ar", "en", "bi")
DOCUMENTS = ("police", "invoice", "ticket", "pos", "report")
A4_DOCUMENTS = ("police", "invoice", "report")
IMAGE_MARK = b"/Subtype /Image"
SLOGAN = "Le confort au bord de la mer"
SLOGAN_AR = "الراحة على شاطئ البحر"
HEADER = "Établissement classé 4 étoiles – en-tête ZQH"
FOOTER = "Pied de document personnalisé ZQF – Merci de votre confiance"
DOC_TEXTS = {"slogan": SLOGAN, "slogan_ar": SLOGAN_AR, "document_header": HEADER, "document_footer": FOOTER,
             "show_logo_on_documents": "1"}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- outils
def png_bytes(size=(300, 120), color=(124, 58, 237, 255)) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


SVG_LOGO = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 10">'
            b'<rect width="20" height="10" fill="#7c3aed"/></svg>')


def has_image(data: bytes) -> bool:
    return IMAGE_MARK in data


def decoded_streams(data: bytes) -> list[bytes]:
    """Flux du PDF décodés (ASCII85 éventuel puis FlateDecode) ; les flux indécodables (polices) sont écartés."""
    out = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.DOTALL):
        raw = match.group(1).rstrip(b"\r\n")
        try:
            if raw.endswith(b"~>"):
                raw = base64.a85decode(raw, adobe=True, ignorechars=b" \t\n\r\v")
            out.append(zlib.decompress(raw))
        except (ValueError, zlib.error):
            continue
    return out


def streams(data: bytes) -> bytes:
    return b"\n".join(decoded_streams(data))


def rgb_operator(hex_color: str) -> bytes:
    """Couleur telle que ReportLab l'écrit dans le flux (composantes RGB normalisées 0…1)."""
    return fp_str(*colors.HexColor(hex_color).rgb()).encode()


def text_of(data: bytes) -> str:
    """Texte du PDF : pdftotext (poppler) si disponible, sinon flux décompressés (texte latin seulement)."""
    if PDFTOTEXT:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "doc.pdf"
            path.write_bytes(data)
            return subprocess.run([PDFTOTEXT, "-layout", str(path), "-"], capture_output=True, text=True,
                                  check=True).stdout
    return streams(data).decode("latin-1")


NUMBER = rb"-?(?:\d+\.?\d*|\.\d+)"
GRAPHICS_OPS = re.compile(rb"(?<![\w/.])(q|Q)(?![\w])|((?:" + NUMBER + rb"\s+){6})cm\b|/(\S+)\s+Do\b")


def image_positions(data: bytes) -> list[float]:
    """Abscisses absolues (points) du coin gauche des images dessinées dans les flux de page.

    Suit la matrice de transformation courante (opérateurs q / Q / cm, translations et échelles imbriquées par
    ReportLab) jusqu'à chaque « /Nom Do » ; les chaînes de texte sont neutralisées au préalable.
    """
    identity = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    xs = []
    for flow in decoded_streams(data):
        if b" Do" in flow:
            xs += _positions_in(re.sub(rb"\((?:\\.|[^\\)])*\)", b"()", flow), identity)
    return xs


def _positions_in(flow: bytes, identity: tuple) -> list[float]:
    ctm, stack, xs = identity, [], []
    for match in GRAPHICS_OPS.finditer(flow):
        if match.group(1) == b"q":
            stack.append(ctm)
        elif match.group(1) == b"Q":
            ctm = stack.pop() if stack else identity
        elif match.group(2):
            a, b, c, d, e, f = (float(v) for v in match.group(2).split())
            A, B, C, D, E, F = ctm
            ctm = (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)
        elif match.group(3) and match.group(3).startswith(b"FormXob"):
            xs.append(ctm[4])
    return xs


async def _reset_theme() -> None:
    async with SessionLocal() as db:
        theme = await tm.get_theme(db)
        for kind, field in tm.KIND_FIELDS.items():
            if getattr(theme, field):
                await tm.delete_image(db, kind)
        for field, value in tm.DEFAULT_THEME.items():
            setattr(theme, field, value)
        await db.commit()


@pytest.fixture(autouse=True)
def clean_theme(app):
    run(_reset_theme())
    yield
    run(_reset_theme())


async def _theme():
    async with SessionLocal() as db:
        theme = await tm.get_theme(db)
        await db.commit()
        return theme


def current_theme():
    return run(_theme())


def upload(client, kind, content, filename="logo.png", content_type="image/png"):
    return client.post(f"/admin/theme/upload/{kind}", data={"csrf_token": client.csrf},
                       files={"file": (filename, content, content_type)}, follow_redirects=False)


def set_documents(client, **values):
    response = client.form("/admin/theme/documents", values)
    assert response.status_code == 303, response.text
    return response


# ---------------------------------------------------------------- jeu de données
async def _room_id(number: str) -> int:
    async with SessionLocal() as db:
        return await db.scalar(select(Room.id).where(Room.number == number))


async def _item_id(name: str) -> int:
    async with SessionLocal() as db:
        return await db.scalar(select(PosItem.id).where(PosItem.name == name))


async def _today():
    async with SessionLocal() as db:
        return today_of(await load_hotel_settings(db))


@pytest.fixture(scope="module")
def stay(app):
    """Séjour en cours (voyageur + accompagnateur muni d'une pièce) et addition POS imputée sur la chambre."""
    from tests.conftest import make_client
    admin = make_client(app, "admin")
    reception = make_client(app, "reception")
    today = run(_today())
    assert admin.form("/admin/modules", {"m_restaurant": "1", "m_parking": "1", "m_salle_reunion": "1"}).status_code == 303
    created = reception.form("/reservations", {
        "check_in": today.isoformat(), "check_out": (today + timedelta(days=2)).isoformat(),
        "room_id": str(run(_room_id("309"))),
        "g_last_name": "Benali", "g_first_name": "Samir", "g_last_name_ar": "بن علي", "g_first_name_ar": "سمير",
        "g_phone": "0661000777", "g_id_type": "passport", "g_id_number": "THEME-DOC-001", "g_nationality": "Algérienne",
        "adults": "2", "children": "0", "channel": "Direct", "coming_from": "Oran", "going_to": "Alger",
        "deposit": "3000", "deposit_method": "especes",
    })
    assert created.status_code == 303, created.text
    res_id = int(re.search(r"/reservations/(\d+)$", created.headers["location"]).group(1))
    url = f"/reservations/{res_id}"
    assert reception.form(f"{url}/companions", {"last_name": "Benali", "first_name": "Nadia", "relationship_label": "Épouse",
                                                "id_type": "cni", "id_number": "THEME-COMP-1",
                                                "birth_date": "1990-03-02"}).status_code == 303
    assert reception.form(f"{url}/checkin").status_code == 303
    assert admin.form("/pos/catalog", {"outlet": "restaurant", "name": "Jus d'orange charte", "category": "Boissons",
                                        "price": "250", "vat_rate": "9", "active": "1"}).status_code == 303
    order = reception.form("/pos/orders", {"outlet": "restaurant", "label": "Table 3"})
    order_url = order.headers["location"]
    assert reception.form(f"{order_url}/lines", {"item_id": str(run(_item_id("Jus d'orange charte"))),
                                                 "quantity": "2"}).status_code == 303
    assert reception.form(f"{order_url}/charge", {"reservation_id": str(res_id)}).status_code == 303
    order_id = int(order_url.rstrip("/").rsplit("/", 1)[1])
    return {"res_id": res_id, "url": url, "order_id": order_id, "order_url": order_url, "today": today,
            "reception": reception}


async def _documents(stay: dict, theme, langs=LANGS) -> dict[str, dict[str, bytes]]:
    """{document: {langue: octets}} générés directement par les générateurs avec la charte ``theme``."""
    async with SessionLocal() as db:
        hs = await load_hotel_settings(db)
        res = await load_reservation(db, stay["res_id"])
        order = await load_pos_order(db, stay["order_id"])
        view = invoice_view(res, hs, await load_calendar(db, hs), now_of(hs))
        summary = await reports.daily_summary(db, stay["today"] - timedelta(days=1))
        now = now_of(hs)
        out: dict[str, dict[str, bytes]] = {name: {} for name in DOCUMENTS}
        for lang in langs:
            out["police"][lang] = pdf.police_form_pdf(hs, res, now, lang=lang, theme=theme)
            out["invoice"][lang] = pdf.invoice_pdf(hs, view, "a4", lang=lang, theme=theme)
            out["ticket"][lang] = pdf.invoice_pdf(hs, view, "ticket", lang=lang, theme=theme)
            out["pos"][lang] = pdf.pos_ticket_pdf(hs, order, lang=lang, theme=theme)
            out["report"][lang] = pdf.daily_report_pdf(hs, summary, lang=lang, theme=theme)
        return out


async def _receipts(stay: dict, theme) -> tuple[bytes, bytes]:
    async with SessionLocal() as db:
        hs = await load_hotel_settings(db)
        res = await load_reservation(db, stay["res_id"])
        order = await load_pos_order(db, stay["order_id"])
        view = invoice_view(res, hs, await load_calendar(db, hs), now_of(hs))
        return printing.invoice_receipt(view, hs, lang="fr", theme=theme), printing.pos_receipt(order, hs, lang="fr", theme=theme)


def documents(stay, theme, langs=LANGS):
    return run(_documents(stay, theme, langs))


def assert_valid(docs: dict) -> None:
    for name, versions in docs.items():
        for lang, data in versions.items():
            assert data.startswith(b"%PDF") and data.rstrip().endswith(b"%%EOF"), (name, lang)


# ---------------------------------------------------------------- logo
def test_without_logo_documents_contain_no_image(stay):
    for theme in (None, current_theme()):
        docs = documents(stay, theme)
        assert_valid(docs)
        for name, versions in docs.items():
            for lang, data in versions.items():
                assert not has_image(data), (name, lang)


def test_uploaded_png_logo_on_every_document_and_language(stay, admin):
    assert upload(admin, "logo", png_bytes()).status_code == 303
    theme = current_theme()
    assert theme.logo_file.endswith(".png") and tm.logo_path_for_documents(theme) is not None
    docs = documents(stay, theme)
    assert_valid(docs)
    for name, versions in docs.items():
        for lang, data in versions.items():
            assert has_image(data), (name, lang)
    # Fiche de police : une page par personne munie d'une pièce, le logo est repris sur chacune.
    assert len(image_positions(docs["police"]["fr"])) == 2


def test_logo_saved_through_service_keeps_aspect_ratio(stay):
    async def save():
        async with SessionLocal() as db:
            theme = await tm.save_image(db, "logo", UploadFile(file=BytesIO(png_bytes((800, 200))), filename="l.png"))
            await db.commit()
            return theme

    theme = run(save())
    data = documents(stay, theme, ("fr",))["invoice"]["fr"]
    match = re.search(rb"([\d.]+) 0 0 ([\d.]+) 0 0 cm\s*/FormXob\S+ Do", streams(data))
    assert match, "logo absent du flux de la facture"
    width, height = float(match.group(1)), float(match.group(2))
    assert abs(width / height - 4.0) < 0.01  # 800 × 200 : ratio conservé
    assert height <= pdf.A4_LOGO_HEIGHT + 0.01 and width <= pdf.A4_LOGO_MAX_WIDTH + 0.01


def test_logo_side_follows_language_direction(stay, admin):
    assert upload(admin, "logo", png_bytes()).status_code == 303
    docs = documents(stay, current_theme())
    half = A4[0] / 2
    for name in A4_DOCUMENTS:
        for lang in ("fr", "en", "bi"):
            positions = image_positions(docs[name][lang])
            assert positions and all(x < half for x in positions), (name, lang, positions)
        positions = image_positions(docs[name]["ar"])
        assert positions and all(x > half for x in positions), (name, "ar", positions)


def test_logo_hidden_when_disabled(stay, admin):
    assert upload(admin, "logo", png_bytes()).status_code == 303
    set_documents(admin, slogan=SLOGAN)  # case « afficher le logo » décochée
    theme = current_theme()
    assert theme.show_logo_on_documents is False and tm.logo_path_for_documents(theme) is None
    for name, versions in documents(stay, theme).items():
        for lang, data in versions.items():
            assert not has_image(data), (name, lang)


def test_svg_logo_without_raster_rendition_does_not_break_documents(stay, admin):
    assert upload(admin, "logo", SVG_LOGO, "logo.svg", "image/svg+xml").status_code == 303
    theme = current_theme()
    assert theme.logo_file.endswith(".svg") and tm.logo_path_for_documents(theme) is None
    docs = documents(stay, theme)
    assert_valid(docs)
    assert not any(has_image(data) for versions in docs.values() for data in versions.values())

    # Une déclinaison PNG déposée à côté du SVG est utilisée pour les documents.
    rendition = tm.branding_file(theme, "logo").with_suffix(".png")
    rendition.write_bytes(png_bytes((120, 120)))
    try:
        docs = documents(stay, current_theme(), ("fr", "ar"))
        assert all(has_image(data) for versions in docs.values() for data in versions.values())
    finally:
        rendition.unlink(missing_ok=True)


def test_missing_logo_file_on_disk_is_ignored(stay, admin):
    assert upload(admin, "logo", png_bytes()).status_code == 303
    theme = current_theme()
    path = tm.branding_file(theme, "logo")
    content = path.read_bytes()
    path.write_bytes(b"not an image")
    try:
        docs = documents(stay, theme, ("fr",))
        assert_valid(docs)
        assert not any(has_image(data) for versions in docs.values() for data in versions.values())
    finally:
        path.write_bytes(content)


# ---------------------------------------------------------------- couleur d'accent
def test_primary_colour_drives_titles_and_rules(stay, admin):
    assert admin.form("/admin/theme/colors", {"primary_color": "#7c3aed"}).status_code == 303
    theme = current_theme()
    assert pdf.accent_hex(theme) == "#7c3aed"
    violet, teal = rgb_operator("#7c3aed"), rgb_operator("#0f766e")
    for name in ("police", "invoice", "report"):
        for lang, data in documents(stay, theme)[name].items():
            flows = streams(data)
            assert violet + b" rg" in flows, (name, lang)  # titres et intertitres
            assert violet + b" RG" in flows, (name, lang)  # filet sous l'en-tête
            assert teal not in flows, (name, lang)
    # Sans charte : teal historique.
    assert teal + b" rg" in streams(documents(stay, None, ("fr",))["invoice"]["fr"])


def test_too_light_primary_colour_is_darkened_for_paper(stay, admin):
    assert admin.form("/admin/theme/colors", {"primary_color": "#fde047"}).status_code == 303
    theme = current_theme()
    accent = pdf.accent_hex(theme)
    assert accent == tm.palette("#fde047")[700]
    assert tm.contrast_ratio(accent, "#ffffff") > tm.contrast_ratio("#fde047", "#ffffff")
    flows = streams(documents(stay, theme, ("fr",))["invoice"]["fr"])
    assert rgb_operator(accent) + b" rg" in flows and rgb_operator("#fde047") not in flows
    assert pdf.accent_hex(None) == "#0f766e"


# ---------------------------------------------------------------- textes de la charte
def test_header_footer_and_slogan_in_every_document(stay, admin):
    set_documents(admin, **DOC_TEXTS)
    assert upload(admin, "logo", png_bytes()).status_code == 303
    docs = documents(stay, current_theme())
    assert_valid(docs)
    for name in ("ticket", "pos"):  # la hauteur du ticket intègre logo, slogan, en-tête et pied : une seule page
        for lang, data in docs[name].items():
            assert len(re.findall(rb"/Type\s*/Page[^s]", data)) == 1, (name, lang)
    for name, versions in docs.items():
        for lang in ("fr", "en", "bi"):
            text = text_of(versions[lang])
            for expected in ("ZQH", "ZQF", "Le confort"):  # le slogan peut passer à la ligne (colonne étroite)
                assert expected in text, (name, lang, expected)
        if PDFTOTEXT:
            arabic = text_of(versions["ar"])
            assert "ZQH" in arabic and "ZQF" in arabic, name
            assert "Le confort" not in arabic, name  # en arabe, le slogan arabe remplace le slogan latin
            normalized = unicodedata.normalize("NFKC", arabic)
            assert "البحر" in normalized or "البحر"[::-1] in normalized, name


def test_custom_footer_repeated_on_each_a4_page(stay, admin):
    set_documents(admin, document_footer=FOOTER)
    police = documents(stay, current_theme(), ("fr",))["police"]["fr"]
    assert len(re.findall(rb"/Type\s*/Page[^s]", police)) == 2
    if PDFTOTEXT:
        assert text_of(police).count("ZQF") == 2
    else:
        assert streams(police).count(b"ZQF") == 2


def test_empty_theme_texts_keep_historical_text(stay):
    plain = documents(stay, None, ("fr",))
    themed = documents(stay, current_theme(), ("fr",))
    if not PDFTOTEXT:
        pytest.skip("pdftotext (poppler) absent")
    def without_clock(text: str) -> str:  # « Fait à …, le … à HH:MM » : heure d'impression
        return re.sub(r"\d{2}:\d{2}", "HH:MM", text)

    for name in DOCUMENTS:
        assert without_clock(text_of(plain[name]["fr"])) == without_clock(text_of(themed[name]["fr"])), name


# ---------------------------------------------------------------- routes
def test_pdf_routes_use_the_theme(stay, admin):
    assert upload(admin, "logo", png_bytes()).status_code == 303
    set_documents(admin, **DOC_TEXTS)
    reception = stay["reception"]
    urls = [f"{stay['url']}/police.pdf?lang=fr", f"{stay['url']}/invoice.pdf?lang=ar",
            f"{stay['url']}/invoice.pdf?format=ticket&lang=bi", f"/pos/orders/{stay['order_id']}/ticket.pdf?lang=en",
            f"/night-audit/{(stay['today'] - timedelta(days=1)).isoformat()}/report.pdf?lang=fr"]
    for url in urls:
        response = reception.get(url)
        assert response.status_code == 200, url
        assert response.headers["content-type"] == "application/pdf"
        assert has_image(response.content), url
        assert "ZQF" in text_of(response.content), url


def test_print_routes_pass_the_theme(stay, admin, monkeypatch):
    set_documents(admin, **DOC_TEXTS)
    assert upload(admin, "logo", png_bytes()).status_code == 303
    sent: list[bytes] = []

    async def fake_print(host, port, data):
        sent.append(data)

    monkeypatch.setattr(printing, "print_raw", fake_print)
    reception = stay["reception"]
    for document in ("ticket", "invoice", "police"):
        assert reception.form(f"{stay['url']}/print/{document}", {"lang": "fr"}).status_code == 303
    assert reception.form(f"{stay['order_url']}/print", {"lang": "fr"}).status_code == 303
    ticket, invoice, police, pos_ticket = sent
    assert b"Le confort au bord de la mer" in ticket and b"ZQF" in ticket and b"ZQH" in ticket
    assert has_image(invoice) and has_image(police)
    assert b"Le confort au bord de la mer" in pos_ticket and b"ZQF" in pos_ticket


# ---------------------------------------------------------------- tickets ESC/POS
def test_escpos_receipts_carry_slogan_and_footer(stay, admin):
    plain_invoice, plain_pos = run(_receipts(stay, None))
    empty_invoice, empty_pos = run(_receipts(stay, current_theme()))
    assert (plain_invoice, plain_pos) == (empty_invoice, empty_pos)  # charte sans texte : ticket inchangé

    set_documents(admin, **DOC_TEXTS)
    invoice, pos_ticket = run(_receipts(stay, current_theme()))
    for data in (invoice, pos_ticket):
        assert data.startswith(b"\x1b@") and data.endswith(b"\x1dVA\x03")
        assert b"Le confort au bord de la mer" in data
        assert b"Etablissement classe 4 etoiles" in data  # accents simplifiés
        assert b"ZQF" in data and b"votre confiance" in data
        assert data.index(b"ZQH") < data.index(b"ZQF")
        assert all(byte < 128 for byte in data)  # flux ASCII : le slogan arabe n'est pas imprimé
        assert b"?" * 3 not in data
        # aucune ligne ne dépasse la largeur du ticket
        width = 48
        for line in data.split(b"\n"):
            printable = re.sub(rb"[\x1b\x1d].{1,2}", b"", line)
            assert len(printable) <= width + 2, line


def test_escpos_wraps_long_footer_and_skips_arabic_only_text(stay, admin):
    long_footer = "Conditions générales : " + "séjour " * 30
    set_documents(admin, slogan_ar=SLOGAN_AR, document_footer=long_footer)
    theme = current_theme()
    invoice, _ = run(_receipts(stay, theme))
    plain, _ = run(_receipts(stay, None))
    assert b"Conditions generales" in invoice and len(invoice) > len(plain)
    lines = printing._wrapped(long_footer, 42)
    assert len(lines) > 1 and all(len(line) <= 42 for line in lines)
    assert printing._wrapped(SLOGAN_AR, 42) == []
    page = printing.test_page(run(load_settings()), theme)
    assert b"Conditions generales" in page and page.endswith(b"\x1dVA\x03")
    # PDF : le rouleau s'allonge pour contenir un pied de document très long sans déborder sur une 2e page.
    docs = documents(stay, theme)
    for name in ("ticket", "pos"):
        for lang, data in docs[name].items():
            assert len(re.findall(rb"/Type\s*/Page[^s]", data)) == 1, (name, lang)


async def load_settings():
    async with SessionLocal() as db:
        return await load_hotel_settings(db)


# ---------------------------------------------------------------- alias du générateur
def test_pdf_generator_is_an_alias_of_pdf():
    from app.services import pdf_generator
    from app.services.pdf_generator import invoice_pdf, police_form_pdf
    assert pdf_generator is pdf
    assert invoice_pdf is pdf.invoice_pdf and police_form_pdf is pdf.police_form_pdf
