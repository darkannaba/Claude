"""Documents PDF et tickets trilingues : fr / ar / en / bi (fiche de police, facture A4 et ticket, addition, clôture)."""
import asyncio
import re
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import PosItem
from app.services import pdf, printing, reports
from app.services.amount_words_en import amount_in_words_en, number_to_words_en
from app.services.i18n import catalog, t
from app.services.invoicing import invoice_view
from app.services.loaders import load_pos_order, load_reservation
from app.services.pricing import load_calendar
from app.services.settings import load_hotel_settings, now_of, today_of

PDFTOTEXT = shutil.which("pdftotext")
LANGS = ("fr", "ar", "en", "bi")


def run(coro):
    return asyncio.run(coro)


def _pages(data: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", data))


def _text(data: bytes) -> str:
    """Texte extrait par pdftotext (poppler) ; chaîne vide si l'outil est absent."""
    if not PDFTOTEXT:
        return ""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "doc.pdf"
        path.write_bytes(data)
        return subprocess.run([PDFTOTEXT, "-layout", str(path), "-"], capture_output=True, text=True, check=True).stdout


@pytest.fixture(scope="module")
def stay(app):
    """Séjour complet créé via HTTP (accompagnateurs, extra, versement) et addition POS imputée sur la chambre."""
    from tests.conftest import make_client
    admin = make_client(app, "admin")
    reception = make_client(app, "reception")
    today = run(_today())
    room_id = run(_room_id("204"))
    assert admin.form("/admin/modules", {"m_restaurant": "1", "m_parking": "1", "m_salle_reunion": "1"}).status_code == 303
    created = reception.form("/reservations", {
        "check_in": today.isoformat(), "check_out": (today + timedelta(days=2)).isoformat(), "room_id": str(room_id),
        "g_last_name": "Haddad", "g_first_name": "Yacine", "g_last_name_ar": "حداد", "g_first_name_ar": "ياسين",
        "g_phone": "0661000099", "g_id_type": "passport", "g_id_number": "I18N-PDF-001", "g_nationality": "Algérienne",
        "adults": "2", "children": "1", "channel": "Direct", "coming_from": "Constantine", "going_to": "Alger",
        "deposit": "4000", "deposit_method": "especes",
    })
    assert created.status_code == 303, created.text
    res_id = int(re.search(r"/reservations/(\d+)$", created.headers["location"]).group(1))
    url = f"/reservations/{res_id}"
    assert reception.form(f"{url}/companions", {"last_name": "Haddad", "first_name": "Lina", "first_name_ar": "لينا",
                                                "relationship_label": "Fille", "id_type": "cni",
                                                "id_number": "I18N-COMP-1", "birth_date": "2012-05-04"}).status_code == 303
    assert reception.form(f"{url}/checkin").status_code == 303
    assert reception.form(f"{url}/extras", {"description": "Blanchisserie", "amount": "1500", "vat": "services"}).status_code == 303
    assert reception.form(f"{url}/payments", {"amount": "2000", "kind": "paiement", "method": "cib",
                                              "reference": "TPE-42"}).status_code == 303
    assert admin.form("/pos/catalog", {"outlet": "restaurant", "name": "Thé à la menthe", "category": "Boissons",
                                        "price": "150", "vat_rate": "9", "active": "1"}).status_code == 303
    item_id = run(_item_id("Thé à la menthe"))
    order = reception.form("/pos/orders", {"outlet": "restaurant", "label": "Table 7"})
    order_url = order.headers["location"]
    assert reception.form(f"{order_url}/lines", {"item_id": str(item_id), "quantity": "2"}).status_code == 303
    assert reception.form(f"{order_url}/charge", {"reservation_id": str(res_id)}).status_code == 303
    order_id = int(order_url.rstrip("/").rsplit("/", 1)[1])
    return {"reception": reception, "res_id": res_id, "url": url, "order_id": order_id, "order_url": order_url,
            "today": today}


async def _today():
    async with SessionLocal() as db:
        return today_of(await load_hotel_settings(db))


async def _room_id(number: str) -> int:
    from app.models import Room
    async with SessionLocal() as db:
        return await db.scalar(select(Room.id).where(Room.number == number))


async def _item_id(name: str) -> int:
    async with SessionLocal() as db:
        return await db.scalar(select(PosItem.id).where(PosItem.name == name))


async def _documents(res_id: int, order_id: int, day) -> dict[str, dict[str, bytes]]:
    """Génère chaque document dans les quatre langues : {document: {lang: octets}}."""
    async with SessionLocal() as db:
        hs = await load_hotel_settings(db)
        res = await load_reservation(db, res_id)
        order = await load_pos_order(db, order_id)
        view = invoice_view(res, hs, await load_calendar(db, hs), now_of(hs))
        summary = await reports.daily_summary(db, day)
        now = now_of(hs)
        out: dict[str, dict[str, bytes]] = {"police": {}, "invoice": {}, "ticket": {}, "pos": {}, "report": {}}
        for lang in LANGS:
            out["police"][lang] = pdf.police_form_pdf(hs, res, now, lang=lang)
            out["invoice"][lang] = pdf.invoice_pdf(hs, view, "a4", lang=lang)
            out["ticket"][lang] = pdf.invoice_pdf(hs, view, "ticket", lang=lang)
            out["pos"][lang] = pdf.pos_ticket_pdf(hs, order, lang=lang)
            out["report"][lang] = pdf.daily_report_pdf(hs, summary, lang=lang)
        # Signatures historiques (langue positionnelle absente = bilingue) et référence du mode bilingue.
        out["police"]["default"] = pdf.police_form_pdf(hs, res, now)
        out["invoice"]["default"] = pdf.invoice_pdf(hs, view)
        out["pos"]["default"] = pdf.pos_ticket_pdf(hs, order)
        out["report"]["default"] = pdf.daily_report_pdf(hs, summary)
        return out


@pytest.fixture(scope="module")
def documents(stay):
    return run(_documents(stay["res_id"], stay["order_id"], stay["today"] - timedelta(days=1)))


# ---------------------------------------------------------------- génération dans les quatre langues
def test_every_document_is_a_pdf_in_every_language(documents):
    for name, versions in documents.items():
        for lang, data in versions.items():
            assert data.startswith(b"%PDF"), (name, lang)
            assert _pages(data) >= 1, (name, lang)


def test_bilingual_mode_is_the_default_and_keeps_page_count(documents):
    # La fiche de police tient sur une page par personne munie d'une pièce (voyageur + 1 accompagnateur).
    assert _pages(documents["police"]["bi"]) == 2
    for name, versions in documents.items():
        if "default" in versions:
            assert _pages(versions["default"]) == _pages(versions["bi"]), name
        if name in ("police", "invoice", "report"):  # documents A4 : même pagination dans les quatre langues
            for lang in ("fr", "en", "ar"):
                assert _pages(versions[lang]) == _pages(versions["bi"]), (name, lang)
    assert pdf.doc_lang("en") == "en" and pdf.doc_lang("AR") == "ar" and pdf.doc_lang("bi") == "bi"
    assert pdf.doc_lang("klingon") == "bi" and pdf.doc_lang(None) == "bi" and pdf.doc_lang("", "fr") == "fr"


@pytest.mark.skipif(not PDFTOTEXT, reason="pdftotext (poppler) absent")
def test_french_and_english_texts(documents):
    police_fr, police_en = _text(documents["police"]["fr"]), _text(documents["police"]["en"])
    assert "FICHE DE POLICE" in police_fr and "Voyageur principal" in police_fr and "Signature du voyageur" in police_fr
    assert "POLICE FORM" in police_en and "Main traveller" in police_en and "Traveller's signature" in police_en
    assert "Companions (1)" in police_en and "Passport" in police_en and "Booking no." in police_en
    for french in ("FICHE DE POLICE", "Voyageur principal", "Nationalité", "Pièce d'identité", "Accompagnateur",
                   "Signature du voyageur", "Visa de la réception", "Fait à", "Séjour"):
        assert french not in police_en, french
    # Aucun libellé arabe (« اللقب » = Nom) hors du mode bilingue ; les champs saisis en arabe restent affichés
    # en mode arabe (texte mis en forme par arabic_reshaper, d'où la comparaison sur les formes de présentation).
    assert "اللقب" not in police_fr and "اللقب" not in police_en
    assert "اللقب" not in _text(documents["police"]["ar"]) and "Voyageur principal" not in _text(documents["police"]["ar"])

    invoice_fr, invoice_en = _text(documents["invoice"]["fr"]), _text(documents["invoice"]["en"])
    assert "FACTURE PROFORMA" in invoice_fr and "Désignation" in invoice_fr and "SOLDE À PAYER" in invoice_fr
    assert "Arrêtée la présente facture à la somme de" in invoice_fr and "dinars algériens" in invoice_fr
    assert "PRO FORMA INVOICE" in invoice_en and "BALANCE DUE" in invoice_en and "Description" in invoice_en
    assert "This invoice is closed at the sum of" in invoice_en and "Algerian dinars" in invoice_en
    assert "Deposit" in invoice_en and "CIB / EDAHABIA card" in invoice_en and "(ref. TPE-42)" in invoice_en
    assert "Thank you for your visit" in invoice_en
    for french in ("FACTURE", "Désignation", "SOLDE À PAYER", "Versements", "Règlements", "Arrêtée la présente",
                   "Document provisoire", "Merci de votre visite", "Taux", "Base HT", "Client", "Séjour du"):
        assert french not in invoice_en, french

    ticket_en = _text(documents["ticket"]["en"])
    assert "PRO FORMA INVOICE" in ticket_en and "Customer:" in ticket_en and "BALANCE DUE" in ticket_en
    assert "Client" not in ticket_en and "SOLDE" not in ticket_en

    pos_fr, pos_en = _text(documents["pos"]["fr"]), _text(documents["pos"]["en"])
    assert "Addition" in pos_fr and "Imputé sur la chambre" in pos_fr and "Signature du client" in pos_fr
    assert "Bill" in pos_en and "Charged to room" in pos_en and "Customer's signature" in pos_en
    assert "Addition" not in pos_en and "Imputé" not in pos_en

    report_fr, report_en = _text(documents["report"]["fr"]), _text(documents["report"]["en"])
    assert "RAPPORT DE CLÔTURE JOURNALIÈRE" in report_fr and "Encaissements" in report_fr and "Direction" in report_fr
    assert "DAILY CLOSING REPORT" in report_en and "Receipts" in report_en and "Management" in report_en
    assert "Night receptionist" in report_en and "Accommodation" in report_en and "Cash" in report_en
    for french in ("RAPPORT DE CLÔTURE", "Encaissements", "Indicateur", "Chiffre d'affaires", "Hébergement", "Espèces",
                   "Réceptionniste"):
        assert french not in report_en, french


@pytest.mark.skipif(not PDFTOTEXT, reason="pdftotext (poppler) absent")
def test_bilingual_text_keeps_french_and_arabic(documents):
    invoice_bi = _text(documents["invoice"]["bi"])
    assert "FACTURE PROFORMA" in invoice_bi and "Arrêtée la présente facture" in invoice_bi
    assert _text(documents["invoice"]["default"]) == invoice_bi  # l'ancien appel sans langue = bilingue
    police_bi = _text(documents["police"]["bi"])
    assert "FICHE DE POLICE" in police_bi and "Voyageur principal" in police_bi
    assert _text(documents["police"]["default"]) == police_bi


def test_arabic_translations_moved_to_catalog():
    ar = catalog("ar")
    for key, value in (("Signature du voyageur", "إمضاء المسافر"), ("Visa de la réception", "تأشيرة الاستقبال"),
                       ("SOLDE À PAYER", "المبلغ المتبقي"), ("TOTAL TTC", "المجموع الكلي"), ("TOTAL TTC|pos", "المجموع"),
                       ("TVA|col", "ر.ق.م"), ("TVA|pdf", "الرسم على القيمة المضافة"), ("TVA|taux", "الرسم"),
                       ("RAPPORT DE CLÔTURE JOURNALIÈRE", "تقرير الإقفال اليومي"), ("Réceptionniste de nuit", "موظف الاستقبال الليلي"),
                       ("Client|pdf", "الزبون"), ("Lien avec le voyageur", "صلة القرابة")):
        assert ar.get(key) == value, key
    assert t("FACTURE", "en") == "INVOICE" and t("FICHE DE POLICE – RENSEIGNEMENTS VOYAGEUR", "en").startswith("POLICE FORM")
    # Texte français d'une clé à suffixe de contexte : le suffixe n'est jamais affiché.
    doc = pdf._Doc("fr")
    assert doc.t("TVA|col") == "TVA" and doc.t("Client|pdf") == "Client"
    assert pdf._Doc("en").t("Clé inconnue|ctx") == "Clé inconnue"
    assert pdf._Doc("ar").t("N° {number}", number="7") == "رقم 7"


@pytest.mark.parametrize("number,words", [
    (0, "zero"), (7, "seven"), (15, "fifteen"), (21, "twenty-one"), (100, "one hundred"), (105, "one hundred and five"),
    (1000, "one thousand"), (1015, "one thousand and fifteen"), (31800, "thirty-one thousand eight hundred"),
    (2_000_000, "two million"), (1_250_000_000, "one billion two hundred and fifty million"), (-5, "minus five"),
])
def test_number_to_words_en(number, words):
    assert number_to_words_en(number) == words


def test_amount_in_words_en():
    assert amount_in_words_en(31800) == "Thirty-one thousand eight hundred Algerian dinars"


# ---------------------------------------------------------------- tickets ESC/POS
def test_escpos_receipts_french_and_english(stay):
    async def build():
        async with SessionLocal() as db:
            hs = await load_hotel_settings(db)
            res = await load_reservation(db, stay["res_id"])
            order = await load_pos_order(db, stay["order_id"])
            view = invoice_view(res, hs, await load_calendar(db, hs), now_of(hs))
            return {lang: (printing.invoice_receipt(view, hs, lang=lang), printing.pos_receipt(order, hs, lang=lang))
                    for lang in ("fr", "en", "ar", "bi", "xx")} | {"default": (printing.invoice_receipt(view, hs),
                                                                               printing.pos_receipt(order, hs))}

    receipts = run(build())
    invoice_fr, pos_fr = receipts["fr"]
    assert b"FACTURE PROFORMA" in invoice_fr and b"Client :" in invoice_fr and b"SOLDE" in invoice_fr
    assert b"TOTAL TTC" in invoice_fr and b"Acompte" in invoice_fr
    assert b"Addition" in pos_fr and b"Impute chambre" in pos_fr and b"Signature client" in pos_fr
    assert receipts["default"] == receipts["fr"] and receipts["xx"] == receipts["fr"]
    invoice_en, pos_en = receipts["en"]
    assert b"PRO FORMA INVOICE" in invoice_en and b"Customer:" in invoice_en and b"BALANCE" in invoice_en
    assert b"TOTAL INCL. TAX" in invoice_en and b"Deposit" in invoice_en and b"Room 204 - from" in invoice_en
    assert b"FACTURE" not in invoice_en and b"SOLDE" not in invoice_en and b"Client :" not in invoice_en
    assert b"Bill" in pos_en and b"Charged to room" in pos_en and b"Customer signature" in pos_en
    assert b"Addition" not in pos_en and b"Impute" not in pos_en
    # Pas de police arabe sur les imprimantes thermiques : ar et bi sont imprimés en anglais.
    assert receipts["ar"] == receipts["en"] and receipts["bi"] == receipts["en"]
    assert printing.receipt_lang("ar") == "en" and printing.receipt_lang(None) == "fr" and printing.receipt_lang("de") == "fr"


# ---------------------------------------------------------------- routes HTTP
def test_pdf_routes_accept_lang_parameter(stay):
    reception, url, order_url = stay["reception"], stay["url"], stay["order_url"]
    yesterday = (stay["today"] - timedelta(days=1)).isoformat()
    routes = (f"{url}/police.pdf", f"{url}/invoice.pdf?format=a4", f"{url}/invoice.pdf?format=ticket",
              f"{order_url}/ticket.pdf", f"/night-audit/{yesterday}/report.pdf")
    for route in routes:
        joiner = "&" if "?" in route else "?"
        for lang in ("en", "ar", "fr", "bi", "klingon"):
            response = reception.get(f"{route}{joiner}lang={lang}")
            assert response.status_code == 200, (route, lang)
            assert response.headers["content-type"].startswith("application/pdf") and response.content.startswith(b"%PDF")
    if PDFTOTEXT:
        assert "POLICE FORM" in _text(reception.get(f"{url}/police.pdf?lang=en").content)
        assert "INVOICE" in _text(reception.get(f"{url}/invoice.pdf?lang=en").content)
        assert "DAILY CLOSING REPORT" in _text(reception.get(f"/night-audit/{yesterday}/report.pdf?lang=en").content)
        # Valeur inconnue ⇒ bilingue (texte identique au défaut).
        assert _text(reception.get(f"{url}/police.pdf?lang=klingon").content) == _text(reception.get(f"{url}/police.pdf").content)
        assert "FICHE DE POLICE" in _text(reception.get(f"{url}/police.pdf?lang=klingon").content)


def test_print_routes_accept_lang_form_field(stay, monkeypatch):
    sent: list[tuple[str, bytes]] = []

    async def fake_print(host, port, data):
        sent.append((host, data))

    monkeypatch.setattr(printing, "print_raw", fake_print)
    reception, url, order_url = stay["reception"], stay["url"], stay["order_url"]
    assert reception.form(f"{url}/print/ticket", {"lang": "en"}).status_code == 303
    assert reception.form(f"{url}/print/ticket", {"lang": "klingon"}).status_code == 303
    assert reception.form(f"{url}/print/invoice", {"lang": "en"}).status_code == 303
    assert reception.form(f"{url}/print/police", {"lang": "ar"}).status_code == 303
    assert reception.form(f"{order_url}/print", {"lang": "en"}).status_code == 303
    assert reception.form(f"{order_url}/print").status_code == 303
    assert len(sent) == 6
    assert b"PRO FORMA INVOICE" in sent[0][1] and b"FACTURE PROFORMA" in sent[1][1]      # valeur inconnue ⇒ français
    assert sent[2][1].startswith(b"%PDF") and sent[3][1].startswith(b"%PDF")
    assert b"Bill" in sent[4][1] and b"Addition" in sent[5][1]
    if PDFTOTEXT:
        assert "PRO FORMA INVOICE" in _text(sent[2][1])
    assert reception.form(f"{url}/print/inconnu", {"lang": "en"}).status_code == 303
    assert len(sent) == 6
