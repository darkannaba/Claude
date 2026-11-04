"""Initialisation de production.

    python scripts/seed_prod.py                      # schéma + paramètres + 30 chambres + comptes
    python scripts/seed_prod.py --demo               # + données de démonstration (formation)
    python scripts/seed_prod.py --reset-password admin
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal  # noqa: E402
from app.services.seed import bootstrap, print_created_users, reset_password, seed_demo_data  # noqa: E402


async def main(args: argparse.Namespace) -> int:
    created = await bootstrap()
    print_created_users(created)
    if not created:
        print("Base déjà initialisée : schéma mis à jour, données de référence complétées.")
    if args.reset_password:
        async with SessionLocal() as db:
            password = await reset_password(db, args.reset_password)
            await db.commit()
        if password is None:
            print(f"Utilisateur introuvable : {args.reset_password}")
            return 1
        print(f"Nouveau mot de passe provisoire pour « {args.reset_password} » : {password}")
    if args.demo:
        async with SessionLocal() as db:
            summary = await seed_demo_data(db)
            await db.commit()
        if summary["reservations"]:
            print(f"Données de démonstration : {summary['guests']} clients, {summary['reservations']} réservations, "
                  f"{summary['items']} articles POS (modules annexes activés).")
        else:
            print("Des clients existent déjà : données de démonstration non ajoutées.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialisation de la base de production")
    parser.add_argument("--demo", action="store_true", help="ajoute des données de démonstration")
    parser.add_argument("--reset-password", metavar="UTILISATEUR", help="régénère un mot de passe provisoire")
    sys.exit(asyncio.run(main(parser.parse_args())))
