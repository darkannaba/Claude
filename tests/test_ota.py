"""Channel manager OTA : clés d'API, inventaire ARI, tarifs, webhook de réservation, surréservation."""
import asyncio
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Guest, OtaLog, Reservation
from app.services.settings import load_hotel_settings, today_of

KEY_RE = re.compile(r"(ota_[A-Za-z0-9_\-]{20,})")


def run(coro):
    return asyncio.run(coro)


async def _today() -> date:
    async with SessionLocal() as db:
        return today_of(await load_hotel_settings(db))


def _create_key(admin, name: str, channel: str = "booking", perms=("inventory:read", "rates:write", "reservations:write")):
    data = {"name": name, "channel": channel, **{f"perm_{p}": "1" for p in perms}}
    page = admin.form("/admin/ota/keys", data)
    assert page.status_code == 200, page.text[:500]
    match = KEY_RE.search(page.text)
    assert match, "clé non affichée"
    return match.group(1)


@pytest.fixture(scope="module")
def api(app):
    return TestClient(app)  # client machine-à-machine : aucun cookie de session


@pytest.fixture(scope="module")
def booking_key(app):
    from tests.conftest import make_client
    return _create_key(make_client(app, "admin"), "Booking.com production")


def _guest(email="ota.client@example.com", **extra):
    return {"first_name": "Nadia", "last_name": "Kaci", "email": email, "phone": "+213 661 22 33 44",
            "nationality": "Algérienne", **extra}


def test_authentication_and_permissions(api, admin):
    assert api.get("/api/v1/ota/inventory").status_code == 401
    assert api.get("/api/v1/ota/inventory", headers={"X-OTA-API-KEY": "ota_inconnue"}).status_code == 401
    read_only = _create_key(admin, "Lecture seule", "expedia", ("inventory:read",))
    assert api.get("/api/v1/ota/inventory", headers={"X-OTA-API-KEY": read_only}).status_code == 200
    denied = api.post("/api/v1/ota/rates", headers={"X-OTA-API-KEY": read_only},
                      json={"rates": [{"room_type": "STD", "start": "2030-01-01", "end": "2030-01-02", "price": 1}]})
    assert denied.status_code == 403 and "rates:write" in denied.json()["detail"]
    # Clé en clair jamais stockée : seule l'empreinte figure en base
    assert read_only not in admin.get("/admin/ota").text


def test_inventory_rates_and_booking_impact(api, booking_key):
    headers = {"X-OTA-API-KEY": booking_key}
    today = run(_today())
    start, end = today + timedelta(days=200), today + timedelta(days=207)
    inventory = api.get(f"/api/v1/ota/inventory?start={start}&end={end}", headers=headers).json()
    assert inventory["currency"] == "DZD" and len(inventory["room_types"][0]["days"]) == 7
    std = next(rt for rt in inventory["room_types"] if rt["code"] == "STD")
    before = std["days"][0]["available"]
    assert before == std["total_rooms"] - std["out_of_service"]

    updated = api.post("/api/v1/ota/rates", headers=headers,
                       json={"rates": [{"room_type": "std", "start": str(start), "end": str(end), "price": 12345}]})
    assert updated.json() == {"created": 1, "updated": 0}
    again = api.post("/api/v1/ota/rates", headers=headers,
                     json={"rates": [{"room_type": "STD", "start": str(start), "end": str(end), "price": 13000}]})
    assert again.json() == {"created": 0, "updated": 1}
    bad = api.post("/api/v1/ota/rates", headers=headers,
                   json={"rates": [{"room_type": "XXL", "start": str(start), "end": str(end), "price": 1}]})
    assert bad.status_code == 422
    weekday = next(d for d in std["days"] if date.fromisoformat(d["date"]).weekday() not in (4, 5))
    inventory = api.get(f"/api/v1/ota/inventory?start={start}&end={end}", headers=headers).json()
    std = next(rt for rt in inventory["room_types"] if rt["code"] == "STD")
    assert next(d for d in std["days"] if d["date"] == weekday["date"])["rate"] == 13000

    created = api.post("/api/v1/ota/reservations/webhook", headers=headers, json={
        "event": "new", "external_id": "BDC-INV-1", "room_type": "STD", "check_in": str(start),
        "check_out": str(start + timedelta(days=2)), "adults": 2, "guest": _guest("inv@example.com")})
    assert created.status_code == 201
    inventory = api.get(f"/api/v1/ota/inventory?start={start}&end={end}", headers=headers).json()
    std = next(rt for rt in inventory["room_types"] if rt["code"] == "STD")
    assert std["days"][0]["available"] == before - 1 and std["days"][2]["available"] == before
    assert api.get(f"/api/v1/ota/inventory?start={end}&end={start}", headers=headers).status_code == 422
    assert api.get(f"/api/v1/ota/inventory?start={start}&end={start + timedelta(days=400)}",
                   headers=headers).status_code == 422


def test_reservation_webhook_lifecycle(api, admin, booking_key):
    headers = {"X-OTA-API-KEY": booking_key}
    today = run(_today())
    check_in, check_out = today + timedelta(days=120), today + timedelta(days=123)
    # Personne distincte des autres tests (le rapprochement CRM compare aussi téléphone et nom)
    person = _guest("ota.lifecycle@example.com", last_name="Benkaci", phone="+213 770 55 66 77")
    payload = {"event": "new", "external_id": "BDC-5001", "room_type": "SUP", "check_in": str(check_in),
               "check_out": str(check_out), "adults": 2, "guest": person, "total_amount": 36000,
               "prepaid_amount": 36000, "notes": "Arrivée tardive"}
    created = api.post("/api/v1/ota/reservations/webhook", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "created" and body["guest_created"] is True

    async def _res(res_id):
        async with SessionLocal() as db:
            return await db.get(Reservation, res_id)
    res = run(_res(body["reservation_id"]))
    assert res.channel == "Booking.com" and res.nightly_rate_override == 12000 and "BDC-5001" in res.notes
    detail = admin.get(f"/reservations/{res.id}").text
    assert "tarif Booking.com" in detail and "Booking.com BDC-5001" in detail  # folio et prépaiement

    # Livraison en double : idempotente
    assert api.post("/api/v1/ota/reservations/webhook", headers=headers, json=payload).json()["status"] == "duplicate"
    # Nouveau séjour du même client (email) : fiche CRM réutilisée
    second = api.post("/api/v1/ota/reservations/webhook", headers=headers, json={
        **payload, "external_id": "BDC-5002", "guest": {**person, "first_name": "NADIA", "phone": ""}})
    assert second.json()["guest_created"] is False

    async def _guests():
        async with SessionLocal() as db:
            return await db.scalar(select(func.count(Guest.id)).where(Guest.email == "ota.lifecycle@example.com"))
    assert run(_guests()) == 1

    modified = api.post("/api/v1/ota/reservations/webhook", headers=headers, json={
        "event": "modify", "external_id": "BDC-5001", "room_type": "SUITE", "check_in": str(check_in),
        "check_out": str(check_out + timedelta(days=1)), "adults": 3, "nightly_rate": 15000})
    assert modified.status_code == 200 and modified.json()["status"] == "modified"
    res = run(_res(body["reservation_id"]))
    assert res.nights == 4 and res.adults == 3 and res.nightly_rate_override == 15000

    cancel = {"event": "cancel", "external_id": "BDC-5001"}
    assert api.post("/api/v1/ota/reservations/webhook", headers=headers, json=cancel).json()["status"] == "cancelled"
    assert api.post("/api/v1/ota/reservations/webhook", headers=headers, json=cancel).json()["status"] == "already_cancelled"
    assert run(_res(body["reservation_id"])).status == "cancelled"
    assert api.post("/api/v1/ota/reservations/webhook", headers=headers,
                    json={"event": "cancel", "external_id": "INCONNUE"}).status_code == 404
    # Modification d'une réservation annulée : refusée proprement
    refused = api.post("/api/v1/ota/reservations/webhook", headers=headers, json={
        "event": "modify", "external_id": "BDC-5001", "room_type": "SUP", "check_in": str(check_in),
        "check_out": str(check_out)})
    assert refused.status_code == 409 and refused.json()["status"] == "rejected"


def test_validation_and_past_dates(api, booking_key):
    headers = {"X-OTA-API-KEY": booking_key}
    today = run(_today())
    missing_guest = api.post("/api/v1/ota/reservations/webhook", headers=headers, json={
        "event": "new", "external_id": "BDC-X1", "room_type": "STD", "check_in": str(today + timedelta(days=5)),
        "check_out": str(today + timedelta(days=6))})
    assert missing_guest.status_code == 422
    past = api.post("/api/v1/ota/reservations/webhook", headers=headers, json={
        "event": "new", "external_id": "BDC-PAST", "room_type": "STD", "check_in": str(today - timedelta(days=3)),
        "check_out": str(today - timedelta(days=1)), "guest": _guest("past@example.com")})
    assert past.status_code == 409 and "passée" in past.json()["detail"]
    unknown_type = api.post("/api/v1/ota/reservations/webhook", headers=headers, json={
        "event": "new", "external_id": "BDC-T", "room_type": "PENTHOUSE", "check_in": str(today + timedelta(days=5)),
        "check_out": str(today + timedelta(days=6)), "guest": _guest("t@example.com")})
    assert unknown_type.status_code == 409


def test_overbooking_protection(api, booking_key):
    headers = {"X-OTA-API-KEY": booking_key}
    today = run(_today())
    check_in, check_out = today + timedelta(days=300), today + timedelta(days=302)
    inventory = api.get(f"/api/v1/ota/inventory?start={check_in}&end={check_out}", headers=headers).json()
    suites = next(rt for rt in inventory["room_types"] if rt["code"] == "SUITE")["days"][0]["available"]
    for index in range(suites):
        response = api.post("/api/v1/ota/reservations/webhook", headers=headers, json={
            "event": "new", "external_id": f"EXP-OB-{index}", "room_type": "SUITE", "check_in": str(check_in),
            "check_out": str(check_out), "guest": _guest(f"ob{index}@example.com", last_name=f"Client{index}")})
        assert response.status_code == 201, response.text
    overbooked = api.post("/api/v1/ota/reservations/webhook", headers=headers, json={
        "event": "new", "external_id": "EXP-OB-EXTRA", "room_type": "SUITE", "check_in": str(check_in),
        "check_out": str(check_out), "guest": _guest("extra@example.com", last_name="Extra")})
    assert overbooked.status_code == 409 and overbooked.json()["status"] == "overbooking"
    inventory = api.get(f"/api/v1/ota/inventory?start={check_in}&end={check_out}", headers=headers).json()
    day = next(rt for rt in inventory["room_types"] if rt["code"] == "SUITE")["days"][0]
    assert day["available"] == 0 and day["stop_sell"] is True


def test_sync_toggle_revocation_and_logs(api, admin):
    key = _create_key(admin, "Expedia test", "expedia")
    headers = {"X-OTA-API-KEY": key}
    page = admin.get("/admin/ota").text
    key_id = re.findall(r"/admin/ota/keys/(\d+)/sync", page)[0]
    assert admin.form(f"/admin/ota/keys/{key_id}/sync").status_code == 303
    suspended = api.get("/api/v1/ota/inventory", headers=headers)
    assert suspended.status_code == 403 and "désactivée" in suspended.json()["detail"]
    admin.form(f"/admin/ota/keys/{key_id}/sync")
    assert api.get("/api/v1/ota/inventory", headers=headers).status_code == 200
    assert admin.form(f"/admin/ota/keys/{key_id}/revoke").status_code == 303
    assert api.get("/api/v1/ota/inventory", headers=headers).status_code == 401
    assert admin.form("/admin/ota/keys/999999/revoke").status_code == 303
    assert admin.form("/admin/ota/keys", {"name": "", "channel": "booking"}).headers["location"] == "/admin/ota"
    assert "Nom de la clé requis" in admin.get("/admin/ota").text

    async def _log_count():
        async with SessionLocal() as db:
            return await db.scalar(select(func.count(OtaLog.id)).where(OtaLog.http_status >= 400))
    assert run(_log_count()) > 0
    logs = admin.get("/admin/ota?status=errors").text
    assert "auth_refused" in logs
    assert "reservation_new" in admin.get("/admin/ota?channel=booking").text
