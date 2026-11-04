"""Parcours métier complets via HTTP : séjour, folio, documents, audit de nuit, POS, départ, recherche, rapports."""
import asyncio
import io
import re
from datetime import timedelta

from openpyxl import load_workbook
from sqlalchemy import select

from app.database import SessionLocal
from app.models import FolioItem, Guest, Invoice, PosItem, Reservation, Room
from app.services.settings import load_hotel_settings, today_of


def run(coro):
    return asyncio.run(coro)


async def _today():
    async with SessionLocal() as db:
        return today_of(await load_hotel_settings(db))


async def _room_id(number: str) -> int:
    async with SessionLocal() as db:
        return await db.scalar(select(Room.id).where(Room.number == number))


async def _reservation(res_id: int) -> Reservation:
    async with SessionLocal() as db:
        return await db.get(Reservation, res_id)


def _res_id(response) -> int:
    match = re.search(r"/reservations/(\d+)$", response.headers["location"])
    assert match, response.headers["location"]
    return int(match.group(1))


def _flashes(client, url: str) -> str:
    return client.get(url).text


def test_full_stay_lifecycle(admin, reception, housekeeper):
    today = run(_today())
    room_id = run(_room_id("101"))

    # Modules annexes activés par l'administrateur
    assert admin.form("/admin/modules", {"m_restaurant": "1", "m_parking": "1", "m_salle_reunion": "1"}).status_code == 303
    assert admin.form("/companies", {"name": "Sonatrach Test", "nif": "123", "rc": "RC1", "discount_percent": "10"}).status_code == 303

    created = reception.form("/reservations", {
        "check_in": (today - timedelta(days=1)).isoformat(), "check_out": (today + timedelta(days=2)).isoformat(),
        "room_id": str(room_id), "g_last_name": "Benali", "g_first_name": "Karim", "g_last_name_ar": "بن علي",
        "g_first_name_ar": "كريم", "g_phone": "0661000001", "g_id_type": "cni", "g_id_number": "FLOW-123-456",
        "g_nationality": "Algérienne", "adults": "2", "children": "0", "channel": "Téléphone",
        "coming_from": "Alger", "going_to": "Annaba", "deposit": "5000", "deposit_method": "cib",
    })
    assert created.status_code == 303, created.text
    res_id = _res_id(created)
    url = f"/reservations/{res_id}"
    detail = reception.get(url)
    assert detail.status_code == 200 and "Benali" in detail.text and "prévisionnel" in detail.text

    # Accompagnateurs
    assert reception.form(f"{url}/companions", {"last_name": "Benali", "first_name": "Amel", "first_name_ar": "أمال",
                                                "relationship_label": "Épouse", "id_type": "cni",
                                                "id_number": "COMP-1", "birth_date": "1990-01-01"}).status_code == 303
    assert reception.form(f"{url}/companions", {"last_name": "", "first_name": ""}).status_code == 303
    page = reception.get(url).text
    assert "Amel" in page and "Nom et prénom de l&#39;accompagnateur requis." in page

    # Documents
    for doc in ("police.pdf", "invoice.pdf?format=a4", "invoice.pdf?format=ticket"):
        response = reception.get(f"{url}/{doc}")
        assert response.status_code == 200 and response.content.startswith(b"%PDF"), doc
    assert "<svg" in reception.get(f"{url}/qr.svg").text
    assert reception.get(f"/print?doc={url}/police.pdf&back={url}").status_code == 200
    assert reception.get("/print?doc=https://evil.example/x.pdf").status_code == 400

    # Arrivée, extra, encaissement, annulation d'un extra
    assert reception.form(f"{url}/checkin").status_code == 303
    assert run(_reservation(res_id)).status == "checked_in"
    assert reception.form(f"{url}/extras", {"description": "Blanchisserie", "amount": "1500", "vat": "services"}).status_code == 303
    assert reception.form(f"{url}/extras", {"description": "Erreur", "amount": "700", "vat": "fnb"}).status_code == 303

    async def _item(description):
        async with SessionLocal() as db:
            return await db.scalar(select(FolioItem.id).where(FolioItem.reservation_id == res_id,
                                                               FolioItem.description == description))
    error_item = run(_item("Erreur"))
    assert reception.form(f"{url}/items/{error_item}/void", {"reason": "saisie"}).status_code == 303
    assert run(_item("Erreur")) is None  # journée ouverte : suppression pure
    assert reception.form(f"{url}/payments", {"amount": "3000", "kind": "paiement", "method": "especes"}).status_code == 303
    assert reception.form(f"{url}/payments", {"amount": "0", "kind": "paiement", "method": "especes"}).status_code == 303
    assert reception.form(f"{url}/details", {"channel": "Email", "notes": "Vue mer", "coming_from": "Oran"}).status_code == 303

    # Audit de nuit : la nuit d'hier est figée
    assert reception.form("/night-audit/run", {"mode": "pending"}).status_code == 303
    page = reception.get("/night-audit").text
    assert (today - timedelta(days=1)).strftime("%d/%m/%Y") in page
    assert reception.form("/night-audit/run", {"mode": "pending"}).status_code == 303  # rien à clôturer : message
    report = reception.get(f"/night-audit/{(today - timedelta(days=1)).isoformat()}/report.pdf")
    assert report.status_code == 200 and report.content.startswith(b"%PDF")
    assert reception.get("/night-audit/pas-une-date/report.pdf").status_code == 400

    # POS : article suivi en stock, addition imputée sur la chambre, addition comptant, annulation
    assert admin.form("/pos/catalog", {"outlet": "restaurant", "name": "Café serré", "category": "Boissons",
                                        "price": "200", "vat_rate": "9", "track_stock": "1", "active": "1",
                                        "low_stock_threshold": "2"}).status_code == 303

    async def _item_id(name):
        async with SessionLocal() as db:
            return await db.scalar(select(PosItem.id).where(PosItem.name == name))
    item_id = run(_item_id("Café serré"))
    assert reception.form(f"/pos/catalog/{item_id}/stock", {"quantity": "10", "reason": "reappro", "reference": "BL-1"}).status_code == 303
    order = reception.form("/pos/orders", {"outlet": "restaurant", "label": "Table 4"})
    order_url = order.headers["location"]
    assert reception.get(order_url).status_code == 200
    assert reception.form(f"{order_url}/lines", {"item_id": str(item_id), "quantity": "3"}).status_code == 303
    assert reception.form(f"{order_url}/lines", {"item_id": str(item_id), "quantity": "50"}).status_code == 303  # stock insuffisant
    assert reception.form(f"{order_url}/charge", {"reservation_id": str(res_id)}).status_code == 303
    assert "Imputée chambre" in reception.get(order_url).text
    assert reception.get(f"{order_url}/ticket.pdf").content.startswith(b"%PDF")

    cash = reception.form("/pos/orders", {"outlet": "restaurant", "label": "Comptoir"}).headers["location"]
    reception.form(f"{cash}/lines", {"item_id": str(item_id), "quantity": "1"})
    page = reception.get(cash).text
    line_id = re.search(r"/lines/(\d+)/delete", page).group(1)
    assert reception.form(f"{cash}/lines/{line_id}/delete").status_code == 303
    reception.form(f"{cash}/lines", {"item_id": str(item_id), "quantity": "2"})
    assert reception.form(f"{cash}/pay", {"method": "especes"}).status_code == 303
    assert "Payée comptant" in reception.get(cash).text
    void = reception.form("/pos/orders", {"outlet": "parking", "label": "16-12345"}).headers["location"]
    assert reception.form(f"{void}/void").headers["location"] == "/pos"
    assert reception.get("/pos").status_code == 200 and reception.get("/pos/stock").status_code == 200

    async def _stock():
        async with SessionLocal() as db:
            return (await db.get(PosItem, item_id)).stock_qty
    assert run(_stock()) == 5  # 10 - 3 (room charge) - 2 (comptant)

    # Départ refusé tant que le solde n'est pas réglé, puis accepté
    assert reception.form(f"{url}/checkout").status_code == 303
    assert run(_reservation(res_id)).status == "checked_in"
    detail = reception.get(url).text
    balance = int(re.search(r'name="amount" min="1" value="(\d+)"', detail).group(1))
    assert reception.form(f"{url}/payments", {"amount": str(balance), "kind": "paiement", "method": "cib",
                                              "reference": "TPE"}).status_code == 303
    assert reception.form(f"{url}/checkout").status_code == 303
    res = run(_reservation(res_id))
    assert res.status == "checked_out" and res.check_out == today

    async def _invoice():
        async with SessionLocal() as db:
            return await db.scalar(select(Invoice).where(Invoice.reservation_id == res_id))
    invoice = run(_invoice())
    assert invoice.number.startswith("FAC-") and invoice.balance == 0
    assert invoice.number in reception.get("/invoices").text
    final = reception.get(f"{url}/invoice.pdf?format=a4")
    assert final.status_code == 200

    # Recherche universelle (douchette / QR)
    assert reception.get(f"/search?q={res.code}", follow_redirects=False).headers["location"] == url
    assert reception.get(f"/search?q={invoice.number}", follow_redirects=False).headers["location"] == url
    assert reception.get("/search?q=flow-123 456", follow_redirects=False).headers["location"].startswith("/guests/")
    assert reception.get(f"/search?q={order_url.rsplit('/', 1)[1]}").status_code == 200
    data = reception.get("/api/search?q=Benali").json()
    assert any(g["name"] == "Karim Benali" for g in data["guests"])
    assert reception.get("/api/search?q=B").json() == {"guests": []}
    assert reception.get("/guests?q=FLOW-123-456", follow_redirects=False).status_code == 303

    # Rapports et exports (admin)
    assert reception.get("/reports?granularity=week").status_code == 200
    csv_export = admin.get(f"/reports/export.csv?start={(today - timedelta(days=3)).isoformat()}&end={today.isoformat()}")
    assert csv_export.status_code == 200 and "Hébergement" in csv_export.content.decode("utf-8-sig")
    xlsx = admin.get("/reports/export.xlsx")
    workbook = load_workbook(io.BytesIO(xlsx.content))
    assert workbook.sheetnames == ["Écritures", "Synthèse"]

    # Housekeeping : chambre libérée à nettoyer puis remise en service
    assert housekeeper.form(f"/housekeeping/rooms/{room_id}/status", {"status": "available", "floor": "1"}).status_code == 303

    async def _room_status():
        async with SessionLocal() as db:
            return (await db.get(Room, room_id)).status
    assert run(_room_status()) == "available"
    assert housekeeper.form(f"/housekeeping/rooms/{room_id}/status", {"status": "occupied"}).status_code == 303
    assert run(_room_status()) == "available"  # statut « occupée » non modifiable à la main


def test_reservation_update_cancel_and_no_show(reception):
    today = run(_today())
    room = run(_room_id("302"))
    other = run(_room_id("303"))
    created = reception.form("/reservations", {
        "check_in": (today + timedelta(days=20)).isoformat(), "check_out": (today + timedelta(days=22)).isoformat(),
        "room_id": str(room), "g_last_name": "Haddad", "g_first_name": "Samia", "g_id_type": "passport",
        "g_id_number": "UPD-777", "adults": "1"})
    res_id = _res_id(created)
    url = f"/reservations/{res_id}"
    assert reception.form(f"{url}/update", {"room_id": str(other), "check_in": (today + timedelta(days=21)).isoformat(),
                                            "check_out": (today + timedelta(days=24)).isoformat(), "adults": "2",
                                            "children": "0"}).status_code == 303
    res = run(_reservation(res_id))
    assert res.room_id == other and res.nights == 3
    assert reception.form(f"{url}/room", {"new_room_id": str(room)}).status_code == 303
    assert run(_reservation(res_id)).room_id == room
    assert reception.form(f"{url}/noshow").status_code == 303  # trop tôt : refusé
    assert run(_reservation(res_id)).status == "confirmed"
    assert reception.form(f"{url}/checkin").status_code == 303  # arrivée future : refusée
    assert reception.form(f"{url}/cancel", {"reason": "Changement de programme"}).status_code == 303
    assert run(_reservation(res_id)).status == "cancelled"
    assert reception.get(f"{url}/invoice.pdf").status_code == 400
    # Réservation sur la même chambre et le même client existant (doublon de pièce retrouvé automatiquement)
    again = reception.form("/reservations", {
        "check_in": (today + timedelta(days=21)).isoformat(), "check_out": (today + timedelta(days=22)).isoformat(),
        "room_id": str(room), "g_last_name": "Autre", "g_first_name": "Nom", "g_id_type": "passport",
        "g_id_number": "upd 777", "adults": "1"})
    new_res = run(_reservation(_res_id(again)))
    assert new_res.guest_id == res.guest_id
    # Erreurs de saisie : retour au formulaire
    bad = reception.form("/reservations", {"check_in": "2030-01-01", "check_out": "2030-01-01", "room_id": str(room)})
    assert bad.headers["location"].startswith("/reservations/new")
    assert reception.form("/reservations", {"check_in": "x"}).headers["location"].startswith("/reservations/new")


def test_room_change_in_house_and_companion_delete(reception):
    today = run(_today())
    room = run(_room_id("303"))
    target = run(_room_id("304"))
    res_id = _res_id(reception.form("/reservations", {
        "check_in": today.isoformat(), "check_out": (today + timedelta(days=3)).isoformat(), "room_id": str(room),
        "g_last_name": "Meziane", "g_first_name": "Yanis", "g_id_type": "cni", "g_id_number": "MOVE-1"}))
    url = f"/reservations/{res_id}"
    reception.form(f"{url}/checkin")
    assert reception.form(f"{url}/room", {"new_room_id": str(target), "from_date": today.isoformat()}).status_code == 303
    res = run(_reservation(res_id))
    assert res.room_id == target
    reception.form(f"{url}/companions", {"last_name": "Meziane", "first_name": "Lina"})
    page = reception.get(url).text
    companion_id = re.search(r"/companions/(\d+)/delete", page).group(1)
    assert reception.form(f"{url}/companions/{companion_id}/delete").status_code == 303
    assert reception.form(f"{url}/companions/999999/delete").status_code == 303
    assert reception.form(f"{url}/checkout", {"force": "1"}).status_code == 303  # aucune nuit écoulée : refusé
    assert run(_reservation(res_id)).status == "checked_in"


async def _guest_by_id_number(number: str) -> Guest | None:
    async with SessionLocal() as db:
        return await db.scalar(select(Guest).where(Guest.id_number == number))
