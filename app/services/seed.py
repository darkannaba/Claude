"""Initialisation de production : schéma, paramètres, modules, 30 chambres, comptes ; données de démo optionnelles."""
from __future__ import annotations

import asyncio
import secrets
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal, run_migrations
from app.models.customer import Company, Guest, normalize_id_number
from app.models.pos import PosItem
from app.models.room import Room, RoomType
from app.models.system import FeatureFlag
from app.models.user import User
from app.services.pricing_admin import ensure_pricing_defaults
from app.services.security import hash_password
from app.services.settings import load_hotel_settings, seed_modules, seed_settings, today_of

# Types de chambres (tarifs TTC de départ, modifiables dans Administration > Chambres).
ROOM_TYPES = [
    ("STD", "Chambre standard", "غرفة عادية", 8000, 2, "Lit double ou deux lits simples, salle d'eau."),
    ("SUP", "Chambre supérieure", "غرفة ممتازة", 11000, 3, "Plus spacieuse, coin salon, vue dégagée."),
    ("SUITE", "Suite", "جناح", 16000, 4, "Chambre et salon séparés."),
]
ROOM_PLAN = [
    (1, "STD", [f"1{n:02d}" for n in range(1, 11)]),
    (2, "SUP", [f"2{n:02d}" for n in range(1, 11)]),
    (3, "SUITE", [f"3{n:02d}" for n in range(1, 11)]),
]
DEFAULT_USERS = [
    ("admin", "Administrateur", "admin"),
    ("reception", "Réception", "reception"),
    ("gouvernante", "Gouvernante", "housekeeping"),
]


async def seed_reference_data(db: AsyncSession) -> None:
    await seed_settings(db)
    await seed_modules(db)
    if not await db.scalar(select(func.count(Room.id))):
        types = {}
        for code, name, name_ar, price, capacity, description in ROOM_TYPES:
            room_type = RoomType(code=code, name=name, name_ar=name_ar, base_price=price,
                                 max_occupancy=capacity, description=description)
            db.add(room_type)
            types[code] = room_type
        await db.flush()
        for floor, code, numbers in ROOM_PLAN:
            for number in numbers:
                db.add(Room(number=number, floor=floor, room_type_id=types[code].id, status="available"))
    await db.flush()
    # Tarification dynamique : saisons d'exemple (inactives) créées une seule fois, gérées ensuite dans
    # Administration > Gestion des tarifs.
    await ensure_pricing_defaults(db)


async def create_default_users(db: AsyncSession) -> list[tuple[str, str]]:
    """Crée les comptes initiaux si aucun utilisateur n'existe (mots de passe aléatoires, à changer)."""
    if await db.scalar(select(func.count(User.id))):
        return []
    created = []
    for username, full_name, role in DEFAULT_USERS:
        password = secrets.token_urlsafe(10)
        db.add(User(username=username, full_name=full_name, role=role,
                    password_hash=hash_password(password), must_change_password=True))
        created.append((username, password))
    await db.flush()
    return created


async def reset_password(db: AsyncSession, username: str) -> str | None:
    user = await db.scalar(select(User).where(User.username == username))
    if user is None:
        return None
    password = secrets.token_urlsafe(10)
    user.password_hash = hash_password(password)
    user.must_change_password = True
    user.active = True
    return password


async def seed_demo_data(db: AsyncSession) -> dict[str, int]:
    """Données de démonstration (formation du personnel) : entreprises, clients, séjours, articles POS."""
    from app.services import booking  # import local : évite un cycle au chargement

    hs = await load_hotel_settings(db)
    today = today_of(hs)
    if await db.scalar(select(func.count(Guest.id))):
        return {"guests": 0, "reservations": 0, "items": 0}

    company = Company(name="Sarl Méditerranée Logistique", nif="000123456789012", rc="23B0012345",
                      address="Zone industrielle Pont-Bouchet, Annaba", discount_percent=10)
    db.add(company)
    people = [
        ("Karim", "Benali", "كريم", "بن علي", "cni", "109876543210987654", "Annaba", "Ingénieur"),
        ("Samia", "Haddad", "سامية", "حداد", "cni", "209876543210987655", "Constantine", "Médecin"),
        ("Yanis", "Meziane", "يانيس", "مزيان", "passport", "AB1234567", "Alger", "Commercial"),
        ("Leïla", "Bouzid", "ليلى", "بوزيد", "cni", "209876543210987656", "Guelma", "Enseignante"),
        ("Marco", "Rossi", "", "", "passport", "YA7654321", "Roma", "Architecte"),
    ]
    guests = []
    for first, last, first_ar, last_ar, id_type, id_number, birth_place, profession in people:
        guest = Guest(first_name=first, last_name=last, first_name_ar=first_ar, last_name_ar=last_ar,
                      id_type=id_type, id_number=id_number, id_number_norm=normalize_id_number(id_number),
                      birth_place=birth_place, profession=profession,
                      nationality="Italienne" if last == "Rossi" else "Algérienne",
                      phone=f"0550{len(guests):06d}", address=f"{birth_place}, Algérie")
        db.add(guest)
        guests.append(guest)
    await db.flush()

    rooms = {r.number: r for r in (await db.scalars(select(Room))).unique().all()}
    plan = [
        (guests[0], "101", -2, 2, company, True),
        (guests[1], "205", -1, 3, None, True),
        (guests[2], "301", 0, 2, None, False),
        (guests[3], "102", 1, 4, None, False),
        (guests[4], "206", 3, 6, None, False),
    ]
    count = 0
    for guest, number, start, end, comp, arrived in plan:
        res = await booking.create_reservation(
            db, guest=guest, room=rooms[number], check_in=today + timedelta(days=start),
            check_out=today + timedelta(days=end), hs=hs, user=None, adults=1, company=comp,
            channel="Direct", coming_from="Alger", going_to="Annaba")
        count += 1
        if arrived:
            res.status = "checked_in"
            res.checked_in_at = res.created_at
            rooms[number].status = "occupied"

    items = [
        ("restaurant", "Café", "Boissons chaudes", 150, True, 200),
        ("restaurant", "Thé à la menthe", "Boissons chaudes", 150, True, 200),
        ("restaurant", "Eau minérale 1,5 L", "Boissons", 80, True, 120),
        ("restaurant", "Jus d'orange pressé", "Boissons", 300, False, 0),
        ("restaurant", "Petit-déjeuner", "Restauration", 900, False, 0),
        ("restaurant", "Menu du jour", "Restauration", 1800, False, 0),
        ("parking", "Parking – nuit", "Parking", 300, False, 0),
        ("salle_reunion", "Salle de réunion – demi-journée", "Location", 12000, False, 0),
        ("salle_reunion", "Salle de réunion – journée", "Location", 20000, False, 0),
    ]
    for outlet, name, category, price, track, qty in items:
        vat = hs.vat_rate_fnb if outlet == "restaurant" else hs.vat_rate_services
        db.add(PosItem(outlet=outlet, name=name, category=category, price=price, vat_rate=vat,
                       track_stock=track, stock_qty=qty, low_stock_threshold=20 if track else 0))
    for flag in (await db.scalars(select(FeatureFlag))).all():
        flag.enabled = True
    await db.flush()
    return {"guests": len(guests), "reservations": count, "items": len(items)}


async def bootstrap(create_users: bool = True) -> list[tuple[str, str]]:
    """Migrations + données de référence ; retourne les comptes créés (affichés une seule fois)."""
    await asyncio.to_thread(run_migrations)
    async with SessionLocal() as db:
        await seed_reference_data(db)
        created = await create_default_users(db) if create_users else []
        await db.commit()
    return created


def print_created_users(created: list[tuple[str, str]]) -> None:
    if not created:
        return
    print("=" * 64)
    print("Comptes créés – notez ces mots de passe (affichés une seule fois) :")
    for username, password in created:
        print(f"  {username:<14} {password}")
    print("Un changement de mot de passe sera demandé à la première connexion.")
    print("=" * 64, flush=True)
