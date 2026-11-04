"""Authentification, sécurité, administration et CRM via HTTP."""
import asyncio
import io
import re
import sqlite3
from datetime import timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AuditLog, Guest, RatePeriod, Room, RoomType, User
from app.routers.auth import throttle
from app.services.settings import load_hotel_settings, today_of
from tests.conftest import PASSWORDS, make_client


def run(coro):
    return asyncio.run(coro)


async def _scalar(stmt):
    async with SessionLocal() as db:
        return await db.scalar(stmt)


# ---------------------------------------------------------------- authentification
def test_login_failures_and_lockout(app):
    client = make_client(app)
    bad = client.form("/login", {"username": "reception", "password": "mauvais"})
    assert bad.status_code == 401
    try:
        for _ in range(6):
            client.form("/login", {"username": "inconnu", "password": "x"})
        locked = client.form("/login", {"username": "inconnu", "password": "x"})
        assert locked.status_code == 429
    finally:
        throttle._failures.clear()
    assert run(_scalar(select(AuditLog.id).where(AuditLog.action == "login_failed").limit(1)))


def test_csrf_is_required(app):
    client = make_client(app, "reception")
    response = client.post("/reservations/1/checkin", data={}, follow_redirects=False)
    assert response.status_code == 403 and "Session expirée" in response.text
    response = client.post("/reservations/1/checkin", data={"csrf_token": "faux"}, follow_redirects=False)
    assert response.status_code == 403


def test_login_redirects_and_logout(app):
    client = make_client(app)
    response = client.form("/login", {"username": "admin", "password": PASSWORDS["admin"], "next": "/planning"})
    assert response.headers["location"] == "/planning"
    assert client.get("/login", follow_redirects=False).status_code == 303  # déjà connecté
    assert client.form("/logout").headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_forced_password_change_for_new_user(admin, app):
    created = admin.form("/admin/users", {"username": "nadia", "full_name": "Nadia", "role": "reception",
                                          "password": "Provisoire-2026"})
    assert created.status_code == 303
    weak = admin.form("/admin/users", {"username": "faible", "role": "reception", "password": "court"})
    assert weak.status_code == 303
    assert run(_scalar(select(User.id).where(User.username == "faible"))) is None

    client = make_client(app)
    client.form("/login", {"username": "nadia", "password": "Provisoire-2026"})
    assert client.get("/", follow_redirects=False).headers["location"] == "/account/password"
    client.refresh_csrf()
    errors = client.form("/account/password", {"current": "faux", "new": "abc", "confirm": "abd"})
    assert errors.status_code == 400
    ok = client.form("/account/password", {"current": "Provisoire-2026", "new": "Nouveau-mdp-2026", "confirm": "Nouveau-mdp-2026"})
    assert ok.status_code == 303
    assert client.get("/").status_code == 200

    user_id = run(_scalar(select(User.id).where(User.username == "nadia")))
    assert admin.form(f"/admin/users/{user_id}", {"full_name": "Nadia B.", "role": "housekeeping", "active": "1",
                                                  "password": "Reinit-mdp-2026"}).status_code == 303
    admin_id = run(_scalar(select(User.id).where(User.username == "admin")))
    admin.form(f"/admin/users/{admin_id}", {"full_name": "Admin", "role": "reception", "active": "1"})
    assert run(_scalar(select(User.role).where(User.id == admin_id))) == "admin"  # auto-rétrogradation refusée


# ---------------------------------------------------------------- administration
def test_settings_validation_and_save(admin):
    ok = admin.form("/admin/settings", {"s_hotel_name": "Hôtel El Mountazah", "s_hotel_name_ar": "فندق المنتزه",
                                        "s_vat_rate_accommodation": "9", "s_tourist_tax_per_person_night": "150",
                                        "s_tourist_tax_enabled": "1", "s_weekend_days": "4,5", "s_timezone": "Africa/Algiers",
                                        "s_print_mode": "browser", "s_require_id_for_checkin": "1", "s_auto_no_show": "1"})
    assert ok.status_code == 303
    page = admin.get("/admin").text
    assert "Hôtel El Mountazah" in page and "Paramètres enregistrés." in page
    bad = admin.form("/admin/settings", {"s_vat_rate_accommodation": "250", "s_weekend_days": "9", "s_timezone": "Mars/Base"})
    assert bad.status_code == 303
    page = admin.get("/admin").text
    assert "Paramètres invalides" in page

    async def _hs():
        async with SessionLocal() as db:
            return await load_hotel_settings(db)
    hs = run(_hs())
    assert hs.vat_rate_accommodation == 9 and hs.tourist_tax_per_person_night == 150
    # Remise des valeurs par défaut pour les autres tests
    admin.form("/admin/settings", {"s_hotel_name": "Hôtel Annaba", "s_vat_rate_accommodation": "19",
                                   "s_tourist_tax_per_person_night": "100", "s_tourist_tax_enabled": "1",
                                   "s_require_id_for_checkin": "1", "s_auto_no_show": "1", "s_print_mode": "browser"})
    assert run(_hs()).vat_rate_accommodation == 19
    assert admin.form("/admin/printers/test").status_code == 303  # aucune imprimante : message d'erreur propre
    assert "Aucune imprimante réseau configurée" in admin.get("/admin").text


def test_rooms_types_and_rates_admin(admin):
    assert admin.form("/admin/room-types", {"code": "fam", "name": "Familiale", "base_price": "14000",
                                            "max_occupancy": "5"}).status_code == 303
    assert admin.form("/admin/room-types", {"code": "FAM", "name": "Doublon", "base_price": "1"}).status_code == 303
    type_id = run(_scalar(select(RoomType.id).where(RoomType.code == "FAM")))
    assert admin.form(f"/admin/room-types/{type_id}", {"name": "Familiale+", "base_price": "15000",
                                                       "max_occupancy": "5"}).status_code == 303
    assert admin.form("/admin/rooms", {"number": "401", "floor": "4", "room_type_id": str(type_id)}).status_code == 303
    assert admin.form("/admin/rooms", {"number": "401", "floor": "4", "room_type_id": str(type_id)}).status_code == 303
    room_id = run(_scalar(select(Room.id).where(Room.number == "401")))
    assert admin.form(f"/admin/rooms/{room_id}", {"number": "402", "floor": "4", "room_type_id": str(type_id),
                                                  "notes": "Vue mer"}).status_code == 303  # désactivée (case absente)
    room = run(_scalar(select(Room).where(Room.id == room_id)))
    assert room.number == "402" and room.active is False

    assert admin.form("/admin/rates", {"name": "Été", "room_type_id": str(type_id), "start_date": "2031-06-15",
                                       "end_date": "2031-09-15", "price": "18000", "priority": "1"}).status_code == 303
    assert admin.form("/admin/rates", {"name": "Invalide", "room_type_id": str(type_id), "start_date": "2031-06-15",
                                       "end_date": "2031-06-01", "price": "1"}).status_code == 303
    rate_id = run(_scalar(select(RatePeriod.id).where(RatePeriod.name == "Été")))
    assert "Été" in admin.get("/admin/rates").text
    assert admin.form(f"/admin/rates/{rate_id}/delete").status_code == 303
    assert run(_scalar(select(RatePeriod.id).where(RatePeriod.id == rate_id))) is None
    assert admin.get("/admin/audit-log?action=rate_deleted").status_code == 200
    assert admin.get("/admin/openapi.json").json()["info"]["title"].startswith("PMS")


def test_backups_create_download_restore_upload(admin, tmp_dir):
    assert admin.form("/admin/backups").status_code == 303
    page = admin.get("/admin/backups").text
    names = re.findall(r"/admin/backups/([0-9_\-]+)/download", page)
    assert names
    name = names[0]
    download = admin.get(f"/admin/backups/{name}/download")
    assert download.status_code == 200 and download.content[:15] == b"SQLite format 3"
    assert admin.form(f"/admin/backups/{name}/restore").status_code == 303
    assert "Base restaurée" in admin.get("/admin/backups").text
    # Import d'un fichier invalide puis d'une vraie sauvegarde
    bad = admin.post("/admin/backups/upload", data={"csrf_token": admin.csrf},
                     files={"file": ("pas-une-base.db", b"bonjour", "application/octet-stream")}, follow_redirects=False)
    assert bad.status_code == 303
    assert "pas une base de données SQLite valide" in admin.get("/admin/backups").text
    other = tmp_dir / "autre.db"
    sqlite3.connect(other).execute("create table t(x)").connection.commit()
    missing = admin.post("/admin/backups/upload", data={"csrf_token": admin.csrf},
                         files={"file": ("autre.db", other.read_bytes(), "application/octet-stream")}, follow_redirects=False)
    assert missing.status_code == 303
    assert "tables manquantes" in admin.get("/admin/backups").text
    good = admin.post("/admin/backups/upload", data={"csrf_token": admin.csrf},
                      files={"file": ("ok.db", download.content, "application/octet-stream")}, follow_redirects=False)
    assert good.status_code == 303
    assert admin.form(f"/admin/backups/{name}/delete").status_code == 303
    assert admin.get(f"/admin/backups/{name}/download").status_code == 404
    assert admin.form("/admin/backups/.secret/delete").status_code == 303
    assert "Nom de sauvegarde invalide" in admin.get("/admin/backups").text


# ---------------------------------------------------------------- CRM
def test_guest_crud_duplicates_status_and_merge(admin, reception):
    created = reception.form("/guests", {"first_name": "Leïla", "last_name": "Bouzid", "id_type": "cni",
                                         "id_number": "CRM-0001", "phone": "0555 12 34 56", "birth_date": "1990-05-05",
                                         "nationality": "Algérienne", "first_name_ar": "ليلى", "last_name_ar": "بوزيد"})
    assert created.status_code == 303
    guest_url = created.headers["location"]
    guest_id = int(guest_url.rsplit("/", 1)[1])
    duplicate = reception.form("/guests", {"first_name": "X", "last_name": "Y", "id_type": "cni", "id_number": "crm 0001"})
    assert duplicate.headers["location"] == "/guests/new"
    assert "Doublon détecté" in reception.get("/guests/new").text
    check = reception.get("/api/guests/check-identity?id_type=cni&id_number=CRM-0001").json()
    assert check["duplicate"]["id"] == guest_id
    assert reception.get(f"/api/guests/check-identity?id_type=cni&id_number=CRM-0001&exclude={guest_id}").json() == {"duplicate": None}
    assert reception.form(guest_url, {"first_name": "Leïla", "last_name": "Bouzid", "id_type": "cni",
                                      "id_number": "CRM-0001", "profession": "Enseignante",
                                      "phone": "0555 12 34 56", "birth_date": "1990-05-05"}).status_code == 303
    assert reception.get(guest_url).status_code == 200
    assert reception.get("/guests?q=بوزيد").status_code == 200
    assert reception.get("/guests?status=vip").status_code == 200

    # VIP par la réception, liste noire réservée à l'admin
    assert reception.form(f"{guest_url}/status", {"status": "vip", "reason": "Fidèle"}).status_code == 303
    assert run(_scalar(select(Guest.status).where(Guest.id == guest_id))) == "vip"
    reception.form(f"{guest_url}/status", {"status": "blacklist", "reason": "Impayé"})
    assert run(_scalar(select(Guest.status).where(Guest.id == guest_id))) == "vip"
    admin.form(f"{guest_url}/status", {"status": "blacklist", "reason": ""})
    assert run(_scalar(select(Guest.status).where(Guest.id == guest_id))) == "vip"  # motif obligatoire
    admin.form(f"{guest_url}/status", {"status": "blacklist", "reason": "Dégradations"})
    assert run(_scalar(select(Guest.status).where(Guest.id == guest_id))) == "blacklist"

    # Réservation refusée pour un client sur liste noire, sauf forçage administrateur
    async def _room_and_today():
        async with SessionLocal() as db:
            room = await db.scalar(select(Room.id).where(Room.number == "305"))
            return room, today_of(await load_hotel_settings(db))
    room_id, today = run(_room_and_today())
    data = {"check_in": (today + timedelta(days=40)).isoformat(), "check_out": (today + timedelta(days=41)).isoformat(),
            "room_id": str(room_id), "guest_id": str(guest_id)}
    assert reception.form("/reservations", data).headers["location"].startswith("/reservations/new")
    assert re.search(r"/reservations/\d+$", admin.form("/reservations", {**data, "override_blacklist": "1"}).headers["location"])
    assert "Liste noire" in reception.get(f"/reservations/new?guest_id={guest_id}").text

    # Doublon potentiel (même nom + date de naissance) puis fusion par l'admin
    twin = reception.form("/guests", {"first_name": "Leïla", "last_name": "Bouzid", "birth_date": "1990-05-05",
                                      "id_type": "passport", "id_number": "P-TWIN"})
    twin_id = int(twin.headers["location"].rsplit("/", 1)[1])
    assert "Bouzid" in reception.get("/guests/duplicates").text
    assert reception.form("/guests/merge", {"keep_id": str(guest_id), "remove_id": str(twin_id)}).status_code == 403
    assert admin.form("/guests/merge", {"keep_id": str(guest_id), "remove_id": str(twin_id)}).headers["location"] == f"/guests/{guest_id}"
    assert run(_scalar(select(Guest.id).where(Guest.id == twin_id))) is None
    assert admin.form("/guests/merge", {"keep_id": str(guest_id), "remove_id": str(guest_id)}).headers["location"] == "/guests/duplicates"
    assert reception.get("/guests/unpaid").status_code == 200
    assert reception.get("/guests/999999").status_code == 404


def test_companies_admin_only(admin, reception):
    assert reception.form("/companies", {"name": "Refusée"}).status_code == 403
    assert admin.form("/companies", {"name": "Groupe Annaba Port", "discount_percent": "15"}).status_code == 303
    page = admin.get("/companies").text
    company_id = re.findall(r'action="/companies/(\d+)"', page)[-1]
    assert admin.form(f"/companies/{company_id}", {"name": "Groupe Annaba Port", "discount_percent": "120"}).status_code == 303
    assert "Remise doit être inférieur" in admin.get("/companies").text
    assert admin.form("/companies/999999", {"name": "X"}).status_code == 303


def test_housekeeping_restriction_option(admin, housekeeper):
    room_id = run(_scalar(select(Room.id).where(Room.number == "110")))
    assert housekeeper.form(f"/housekeeping/rooms/{room_id}/status", {"status": "dirty"}).status_code == 303
    admin.form("/admin/settings", {"s_restrict_cleaning_to_reception": "1", "s_tourist_tax_enabled": "1",
                                   "s_require_id_for_checkin": "1", "s_auto_no_show": "1"})
    assert "lecture seule" in housekeeper.get("/housekeeping").text
    assert housekeeper.form(f"/housekeeping/rooms/{room_id}/status", {"status": "available"}).status_code == 403
    admin.form("/admin/settings", {"s_tourist_tax_enabled": "1", "s_require_id_for_checkin": "1", "s_auto_no_show": "1"})
    assert housekeeper.form(f"/housekeeping/rooms/{room_id}/status", {"status": "available"}).status_code == 303
    assert housekeeper.form("/housekeeping/rooms/999999/status", {"status": "available"}).status_code == 404


def test_modules_toggle_hides_pos(admin, reception):
    admin.form("/admin/modules", {})
    assert "Aucun module annexe" in reception.get("/pos").text
    assert reception.form("/pos/orders", {"outlet": "restaurant"}).headers["location"] == "/pos"
    admin.form("/admin/modules", {"m_restaurant": "1", "m_parking": "1", "m_salle_reunion": "1"})
    assert "Nouvelle addition" in reception.get("/pos").text
    assert io  # import utilisé pour la cohérence des tests d'export
