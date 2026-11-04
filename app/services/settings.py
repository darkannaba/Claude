"""Paramètres métier stockés en base, et modules annexes (feature flags).

Les valeurs ci-dessous ne sont que des VALEURS PAR DÉFAUT insérées au premier
démarrage. Ensuite, tout se modifie depuis Administration > Paramètres : le code
ne contient aucune valeur métier en dur.
"""
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.admin import FeatureFlag, Setting

# clé: (valeur par défaut, type, libellé, groupe)
DEFAULTS: dict[str, tuple] = {
    "hotel_name": ("Hôtel Annaba (à personnaliser)", "str", "Nom de l'hôtel", "Identité"),
    "hotel_address": ("Annaba, Algérie", "str", "Adresse", "Identité"),
    "hotel_phone": ("", "str", "Téléphone", "Identité"),
    "hotel_email": ("", "str", "Email", "Identité"),
    "hotel_nif": ("", "str", "NIF / NIS", "Identité"),
    "hotel_rc": ("", "str", "Registre de commerce (RC)", "Identité"),
    "currency_code": ("DZD", "str", "Code devise", "Devise & taxes"),
    "currency_symbol": ("DA", "str", "Symbole devise", "Devise & taxes"),
    "vat_rate": (19, "int", "Taux de TVA (%) appliqué aux prestations", "Devise & taxes"),
    "tourist_tax_enabled": (True, "bool", "Appliquer la taxe de séjour", "Devise & taxes"),
    "tourist_tax_per_person_night": (100, "int", "Taxe de séjour (DA / nuitée / personne)", "Devise & taxes"),
    "weekend_days": ("4,5", "str", "Jours de week-end (0=Lundi … 6=Dimanche)", "Tarifs"),
    "weekend_surcharge_percent": (10, "int", "Majoration week-end (%)", "Tarifs"),
    "check_in_time": ("14:00", "str", "Heure d'arrivée standard", "Séjour"),
    "check_out_time": ("12:00", "str", "Heure de départ standard", "Séjour"),
    "timezone": ("Africa/Algiers", "str", "Fuseau horaire (IANA)", "Système"),
    "invoice_prefix": ("FAC", "str", "Préfixe des numéros de facture", "Facturation"),
    "restrict_cleaning_to_reception": (
        False, "bool", "Réserver les statuts de nettoyage à la Réception / Admin (gouvernante en lecture seule)",
        "Housekeeping",
    ),
    "police_form_title": (
        "FICHE DE POLICE – RENSEIGNEMENTS VOYAGEURS", "str", "Titre de la fiche de police", "Fiche de police",
    ),
    "police_form_notice": (
        "Document établi conformément à la réglementation en vigueur. Informations à usage exclusif "
        "des services de sécurité.", "text", "Mention légale", "Fiche de police",
    ),
    "police_form_footer": (
        "Le voyageur certifie l'exactitude des renseignements fournis.", "text", "Pied de fiche", "Fiche de police",
    ),
}

MODULES = {
    "restaurant": "Restaurant / Bar",
    "salle_reunion": "Salle de réunion",
    "parking": "Parking",
}


def _cast(raw, typ: str):
    if typ == "bool":
        return str(raw).strip().lower() in ("1", "true", "on", "yes", "oui")
    if typ == "int":
        try:
            return int(float(str(raw).strip() or 0))
        except ValueError:
            return 0
    return "" if raw is None else str(raw)


def _store(value, typ: str) -> str:
    if typ == "bool":
        return "1" if value else "0"
    return str(value)


def load_settings(db: Session) -> dict:
    rows = {r.key: r.value for r in db.scalars(select(Setting))}
    out = {}
    for key, (default, typ, _label, _group) in DEFAULTS.items():
        out[key] = _cast(rows[key], typ) if key in rows else default
    return out


def save_setting(db: Session, key: str, raw) -> None:
    typ = DEFAULTS[key][1]
    value = _store(_cast(raw, typ), typ)
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def setting_groups(db: Session) -> dict[str, list[dict]]:
    values = load_settings(db)
    groups: dict[str, list[dict]] = {}
    for key, (_default, typ, label, group) in DEFAULTS.items():
        groups.setdefault(group, []).append({"key": key, "label": label, "type": typ, "value": values[key]})
    return groups


def seed_settings(db: Session) -> None:
    existing = {r.key for r in db.scalars(select(Setting))}
    for key, (default, typ, _label, _group) in DEFAULTS.items():
        if key not in existing:
            db.add(Setting(key=key, value=_store(default, typ)))


def seed_modules(db: Session) -> None:
    existing = {r.key for r in db.scalars(select(FeatureFlag))}
    for key, label in MODULES.items():
        if key not in existing:
            db.add(FeatureFlag(key=key, label=label, enabled=False))


def module_flags(db: Session) -> list[FeatureFlag]:
    return list(db.scalars(select(FeatureFlag).order_by(FeatureFlag.key)))


def enabled_modules(db: Session) -> dict[str, str]:
    return {f.key: f.label for f in module_flags(db) if f.enabled}


def weekend_set(settings: dict) -> set[int]:
    return {int(x) for x in str(settings["weekend_days"]).split(",") if x.strip().isdigit()}


def now_of(settings: dict) -> datetime:
    """Date-heure locale (naïve) selon le fuseau configuré."""
    try:
        tz = ZoneInfo(settings["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("Africa/Algiers")
    return datetime.now(tz).replace(tzinfo=None)


def today_of(settings: dict):
    return now_of(settings).date()
