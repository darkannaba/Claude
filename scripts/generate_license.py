"""Outil ÉDITEUR : paire de clés et génération des licences d'activation.

    python scripts/generate_license.py keygen --out publisher_keys
        → publisher_keys/license_private_key.pem (À GARDER SECRÈTE, jamais livrée au client)
        → app/license_public_key.pem (livrée avec l'application)

    python scripts/generate_license.py hwid
        → ID matériel de la machine courante (à exécuter chez le client, ou lu sur sa page /activate)

    python scripts/generate_license.py issue --private-key publisher_keys/license_private_key.pem \\
        --hotel "Hôtel El Mountazah" --hardware-id HW-XXXX-XXXX-XXXX-XXXX-XXXX \\
        --type annual --days 365 --max-rooms 30 --features housekeeping,pos,ota

    python scripts/generate_license.py inspect HPMS1.xxxxx.yyyyy
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.licensing import (  # noqa: E402
    FEATURES, LICENSE_TYPES, LicenseError, decode_license, generate_keypair, hardware_fingerprint, issue_license_key,
    verify_license,
)

DEFAULT_PUBLIC = ROOT / "app" / "license_public_key.pem"


def cmd_keygen(args: argparse.Namespace) -> int:
    out = Path(args.out)
    private_path = out / "license_private_key.pem"
    if private_path.exists() and not args.force:
        print(f"Refus : {private_path} existe déjà (--force pour écraser ; les licences émises seraient invalidées).")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    private_pem, public_pem = generate_keypair()
    private_path.write_bytes(private_pem)
    os.chmod(private_path, 0o600)
    public_path = Path(args.public_out)
    public_path.write_bytes(public_pem)
    print(f"Clé privée éditeur : {private_path}  (à conserver hors du dépôt, sauvegardée et secrète)")
    print(f"Clé publique       : {public_path}  (à livrer avec l'application)")
    return 0


def cmd_hwid(_args: argparse.Namespace) -> int:
    print(hardware_fingerprint())
    return 0


def cmd_issue(args: argparse.Namespace) -> int:
    features = [f.strip() for f in args.features.split(",") if f.strip()]
    days = None if args.type == "lifetime" else (args.days or (30 if args.type == "trial" else 365))
    try:
        key = issue_license_key(
            Path(args.private_key).read_bytes(), hotel_name=args.hotel, hardware_id=args.hardware_id,
            max_rooms=args.max_rooms, features=features, license_type=args.type, days=days,
            issued_at=date.fromisoformat(args.issued) if args.issued else None,
        )
    except (LicenseError, OSError, ValueError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    data = decode_license(key)
    print(f"Licence {data.license_id} – {data.hotel_name} – {data.type_label} – "
          f"{'sans expiration' if not data.expires_at else 'expire le ' + data.expires_at.strftime('%d/%m/%Y')} – "
          f"{data.max_rooms} chambres – modules : {', '.join(data.features) or 'aucun'}", file=sys.stderr)
    print(key)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    try:
        data = decode_license(args.key)
        verified = ""
        public = Path(args.public_key)
        if public.exists():
            verify_license(args.key, public.read_bytes())
            verified = " (signature vérifiée)"
    except LicenseError as exc:
        print(f"Clé invalide : {exc}", file=sys.stderr)
        return 1
    print(f"Licence {data.license_id}{verified}")
    for label, value in (("Hôtel", data.hotel_name), ("ID matériel", data.hardware_id), ("Type", data.type_label),
                         ("Émise le", data.issued_at.strftime("%d/%m/%Y")),
                         ("Expire le", data.expires_at.strftime("%d/%m/%Y") if data.expires_at else "jamais"),
                         ("Chambres", data.max_rooms), ("Modules", ", ".join(data.features) or "aucun")):
        print(f"  {label:<12} {value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gestion des licences (éditeur)")
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="crée la paire de clés éditeur Ed25519")
    keygen.add_argument("--out", default="publisher_keys", help="dossier de la clé privée")
    keygen.add_argument("--public-out", default=str(DEFAULT_PUBLIC), help="fichier de la clé publique livrée")
    keygen.add_argument("--force", action="store_true")
    keygen.set_defaults(func=cmd_keygen)

    sub.add_parser("hwid", help="affiche l'ID matériel de cette machine").set_defaults(func=cmd_hwid)

    issue = sub.add_parser("issue", help="génère une clé d'activation")
    issue.add_argument("--private-key", required=True)
    issue.add_argument("--hotel", required=True)
    issue.add_argument("--hardware-id", required=True)
    issue.add_argument("--type", choices=sorted(LICENSE_TYPES), default="annual", help="trial, annual ou lifetime")
    issue.add_argument("--days", type=int, help="durée en jours (défaut : 30 pour un essai, 365 pour un abonnement)")
    issue.add_argument("--max-rooms", type=int, default=30)
    issue.add_argument("--features", default=",".join(FEATURES), help=f"parmi : {', '.join(FEATURES)}")
    issue.add_argument("--issued", help="date d'émission AAAA-MM-JJ (défaut : aujourd'hui)")
    issue.set_defaults(func=cmd_issue)

    inspect = sub.add_parser("inspect", help="affiche le contenu d'une clé")
    inspect.add_argument("key")
    inspect.add_argument("--public-key", default=str(DEFAULT_PUBLIC))
    inspect.set_defaults(func=cmd_inspect)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
