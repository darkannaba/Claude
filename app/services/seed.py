"""Initialisation de la base : structure, paramètres par défaut, 30 chambres, utilisateurs.

Usage :
    python -m app.services.seed                       # idempotent
    python -m app.services.seed --reset-password admin # régénère le mot de passe d'un utilisateur
    DATABASE_URL=sqlite:///data/hotel_template.db python -m app.services.seed --build-template
        # produit la base modèle livrée (structure + 30 chambres, sans comptes)
"""
import secrets
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import SessionLocal, init_db
from app.models.admin import User
from app.models.property import Room, RoomType
from app.services.auth import hash_password
from app.services.settings import seed_modules, seed_settings

# Données de départ : 30 chambres. Tarifs TTC INDICATIFS, à ajuster dans Administration > Chambres.
ROOM_TYPES = [
    ("STD", "Chambre standard", 8000, 2),
    ("SUP", "Chambre supérieure", 11000, 3),
    ("SUITE", "Suite", 16000, 4),
]
ROOM_PLAN = [
    (1, "STD", [f"1{n:02d}" for n in range(1, 11)]),     # 101 → 110
    (2, "SUP", [f"2{n:02d}" for n in range(1, 11)]),     # 201 → 210
    (3, "SUITE", [f"3{n:02d}" for n in range(1, 11)]),   # 301 → 310
]
DEFAULT_USERS = [
    ("admin", "Administrateur", "admin"),
    ("reception", "Réception", "reception"),
    ("gouvernante", "Gouvernante", "housekeeping"),
]


def seed_rooms(db: Session) -> None:
    if db.scalar(select(func.count(Room.id))):
        return
    types = {}
    for code, name, price, capacity in ROOM_TYPES:
        rt = RoomType(code=code, name=name, base_price_ttc=price, max_occupancy=capacity)
        db.add(rt)
        types[code] = rt
    db.flush()
    for floor, type_code, numbers in ROOM_PLAN:
        for number in numbers:
            db.add(Room(number=number, floor=floor, room_type_id=types[type_code].id, status="available"))


def seed_users(db: Session) -> list[tuple[str, str]]:
    """Crée les comptes par défaut si la table est vide. Retourne (login, mot de passe) affichés une seule fois."""
    if db.scalar(select(func.count(User.id))):
        return []
    created = []
    for username, full_name, role in DEFAULT_USERS:
        password = secrets.token_urlsafe(9)
        db.add(User(username=username, full_name=full_name, role=role, password_hash=hash_password(password)))
        created.append((username, password))
    return created


def reset_password(db: Session, username: str) -> str | None:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        return None
    password = secrets.token_urlsafe(9)
    user.password_hash = hash_password(password)
    return password


def run(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    init_db()
    with SessionLocal() as db:
        if "--reset-password" in argv:
            username = argv[argv.index("--reset-password") + 1]
            password = reset_password(db, username)
            db.commit()
            if password is None:
                print(f"Utilisateur introuvable : {username}")
            else:
                print(f"Nouveau mot de passe pour « {username} » : {password}")
            return

        seed_settings(db)
        seed_modules(db)
        seed_rooms(db)
        if "--build-template" in argv:
            db.commit()
            print("Base modèle générée (sans comptes utilisateurs).")
            return
        created = seed_users(db)
        db.commit()
        if created:
            print("=" * 60)
            print("Comptes créés (à noter, affichés une seule fois) :")
            for username, password in created:
                print(f"  {username:<14} {password}")
            print("Changez ces mots de passe dans Administration > Utilisateurs.")
            print("=" * 60)


if __name__ == "__main__":
    run()
