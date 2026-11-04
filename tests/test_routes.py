"""Parcours HTTP complets et contrôle des rôles (réception, admin, housekeeping)."""
import re
from datetime import timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.models.admin import FeatureFlag
from app.models.property import Room
from app.services.settings import load_settings, save_setting, today_of
from tests.conftest import login


def _today():
    with SessionLocal() as db:
        return today_of(load_settings(db))


def test_login_required_and_bad_password(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    bad = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert bad.status_code == 401


def test_admin_pages_render(client):
    login(client, "admin")
    for path in ["/", "/planning", "/reservations", "/guests", "/housekeeping", "/admin", "/admin/rooms",
                 "/admin/rates", "/admin/modules", "/admin/services", "/admin/companies", "/admin/users",
                 "/reservations/new", "/guests/new", "/planning?days=30&floor=2"]:
        response = client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"


def test_modules_are_disabled_by_default():
    with SessionLocal() as db:
        flags = db.scalars(select(FeatureFlag)).all()
        assert {f.key for f in flags} == {"restaurant", "salle_reunion", "parking"}
        assert all(f.enabled is False for f in flags)


def test_full_stay_workflow_over_http(client):
    login(client, "reception")
    today = _today()
    with SessionLocal() as db:
        room = db.scalar(select(Room).where(Room.number == "301"))
        room_id = room.id

    created = client.post("/reservations", data={
        "last_name": "Benali", "first_name": "Karim", "phone": "0661000000", "id_type": "passport",
        "id_number": "P998877", "nationality": "Algérienne", "room_id": str(room_id),
        "check_in": (today - timedelta(days=1)).isoformat(), "check_out": (today + timedelta(days=2)).isoformat(),
        "adults": "2", "children": "0", "channel": "Direct",
    }, follow_redirects=False)
    assert created.status_code == 303, created.text
    res_url = created.headers["location"]
    res_id = int(re.search(r"/reservations/(\d+)", res_url).group(1))

    detail = client.get(res_url)
    assert detail.status_code == 200 and "Benali" in detail.text

    assert client.get(f"/reservations/{res_id}/police.pdf").headers["content-type"] == "application/pdf"
    assert client.get(f"/reservations/{res_id}/facture.pdf?format=a4").status_code == 200
    assert client.get(f"/reservations/{res_id}/facture.pdf?format=ticket").status_code == 200

    client.post(f"/reservations/{res_id}/checkin")
    assert "En séjour" in client.get(res_url).text

    client.post(f"/reservations/{res_id}/charges", data={"label": "Café", "amount": "400", "category": "autre"})
    client.post(f"/reservations/{res_id}/payments", data={"amount": "5000", "kind": "acompte", "method": "carte"})
    client.post(f"/reservations/{res_id}/checkout", data={"force": "1"})
    final = client.get(res_url).text
    assert "Clôturée" in final

    with SessionLocal() as db:
        assert db.get(Room, room_id).status == "dirty"


def test_search_scanner_and_api(client):
    login(client, "reception")
    created = client.post("/guests", data={"first_name": "Amina", "last_name": "Scan", "id_type": "cni",
                                           "id_number": "SCAN-XYZ-42"}, follow_redirects=False)
    assert created.status_code == 303
    # Un numéro de pièce exact (lecteur USB) ouvre directement la fiche.
    scanned = client.get("/guests", params={"q": "SCAN-XYZ-42"}, follow_redirects=False)
    assert scanned.status_code == 303 and scanned.headers["location"].startswith("/guests/")
    results = client.get("/api/guests/search", params={"q": "Scan"}).json()
    assert any(item["label"].startswith("Amina Scan") for item in results)


def test_housekeeping_role_restrictions(client):
    with SessionLocal() as db:
        save_setting(db, "restrict_cleaning_to_reception", "0")
        db.commit()

    login(client, "gouvernante")
    assert client.get("/reservations", follow_redirects=False).status_code == 403
    assert client.get("/admin", follow_redirects=False).status_code == 403
    board = client.get("/housekeeping")
    assert board.status_code == 200

    with SessionLocal() as db:
        room_id = db.scalar(select(Room.id).where(Room.number == "102"))
        save_setting(db, "restrict_cleaning_to_reception", "1")
        db.commit()
    blocked = client.post(f"/housekeeping/rooms/{room_id}/status", data={"status": "available"},
                          follow_redirects=False)
    assert blocked.status_code == 403

    with SessionLocal() as db:
        save_setting(db, "restrict_cleaning_to_reception", "0")
        db.commit()
    allowed = client.post(f"/housekeeping/rooms/{room_id}/status", data={"status": "available"},
                          follow_redirects=False)
    assert allowed.status_code == 303


def test_settings_are_editable_by_admin(client):
    login(client, "admin")
    response = client.post("/admin/settings", data={
        "s_hotel_name": "Hôtel Test Annaba", "s_currency_symbol": "DA", "s_vat_rate": "9",
        "s_tourist_tax_per_person_night": "100", "s_tourist_tax_enabled": "1",
    }, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        settings = load_settings(db)
        assert settings["hotel_name"] == "Hôtel Test Annaba"
        assert settings["vat_rate"] == 9
        assert settings["tourist_tax_enabled"] is True
        assert settings["restrict_cleaning_to_reception"] is False  # case absente = décochée
        save_setting(db, "vat_rate", "19")
        save_setting(db, "hotel_name", "Hôtel Annaba (à personnaliser)")
        db.commit()
