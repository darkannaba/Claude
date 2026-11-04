"""Sauvegarde en ligne de commande (utilisable depuis le Planificateur de tâches Windows ou cron).

    python scripts/backup.py              # crée une sauvegarde horodatée dans backups/
    python scripts/backup.py --list       # liste les sauvegardes
    python scripts/backup.py --restore 2026-11-04_030000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import config  # noqa: E402
from app.services import backup  # noqa: E402
from app.services.common import BusinessError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Sauvegarde / restauration de la base de l'hôtel")
    parser.add_argument("--list", action="store_true", help="liste les sauvegardes existantes")
    parser.add_argument("--restore", metavar="NOM", help="restaure une sauvegarde (arrêtez le serveur avant)")
    parser.add_argument("--keep", type=int, default=config.backup_retention, help="nombre de sauvegardes automatiques conservées")
    args = parser.parse_args()
    try:
        if args.list:
            for info in backup.list_backups():
                print(f"{info.name:<22} {info.created_at:%d/%m/%Y %H:%M}  {info.reason:<20} {info.size_label}")
            return 0
        if args.restore:
            safety = backup.restore_backup(args.restore)
            print(f"Base restaurée depuis {args.restore} (sauvegarde de sécurité : {safety.name}).")
            return 0
        info = backup.create_backup("automatique")
        removed = backup.prune_backups(args.keep)
        print(f"Sauvegarde créée : {info.path} ({info.size_label}). {removed} ancienne(s) supprimée(s).")
        return 0
    except BusinessError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
