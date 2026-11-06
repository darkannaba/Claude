"""Pièces d'identité : analyseur MRZ (TD1 / TD2 / TD3), douchette, codes-barres, OCR simulé, stockage sécurisé,
routes authentifiées, application à la fiche client (dédoublonnage) et fiche de police."""
import asyncio
import io
import json
import re
import secrets
import shutil
import subprocess
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from reportlab.pdfgen import canvas
from sqlalchemy import select

from app.config import config
from app.database import SessionLocal
from app.models import Guest, IdScan, Room
from app.services import ocr_service, pdf
from app.services.loaders import load_reservation
from app.services.settings import load_hotel_settings, now_of, today_of

TODAY = date(2026, 11, 5)

# Spécimens officiels ICAO 9303 (parties 4, 5 et 6).
TD3_SPECIMEN = ("P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\n"
                "L898902C36UTO7408122F1204159ZE184226B<<<<<10")
TD2_SPECIMEN = ("I<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<\n"
                "D231458907UTO7408122F1204159<<<<<<<6")
TD1_SPECIMEN = ("I<UTOD231458907<<<<<<<<<<<<<<<\n"
                "7408122F1204159UTO<<<<<<<<<<<6\n"
                "ERIKSSON<<ANNA<MARIA<<<<<<<<<<")
PDFTOTEXT = shutil.which("pdftotext")


def run(coro):
    return asyncio.run(coro)


def cd(data: str) -> str:
    return ocr_service.check_digit(data)


def make_td3(surname: str, given: str, number: str, nationality: str, birth: str, sex: str, expiry: str,
             personal: str = "", state: str = "") -> str:
    """Passeport TD3 valide (sommes de contrôle calculées)."""
    line1 = (f"P<{state or nationality}{surname}<<{given.replace(' ', '<')}" + "<" * 44)[:44]
    num = (number + "<" * 9)[:9]
    pers = (personal + "<" * 14)[:14]
    pers_cd = cd(pers) if personal else "<"
    body = f"{num}{cd(num)}{nationality}{birth}{cd(birth)}{sex}{expiry}{cd(expiry)}{pers}{pers_cd}"
    composite = cd(body[0:10] + body[13:20] + body[21:43])
    return line1 + "\n" + body + composite


def make_td1(surname: str, given: str, number: str, nationality: str, birth: str, sex: str, expiry: str,
             optional1: str = "", optional2: str = "") -> str:
    """Carte d'identité TD1 valide (ex. CNI biométrique algérienne)."""
    num = (number + "<" * 9)[:9]
    line1 = f"ID{nationality}{num}{cd(num)}" + (optional1 + "<" * 15)[:15]
    opt2 = (optional2 + "<" * 11)[:11]
    line2 = f"{birth}{cd(birth)}{sex}{expiry}{cd(expiry)}{nationality}{opt2}"
    line2 += cd(line1[5:30] + line2[0:7] + line2[8:15] + line2[18:29])
    line3 = (f"{surname}<<{given.replace(' ', '<')}" + "<" * 30)[:30]
    return "\n".join((line1, line2, line3))


def unique_number(prefix: str = "") -> str:
    return (prefix + secrets.token_hex(4).upper().replace("O", "0"))[:9]


def png_bytes(size=(320, 200), color=(200, 200, 200), exif: Image.Exif | None = None, fmt="PNG") -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", size, color)
    kwargs = {"exif": exif} if exif is not None else {}
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def pdf_bytes(text: str = "Carte nationale d'identite") -> bytes:
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer)
    page.drawString(72, 750, text)
    page.save()
    return buffer.getvalue()


def json_post(client, url: str, payload: dict):
    return client.post(url, json=payload, headers={"X-CSRF-Token": client.csrf}, follow_redirects=False)


def upload(client, data: bytes, filename="piece.png", mime="image/png", **fields):
    form = {"csrf_token": client.csrf, "source": "image_upload", "side": "recto"}
    form.update({k: str(v) for k, v in fields.items()})
    return client.post("/api/id-scan/upload", data=form, files={"file": (filename, data, mime)},
                       headers={"X-CSRF-Token": client.csrf}, follow_redirects=False)


@pytest.fixture()
def fake_tesseract(monkeypatch):
    """pytesseract simulé : renvoie la MRZ fournie pour la zone MRZ, un texte de page pour la page entière."""
    import pytesseract

    state = {"mrz": TD3_SPECIMEN, "page": "REPUBLIQUE D'UTOPIE\nPASSEPORT\nDate de délivrance : 16/04/2007", "calls": 0}

    def image_to_string(image, lang=None, config="", **kwargs):
        state["calls"] += 1
        return state["mrz"] if "whitelist" in config else state["page"] + "\n" + state["mrz"]

    monkeypatch.setattr(pytesseract, "image_to_string", image_to_string)
    monkeypatch.setattr(ocr_service, "tesseract_available", lambda: True)
    return state


@pytest.fixture()
def no_tesseract(monkeypatch):
    monkeypatch.setattr(ocr_service, "tesseract_available", lambda: False)


# ======================================================================== sommes de contrôle et formats
def test_check_digit_matches_icao_examples():
    assert cd("L898902C3") == "6"
    assert cd("740812") == "2"
    assert cd("120415") == "9"
    assert cd("ZE184226B<<<<<") == "1"
    assert cd("<<<<<<") == "0"


def test_td3_passport_specimen_is_fully_valid():
    result = ocr_service.parse_scanner_input(TD3_SPECIMEN, TODAY)
    assert result.format == "td3"
    assert result.checks == {"document_number": True, "birth_date": True, "expiry_date": True,
                             "optional_data": True, "composite": True}
    assert result.checksums_valid and result.status == "extracted"
    f = result.fields
    assert (f["last_name"], f["first_name"]) == ("Eriksson", "Anna Maria")
    assert f["id_number"] == "L898902C3" and f["id_type"] == "passport"
    assert f["birth_date"] == "1974-08-12" and f["id_expiry_date"] == "2012-04-15" and f["gender"] == "F"
    assert f["nationality"] == "UTO"  # code inconnu de la table : repli sur le code
    assert result.extra["optional_data"] == "ZE184226B"
    assert ocr_service.MSG_EXPIRED in result.warnings  # expiré en 2012
    assert ocr_service.MSG_ISSUE_DATE in result.notes and f["id_issue_date"] == ""


def test_td2_specimen_is_fully_valid():
    result = ocr_service.parse_scanner_input(TD2_SPECIMEN, TODAY)
    assert result.format == "td2" and result.checksums_valid
    assert result.fields["id_number"] == "D23145890" and result.fields["id_type"] == "cni"
    assert result.fields["last_name"] == "Eriksson"


def test_td1_specimen_is_fully_valid():
    result = ocr_service.parse_scanner_input(TD1_SPECIMEN, TODAY)
    assert result.format == "td1" and result.checksums_valid
    assert set(result.checks) == {"document_number", "birth_date", "expiry_date", "composite"}
    assert result.fields["id_number"] == "D23145890" and result.fields["birth_date"] == "1974-08-12"
    assert result.fields["first_name"] == "Anna Maria"


def test_algerian_biometric_id_card_td1():
    mrz = make_td1("BENALI", "MOHAMED AMINE", "123456789", "DZA", "900115", "M", "310620",
                   optional2="10901")
    result = ocr_service.parse_scanner_input(mrz, TODAY)
    assert result.format == "td1" and result.checksums_valid, result.checks
    f = result.fields
    assert f["nationality"] == "Algérienne" and f["id_type"] == "cni" and f["gender"] == "M"
    assert (f["last_name"], f["first_name"]) == ("Benali", "Mohamed Amine")
    assert f["birth_date"] == "1990-01-15" and f["id_expiry_date"] == "2031-06-20"
    assert not result.warnings


def test_td1_long_document_number_in_optional_data():
    # Numéro de 12 caractères : « < » à la place du chiffre de contrôle, suite + chiffre dans les données optionnelles.
    number = "D23145890734"
    head, tail = number[:9], number[9:]
    line1 = f"I<UTO{head}<{tail}{cd(number)}" + "<" * 11
    line2 = "7408122F1204159UTO<<<<<<<<<<<"
    line2 += cd(line1[5:30] + line2[0:7] + line2[8:15] + line2[18:29])
    line3 = "ERIKSSON<<ANNA<MARIA<<<<<<<<<<"
    result = ocr_service.parse_scanner_input("\n".join((line1, line2, line3)), TODAY)
    assert result.fields["id_number"] == number and result.checksums_valid, result.checks


def test_nationality_table():
    assert ocr_service.nationality_label("DZA") == "Algérienne"
    assert ocr_service.nationality_label("FRA") == "Française"
    assert ocr_service.nationality_label("TUN") == "Tunisienne"
    assert ocr_service.nationality_label("MAR") == "Marocaine"
    assert ocr_service.nationality_label("D<<") == "Allemande"
    assert ocr_service.nationality_label("XYZ") == "XYZ"


# ======================================================================== erreurs et corrections de lecture
def test_invalid_check_digits_are_reported():
    lines = TD3_SPECIMEN.split("\n")
    broken = lines[1][:19] + "7" + lines[1][20:]  # chiffre de contrôle de la naissance faux
    result = ocr_service.parse_scanner_input(lines[0] + "\n" + broken, TODAY)
    assert result.format == "td3"
    assert result.checks["birth_date"] is False and result.checks["composite"] is False
    assert result.checks["document_number"] is True
    assert result.status == "partial" and not result.checksums_valid
    assert ocr_service.CHECK_WARNINGS["birth_date"] in result.warnings
    assert result.fields["birth_date"] == "1974-08-12"  # la date reste proposée, à vérifier


def test_ocr_misreads_are_corrected_by_position():
    lines = TD3_SPECIMEN.split("\n")
    # « « » pour « < », espaces parasites, O/0 et I/1 confondus dans les zones numériques, 0 dans un nom.
    line1 = lines[0].replace("<<ANNA", "««ANNA").replace("ERIKSSON", "ERIKSS0N")
    line2 = lines[1].replace("7408122", "74O8I22").replace("1204159", "12O4l59")
    noisy = " ".join(line1[i:i + 11] for i in range(0, 44, 11)) + "\r\n" + line2.lower()
    result = ocr_service.parse_scanner_input(noisy, TODAY)
    assert result.checksums_valid, (result.mrz, result.checks)
    assert result.fields["last_name"] == "Eriksson" and result.fields["birth_date"] == "1974-08-12"


def test_document_number_letter_digit_confusion_is_repaired():
    lines = TD3_SPECIMEN.split("\n")
    line2 = lines[1].replace("L898902C3", "L8989O2C3")  # O lu à la place de 0 dans le numéro (zone alphanumérique)
    result = ocr_service.parse_scanner_input(lines[0] + "\n" + line2, TODAY)
    assert result.fields["id_number"] == "L898902C3" and result.checksums_valid
    assert ocr_service.MSG_DOC_CORRECTED in result.warnings


def test_scanner_single_continuous_line():
    td3 = TD3_SPECIMEN.replace("\n", "")
    assert len(td3) == 88
    result = ocr_service.parse_scanner_input(td3 + "\r", TODAY)
    assert result.format == "td3" and result.checksums_valid
    td1 = TD1_SPECIMEN.replace("\n", "")
    assert len(td1) == 90
    result = ocr_service.parse_scanner_input(td1, TODAY)
    assert result.format == "td1" and result.checksums_valid
    td2 = TD2_SPECIMEN.replace("\n", "")
    result = ocr_service.parse_scanner_input(td2, TODAY)
    assert result.format == "td2" and result.checksums_valid


def test_ocr_noise_lines_around_mrz():
    text = "REPUBLIQUE D'UTOPIE\nPASSEPORT / PASSPORT\nNom: ERIKSSON\n" + TD3_SPECIMEN + "\n  \n"
    result = ocr_service.parse_scanner_input(text, TODAY)
    assert result.format == "td3" and result.checksums_valid


def test_names_with_filler_misread_as_k():
    lines = TD3_SPECIMEN.split("\n")
    line1 = lines[0][:25] + "KKKKKKKK" + lines[0][33:]
    result = ocr_service.parse_scanner_input(line1 + "\n" + lines[1], TODAY)
    assert result.fields["first_name"] == "Anna Maria"


def test_century_of_dates():
    assert ocr_service.mrz_date("740812", "birth", TODAY) == date(1974, 8, 12)
    assert ocr_service.mrz_date("200101", "birth", TODAY) == date(2020, 1, 1)
    assert ocr_service.mrz_date("261231", "birth", TODAY) == date(1926, 12, 31)  # pas de naissance future
    assert ocr_service.mrz_date("300101", "birth", TODAY) == date(1930, 1, 1)
    assert ocr_service.mrz_date("300101", "expiry", TODAY) == date(2030, 1, 1)
    assert ocr_service.mrz_date("120415", "expiry", TODAY) == date(2012, 4, 15)
    assert ocr_service.mrz_date("991231", "expiry", TODAY) == date(1999, 12, 31)
    assert ocr_service.mrz_date("991399", "birth", TODAY) is None
    assert ocr_service.mrz_date("<<<<<<", "birth", TODAY) is None


def test_document_type_mapping():
    assert ocr_service.document_type("P<") == "passport"
    assert ocr_service.document_type("PM") == "passport"
    assert ocr_service.document_type("ID") == "cni"
    assert ocr_service.document_type("I<") == "cni"
    assert ocr_service.document_type("AC") == "cni"
    assert ocr_service.document_type("C<") == "cni"
    assert ocr_service.document_type("V<") == "autre"


def test_gender_unspecified():
    mrz = make_td3("DOE", "ALEX", "X12345678", "FRA", "850101", "<", "300101")
    result = ocr_service.parse_scanner_input(mrz, TODAY)
    assert result.checksums_valid and result.fields["gender"] == "" and result.fields["nationality"] == "Française"


# ======================================================================== codes-barres structurés (douchette)
def test_aamva_driver_license_pdf417():
    raw = ("@\n\x1e\rANSI 636014040002DL00410278ZC03190008DLDAQD1234562\nDCSSMITH\nDACJOHN\nDBB01151985\n"
           "DBA06302030\nDBC1\nDCGUSA\nDAIANYTOWN\n")
    result = ocr_service.parse_scanner_input(raw, TODAY)
    assert result.format == "aamva" and result.status == "extracted"
    f = result.fields
    assert (f["last_name"], f["first_name"], f["id_number"]) == ("SMITH", "JOHN", "D1234562")
    assert f["birth_date"] == "1985-01-15" and f["id_expiry_date"] == "2030-06-30"
    assert f["gender"] == "M" and f["id_type"] == "permis" and f["nationality"] == "Américaine"


def test_qr_json_and_key_value():
    data = json.dumps({"nom": "Haddad", "prenom": "Lina", "date_naissance": "04/05/1992", "sexe": "F",
                       "nationalite": "DZA", "numero": "109876543", "type": "cni", "date_expiration": "2030-01-31"})
    result = ocr_service.parse_scanner_input(data, TODAY)
    assert result.format == "json" and result.status == "extracted"
    assert result.fields["birth_date"] == "1992-05-04" and result.fields["nationality"] == "Algérienne"
    assert result.fields["id_type"] == "cni" and result.fields["id_expiry_date"] == "2030-01-31"
    text = "NOM: Mansouri\nPRÉNOM: Karim\nN°: 220011334\nDate de naissance: 12.03.1980\nDate d'expiration: 01/01/2020"
    result = ocr_service.parse_scanner_input(text, TODAY)
    assert result.format == "cles" and result.fields["id_number"] == "220011334"
    assert result.fields["first_name"] == "Karim" and ocr_service.MSG_EXPIRED in result.warnings


def test_unrecognized_scanner_input_gives_clear_error():
    for raw in ("RES-2026-00042", "https://example.com/abc", "", "12345;ABC|x"):
        result = ocr_service.parse_scanner_input(raw, TODAY)
        assert result.status == "failed" and result.message in (ocr_service.MSG_UNRECOGNIZED, ocr_service.MSG_NO_INPUT)


def test_text_heuristics_without_mrz():
    text = ("RÉPUBLIQUE ALGÉRIENNE DÉMOCRATIQUE ET POPULAIRE\nCARTE NATIONALE D'IDENTITÉ\nNom : BENALI\n"
            "Prénom : SAMIR\nDate de naissance : 03/02/1988\nN° 109876543\nDate d'expiration : 15/09/2032\n"
            "Date de délivrance : 15/09/2022")
    result = ocr_service.extract_from_text(text, TODAY)
    f = result.fields
    assert f["birth_date"] == "1988-02-03" and f["id_expiry_date"] == "2032-09-15" and f["id_issue_date"] == "2022-09-15"
    assert f["id_number"] == "109876543" and f["id_type"] == "cni" and f["nationality"] == "Algérienne"
    assert f["last_name"] == "Benali" and result.status == "partial"


# ======================================================================== OCR d'image (Tesseract simulé)
def test_ocr_image_with_mocked_tesseract(fake_tesseract):
    result = ocr_service.ocr_image(Image.new("RGB", (900, 600), "white"), TODAY)
    assert result.engine == "tesseract" and result.format == "td3" and result.checksums_valid
    assert result.fields["id_number"] == "L898902C3"
    assert result.fields["id_issue_date"] == "2007-04-16"  # complété par le texte de la page
    assert fake_tesseract["calls"] >= 2  # zone MRZ puis page entière


def test_preprocess_grayscale_resize_and_exif_rotation():
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation : rotation de 90°
    image = Image.open(io.BytesIO(png_bytes((400, 200), exif=exif, fmt="JPEG")))
    prepared = ocr_service.preprocess(image)
    assert prepared.mode == "L"
    assert prepared.size[0] == 1600 and prepared.size[1] > prepared.size[0]  # redressée (portrait) puis agrandie


def test_tesseract_absent_is_detected(monkeypatch):
    import pytesseract

    def missing():
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "get_tesseract_version", missing)
    status = ocr_service.tesseract_status(force=True)
    assert status["ok"] is False and status["error"]
    result = ocr_service.ocr_image(Image.new("RGB", (100, 100)), TODAY)
    assert result.engine == "none" and result.message == ocr_service.MSG_NO_TESSERACT
    ocr_service._probe["at"] = 0.0  # pas de cache pour les tests suivants


# ======================================================================== validation et stockage des fichiers
def test_exif_and_gps_are_removed():
    exif = Image.Exif()
    exif[0x010F] = "FabricantSecret"
    exif[0x8825] = {1: "N", 2: (36.0, 54.0, 0.0)}  # GPSInfo : latitude d'Annaba
    data = png_bytes((640, 400), exif=exif, fmt="JPEG")
    assert Image.open(io.BytesIO(data)).getexif()
    prepared = ocr_service.prepare_file(data)
    assert prepared.mime_type == "image/jpeg" and prepared.content.startswith(b"\xff\xd8\xff")
    cleaned = Image.open(io.BytesIO(prepared.content))
    assert not cleaned.getexif() and b"Exif" not in prepared.content and b"FabricantSecret" not in prepared.content


def test_rejects_non_images_and_oversized_files(monkeypatch):
    from app.services.common import BusinessError

    for data in (b"<?php echo 1; ?>", b"GIF89a" + b"\0" * 50, b"MZ\x90\x00binaire", b""):
        with pytest.raises(BusinessError):
            ocr_service.prepare_file(data)
    with pytest.raises(BusinessError):  # en-tête PNG suivi de déchets
        ocr_service.prepare_file(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    monkeypatch.setattr(config, "id_scan_max_bytes", 2000)
    with pytest.raises(BusinessError, match="volumineux"):
        ocr_service.prepare_file(png_bytes((800, 800), color=(10, 20, 30)) + b"\0" * 3000)


def test_pdf_validation():
    from app.services.common import BusinessError

    prepared = ocr_service.prepare_file(pdf_bytes())
    assert prepared.is_pdf and prepared.mime_type == "application/pdf" and prepared.pdf_readable
    image = ocr_service.render_pdf_first_page(prepared.content)
    assert image is not None and image.size[0] > 500
    with pytest.raises(BusinessError, match="actif"):
        ocr_service.prepare_file(pdf_bytes().replace(b"/Type /Catalog", b"/Type /Catalog /OpenAction << /S /JavaScript >>"))
    with pytest.raises(BusinessError):
        ocr_service.prepare_file(b"%PDF-1.4\n garbage without end")


def test_resolve_scan_rejects_traversal():
    assert ocr_service.resolve_scan("../../etc/passwd") is None
    assert ocr_service.resolve_scan("id_scans/1/../../hotel.db") is None
    assert ocr_service.resolve_scan("branding/logo.png") is None
    assert ocr_service.resolve_scan("id_scans/1/" + "0" * 32 + ".jpg") is None  # absent du disque


def test_restore_scan_files_ignores_foreign_paths(tmp_path):
    archive_path = tmp_path / "save.zip"
    good = "data/media/id_scans/7/" + "a" * 32 + ".png"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr(good, png_bytes())
        zf.writestr("data/media/id_scans/../../evil.py", "print(1)")
        zf.writestr("data/media/id_scans/7/notes.txt", "x")
    with zipfile.ZipFile(archive_path) as zf:
        restored = ocr_service.restore_scan_files(zf, "data/media/")
    assert restored == ["id_scans/7/" + "a" * 32 + ".png"]
    assert ocr_service.resolve_scan(restored[0]) is not None
    assert not (config.data_dir / "evil.py").exists()


# ======================================================================== routes : sécurité
def test_scan_file_route_requires_staff(app, reception, housekeeper, anonymous, no_tesseract):
    response = upload(reception, png_bytes())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] and body["stored"] and body["result"]["engine"] == "none"
    assert "Tesseract" in body["result"]["message"]  # OCR absent : message clair, image conservée
    url = body["file_url"]
    ok = reception.get(url)
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/png"
    assert ok.headers["cache-control"].startswith("no-store") and ok.headers["x-content-type-options"] == "nosniff"
    assert Image.open(io.BytesIO(ok.content)).size == (320, 200)
    denied = anonymous.get(url, follow_redirects=False)
    assert denied.status_code == 303 and denied.headers["location"].startswith("/login")
    assert housekeeper.get(url, follow_redirects=False).status_code == 403
    assert anonymous.get(f"/static/../data/media/{url}").status_code == 404


def test_tesseract_absent_keeps_image_and_logs_scan(app, reception, no_tesseract):
    response = upload(reception, png_bytes(color=(1, 2, 3)), source="webcam")
    body = response.json()

    async def check():
        async with SessionLocal() as db:
            scan = await db.get(IdScan, body["scan_id"])
            return scan.source, scan.engine, scan.status, scan.file_path, scan.mime_type

    source, engine, status, path, mime = run(check())
    assert (source, engine, status, mime) == ("webcam", "none", "failed", "image/png")
    assert path.startswith("id_scans/_attente/") and ocr_service.resolve_scan(path) is not None
    status_json = reception.get("/api/id-scan/status").json()
    assert status_json["tesseract"] is False and status_json["message"] and status_json["pdf"] is True


def test_upload_rejects_bad_files(app, reception):
    bad = upload(reception, b"not an image at all", filename="x.png")
    assert bad.status_code == 422 and bad.json()["ok"] is False and "PNG" in bad.json()["message"]


def test_csrf_and_roles(app, reception, housekeeper):
    no_token = reception.post("/api/id-scan/mrz", json={"text": TD3_SPECIMEN}, follow_redirects=False)
    assert no_token.status_code == 403
    wrong = reception.post("/api/id-scan/mrz", json={"text": TD3_SPECIMEN}, headers={"X-CSRF-Token": "faux"})
    assert wrong.status_code == 403
    assert json_post(housekeeper, "/api/id-scan/mrz", {"text": TD3_SPECIMEN}).status_code == 403
    assert json_post(housekeeper, "/api/id-scan/apply", {"fields": {}}).status_code == 403
    no_file_token = reception.post("/api/id-scan/upload", files={"file": ("a.png", png_bytes(), "image/png")},
                                   follow_redirects=False)
    assert no_file_token.status_code == 403


def test_mrz_route_returns_fields_and_logs(app, reception):
    response = json_post(reception, "/api/id-scan/mrz", {"text": TD3_SPECIMEN.replace("\n", "")})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] and body["result"]["fields"]["id_number"] == "L898902C3"
    assert body["result"]["checksums_valid"] is True and body["result"]["check_labels"]["composite"]
    failed = json_post(reception, "/api/id-scan/mrz", {"text": "RES-0001"})
    assert failed.status_code == 422 and "MRZ" in failed.json()["message"]

    async def logged():
        async with SessionLocal() as db:
            scan = await db.get(IdScan, body["scan_id"])
            return scan.raw_mrz, scan.checksums_valid, json.loads(scan.fields_json)["fields"]["last_name"]

    raw_mrz, valid, name = run(logged())
    assert raw_mrz.split("\n")[0].startswith("P<UTOERIKSSON") and valid and name == "Eriksson"


def test_mrz_route_translates_messages(app, reception):
    assert reception.form("/lang", {"lang": "en", "next": "/"}).status_code in (302, 303)
    body = json_post(reception, "/api/id-scan/mrz", {"text": TD3_SPECIMEN}).json()
    assert body["result"]["message"] == "MRZ read: all check digits are valid."
    assert "Expired document: check the validity of the ID document." in body["result"]["warnings"]
    reception.form("/lang", {"lang": "fr", "next": "/"})


# ======================================================================== application à la fiche client
def _fields(mrz: str) -> dict:
    return ocr_service.parse_scanner_input(mrz, TODAY).to_dict()["fields"]


async def _guest(guest_id: int) -> Guest:
    async with SessionLocal() as db:
        return await db.get(Guest, guest_id)


def test_apply_creates_then_deduplicates(app, reception, fake_tesseract):
    number = unique_number("A")
    mrz = make_td3("HADDAD", "YACINE", number, "DZA", "880203", "M", "330101")
    fake_tesseract["mrz"] = mrz
    first = upload(reception, png_bytes(color=(5, 5, 5))).json()
    assert first["result"]["fields"]["id_number"] == number
    fields = dict(first["result"]["fields"], id_issue_date="2023-01-02", id_issue_place="Annaba")
    created = json_post(reception, "/api/id-scan/apply", {"fields": fields, "scan_ids": [first["scan_id"]]}).json()
    assert created["ok"] and created["created"] is True
    guest_id = created["guest"]["id"]
    guest = run(_guest(guest_id))
    assert guest.nationality == "Algérienne" and guest.id_type == "passport" and guest.id_expiry_date == date(2033, 1, 1)
    assert guest.id_issue_place == "Annaba" and guest.id_scanned_at is not None
    assert guest.id_scan_path.startswith(f"id_scans/{guest_id}/")
    first_file = ocr_service.resolve_scan(guest.id_scan_path)
    assert first_file is not None

    # Deuxième numérisation de la même pièce : même fiche, scan remplacé, ancien fichier supprimé.
    second = json_post(reception, "/api/id-scan/mrz", {"text": mrz}).json()
    again = upload(reception, png_bytes(color=(9, 9, 9))).json()
    fields = dict(second["result"]["fields"], first_name="Yacine Amine")
    updated = json_post(reception, "/api/id-scan/apply",
                        {"fields": fields, "scan_ids": [second["scan_id"], again["scan_id"]]}).json()
    assert updated["ok"] and updated["created"] is False and updated["guest"]["id"] == guest_id
    guest = run(_guest(guest_id))
    assert guest.first_name == "Yacine Amine" and guest.id_issue_place == "Annaba"  # champs absents conservés
    assert not first_file.exists() and ocr_service.resolve_scan(guest.id_scan_path) is not None

    # Même numéro saisi sous un autre type : toujours la même fiche.
    other_type = json_post(reception, "/api/id-scan/apply", {"fields": dict(fields, id_type="cni")}).json()
    assert other_type["guest"]["id"] == guest_id

    async def count():
        async with SessionLocal() as db:
            return len((await db.scalars(select(Guest).where(Guest.id_number_norm == number))).all())

    assert run(count()) == 1


def test_apply_validates_fields(app, reception):
    missing = json_post(reception, "/api/id-scan/apply", {"fields": {"last_name": "X", "first_name": "Y"}})
    assert missing.status_code == 422 and "requis" in missing.json()["message"]
    bad_type = json_post(reception, "/api/id-scan/apply", {"fields": {"last_name": "X", "first_name": "Y",
                                                                      "id_number": "123456", "id_type": "visa"}})
    assert bad_type.status_code == 422


def test_guest_page_shows_scan_thumbnail(app, reception, fake_tesseract):
    number = unique_number("G")
    fake_tesseract["mrz"] = make_td1("ZERROUKI", "NADIA", number, "DZA", "950707", "F", "300707")
    scan = upload(reception, png_bytes(color=(40, 40, 40))).json()
    applied = json_post(reception, "/api/id-scan/apply",
                        {"fields": scan["result"]["fields"], "scan_ids": [scan["scan_id"]]}).json()
    guest_id = applied["guest"]["id"]
    page = reception.get(f"/guests/{guest_id}")
    assert page.status_code == 200
    assert f"/guests/{guest_id}/id-scan/recto" in page.text and "Pièce numérisée le" in page.text
    assert "Numériser la pièce d" in page.text and 'id="id-scan-config"' in page.text
    image = reception.get(f"/guests/{guest_id}/id-scan/recto")
    assert image.status_code == 200 and image.headers["cache-control"].startswith("no-store")
    assert reception.get(f"/guests/{guest_id}/id-scan/verso").status_code == 404
    # Suppression du scan (fichier effacé du disque).
    path = ocr_service.resolve_scan(run(_guest(guest_id)).id_scan_path)
    assert reception.form(f"/guests/{guest_id}/id-scan/recto/delete").status_code == 303
    assert not path.exists() and run(_guest(guest_id)).id_scan_path == ""


def test_reservation_pages_offer_scanner(app, reception):
    page = reception.get("/reservations/new")
    assert page.status_code == 200
    assert "Numériser la pièce d" in page.text and "id-scan-applied" in page.text
    config_json = re.search(r'<script type="application/json" id="id-scan-config">(.*?)</script>', page.text, re.S)
    data = json.loads(config_json.group(1))
    assert data["context"] == "new" and data["reservation_id"] is None and "passport" in data["id_types"]


async def _free_room() -> int:
    async with SessionLocal() as db:
        return await db.scalar(select(Room.id).where(Room.active.is_(True)).order_by(Room.number.desc()))


async def _today() -> date:
    async with SessionLocal() as db:
        return today_of(await load_hotel_settings(db))


def _pdf_text(data: bytes) -> str:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "doc.pdf"
        path.write_bytes(data)
        return subprocess.run([PDFTOTEXT, "-layout", str(path), "-"], capture_output=True, text=True, check=True).stdout


def test_checkin_scan_updates_reservation_guest_and_police_form(app, reception, fake_tesseract):
    today = run(_today())
    start = today + timedelta(days=500 + secrets.randbelow(200))
    created = reception.form("/reservations", {
        "check_in": start.isoformat(), "check_out": (start + timedelta(days=2)).isoformat(),
        "room_id": str(run(_free_room())), "g_last_name": "Temporaire", "g_first_name": "Client",
        "g_id_type": "cni", "g_id_number": "", "adults": "1", "children": "0", "channel": "Direct",
    })
    assert created.status_code == 303, created.text
    res_id = int(re.search(r"/reservations/(\d+)$", created.headers["location"]).group(1))
    assert "Numériser la pièce d" in reception.get(f"/reservations/{res_id}").text

    number = unique_number("C")
    fake_tesseract["mrz"] = make_td3("BOUZID", "KARIM", number, "TUN", "791224", "M", "340315", state="TUN")
    pdf_scan = upload(reception, pdf_bytes(), filename="piece.pdf", mime="application/pdf", reservation_id=res_id).json()
    assert pdf_scan["ok"] and pdf_scan["mime_type"] == "application/pdf"
    assert pdf_scan["result"]["fields"]["id_number"] == number  # PDF rendu (pypdfium2) puis OCR simulé
    applied = json_post(reception, "/api/id-scan/apply", {
        "fields": dict(pdf_scan["result"]["fields"], id_issue_date="2024-03-16"),
        "scan_ids": [pdf_scan["scan_id"]], "reservation_id": res_id,
    }).json()
    assert applied["ok"] and applied["police_url"] == f"/reservations/{res_id}/police.pdf"

    async def documents():
        async with SessionLocal() as db:
            hs = await load_hotel_settings(db)
            res = await load_reservation(db, res_id)
            return res.guest, pdf.police_form_pdf(hs, res, now_of(hs), lang="fr"), \
                pdf.police_form_pdf(hs, res, now_of(hs), lang="bi")

    guest, police_fr, police_bi = run(documents())
    assert guest.last_name == "Bouzid" and guest.nationality == "Tunisienne" and guest.id_number == number
    assert guest.id_expiry_date == date(2034, 3, 15) and guest.id_scan_path.endswith(".pdf")
    assert police_fr.startswith(b"%PDF") and police_bi.startswith(b"%PDF")
    assert reception.get(applied["police_url"]).status_code == 200
    detail = reception.get(f"/reservations/{res_id}").text
    assert "15/03/2034" in detail and "Pièce numérisée le" in detail
    if PDFTOTEXT:
        text = _pdf_text(police_fr)
        assert number in text and "15/03/2034" in text and "Pièce numérisée le" in text
        assert "Date d'expiration" in text and "16/03/2024" in text
        assert number in _pdf_text(police_bi)


def test_backup_archive_contains_scans(app, reception, no_tesseract):
    from app.services import backup_manager

    body = upload(reception, png_bytes(color=(77, 1, 1))).json()

    async def path():
        async with SessionLocal() as db:
            return (await db.get(IdScan, body["scan_id"])).file_path

    relative = run(path())
    archive = backup_manager.build_archive("Test", datetime.now(), "manuelle")
    with zipfile.ZipFile(archive.path) as zf:
        assert backup_manager.MEDIA_PREFIX + relative in zf.namelist()


def test_tesseract_found_in_standard_windows_folder(monkeypatch, tmp_path):
    """Installé avec l'installeur Windows sans toucher au PATH ni au .env : trouvé quand même."""
    from app.config import config as app_config
    from app.services import ocr_service

    exe = tmp_path / "Tesseract-OCR" / "tesseract.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(app_config, "tesseract_cmd", "")
    monkeypatch.setattr(ocr_service.shutil, "which", lambda _name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    assert ocr_service.find_tesseract_cmd() == str(exe)
    monkeypatch.setattr(app_config, "tesseract_cmd", r"D:\OCR\tesseract.exe")
    assert ocr_service.find_tesseract_cmd() == r"D:\OCR\tesseract.exe"


# ---------------------------------------------------------------- photo réelle : hologramme, reflets
def _td3_line2(number, nationality, birth, sex, expiry, optional):
    from app.services.ocr_service import check_digit

    line = number + check_digit(number) + nationality + birth + check_digit(birth) + sex + expiry + check_digit(expiry)
    opt = (optional + "<" * 14)[:14]
    line += opt + check_digit(opt)
    return line + check_digit(line[0:10] + line[13:20] + line[21:43])


_L1 = ("P<DZABENALI<<KARIM" + "<" * 44)[:44]
_L2 = _td3_line2("123456789", "DZA", "900101", "M", "300505", "X1234")


@pytest.mark.parametrize("line2", [_L2[2:], _L2[1:], "<1" + _L2, "Z" + _L2])
def test_shifted_data_line_is_realigned(line2):
    """Caractères perdus ou ajoutés en début de 2e ligne (bord de photo, hologramme) : dates retrouvées."""
    from app.services.ocr_service import parse_mrz

    result = parse_mrz(f"{_L1}\n{line2}", date(2026, 11, 6))
    assert result.fields["birth_date"] == "1990-01-01"
    assert result.fields["id_expiry_date"] == "2030-05-05"
    assert result.fields["gender"] == "M" and result.fields["nationality"] == "Algérienne"


def test_filler_misread_as_letters_is_removed_from_given_names():
    from app.services.ocr_service import parse_mrz

    noisy = ("P<DZABENALI<<KARIM<S<SS" + "<" * 44)[:44]
    result = parse_mrz(f"{noisy}\n{_L2}", date(2026, 11, 6))
    assert (result.fields["last_name"], result.fields["first_name"]) == ("Benali", "Karim")
    two_names = ("P<DZABENALI<<KARIM<AMINE<<K<<" + "<" * 44)[:44]
    assert parse_mrz(f"{two_names}\n{_L2}", date(2026, 11, 6)).fields["first_name"] == "Karim Amine"


def test_number_recovered_from_printed_number_on_page():
    from app.services.ocr_service import _recover_number_from_page, parse_mrz

    result = parse_mrz(f"{_L1}\n{_L2[2:]}", date(2026, 11, 6))
    assert not result.checks["document_number"]
    _recover_number_from_page(result, "PASSEPORT  Type P  Code DZA  N° 123456789\nNom BENALI")
    assert result.fields["id_number"] == "123456789" and result.checks["document_number"]
    # Un autre nombre de la page (n° personnel) ne remplace jamais le numéro : il ne valide pas la somme.
    other = parse_mrz(f"{_L1}\n{_L2[2:]}", date(2026, 11, 6))
    _recover_number_from_page(other, "N° personnel 109890841056800005")
    assert not other.checks["document_number"]


def test_number_repair_never_slips_a_letter_into_a_numeric_number():
    """« 187128777 » ne doit pas devenir « 9I28777 » parce qu'un remplacement chiffre → lettre valide par hasard."""
    import itertools

    from app.services.ocr_service import _AMBIGUOUS, _repair_number, check_digit, check_ok

    tested = 0
    for digits in itertools.islice(("".join(p) for p in itertools.product("1258", repeat=9)), 4000):
        wrong = str((int(check_digit(digits)) + 3) % 10)
        letter_fix = any(check_ok(digits[:i] + r + digits[i + 1:], wrong)
                         for i, ch in enumerate(digits) for r in _AMBIGUOUS.get(ch, "") if r.isalpha())
        if not letter_fix:
            continue
        line2 = (digits + wrong + "DZA9001011M3005051" + "<" * 44)[:44]
        repaired = _repair_number([_L1, line2], "td3")
        assert repaired is None or repaired[1][:9].isdigit()
        tested += 1
    assert tested > 10


def test_guessed_number_replaced_by_printed_number():
    from app.services.ocr_service import _recover_number_from_page, parse_mrz

    result = parse_mrz(f"{_L1}\n{_L2[2:]}", date(2026, 11, 6))
    result.extra["number_repaired"] = True
    result.checks["document_number"] = True
    _recover_number_from_page(result, "N° 123456789")
    assert result.fields["id_number"] == "123456789"
    assert not result.extra["number_repaired"]
