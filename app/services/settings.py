"""Paramètres métier de l'hôtel : modèle Pydantic validé, stocké en base, éditable par l'admin."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, get_args, get_origin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pos import OUTLETS
from app.models.system import FeatureFlag, Setting
from app.services.common import BusinessError

MODULES = dict(OUTLETS)


def _field(default, label: str, group: str, help_text: str = "", widget: str = "", **kwargs):
    return Field(default, title=label, json_schema_extra={"group": group, "help": help_text, "widget": widget}, **kwargs)


class HotelSettings(BaseModel):
    # Identité
    hotel_name: str = _field("Hôtel Annaba", "Nom de l'hôtel", "Identité", min_length=1, max_length=160)
    hotel_name_ar: str = _field("فندق عنابة", "Nom de l'hôtel (arabe)", "Identité", max_length=160)
    hotel_address: str = _field("Annaba, Algérie", "Adresse", "Identité", max_length=300)
    hotel_address_ar: str = _field("عنابة، الجزائر", "Adresse (arabe)", "Identité", max_length=300)
    hotel_phone: str = _field("", "Téléphone", "Identité", max_length=60)
    hotel_email: str = _field("", "Email", "Identité", max_length=160)
    hotel_category: str = _field("", "Classement (ex. 3 étoiles)", "Identité", max_length=60)
    hotel_nif: str = _field("", "NIF", "Identité", max_length=40)
    hotel_nis: str = _field("", "NIS", "Identité", max_length=40)
    hotel_rc: str = _field("", "Registre de commerce (RC)", "Identité", max_length=40)
    hotel_ai: str = _field("", "Article d'imposition (AI)", "Identité", max_length=40)
    # Devise & taxes
    currency_code: str = _field("DZD", "Code devise", "Devise & taxes", min_length=3, max_length=3)
    currency_symbol: str = _field("DA", "Symbole devise", "Devise & taxes", min_length=1, max_length=5)
    vat_rate_accommodation: int = _field(19, "TVA hébergement (%)", "Devise & taxes", ge=0, le=100)
    vat_rate_fnb: int = _field(19, "TVA restauration / bar (%)", "Devise & taxes", ge=0, le=100)
    vat_rate_services: int = _field(19, "TVA autres services (%)", "Devise & taxes", ge=0, le=100)
    tourist_tax_enabled: bool = _field(True, "Appliquer la taxe de séjour", "Devise & taxes")
    tourist_tax_per_person_night: int = _field(100, "Taxe de séjour (DA / nuitée / personne)", "Devise & taxes", ge=0)
    tourist_tax_include_children: bool = _field(False, "Taxe de séjour appliquée aussi aux enfants", "Devise & taxes")
    # Tarifs
    weekend_days: str = _field("4,5", "Jours de week-end (0=lundi … 6=dimanche)", "Tarifs",
                               "Vendredi et samedi = 4,5")
    weekend_surcharge_percent: int = _field(10, "Majoration week-end (%)", "Tarifs", ge=-100, le=300)
    # Séjour
    check_in_time: str = _field("14:00", "Heure d'arrivée standard", "Séjour", pattern=r"^\d{2}:\d{2}$")
    check_out_time: str = _field("12:00", "Heure de départ standard", "Séjour", pattern=r"^\d{2}:\d{2}$")
    require_id_for_checkin: bool = _field(True, "Pièce d'identité obligatoire au check-in", "Séjour")
    # Clôture & sauvegarde
    night_audit_hour: int = _field(2, "Heure de l'audit de nuit automatique (0-23)", "Clôture & sauvegarde", ge=0, le=23)
    auto_no_show: bool = _field(True, "Passer en no-show les arrivées non présentées lors de l'audit",
                                "Clôture & sauvegarde")
    backup_hour: int = _field(3, "Heure de la sauvegarde automatique (0-23)", "Clôture & sauvegarde", ge=0, le=23)
    backup_local_enabled: bool = _field(True, "Sauvegarde automatique sur disque local", "Clôture & sauvegarde")
    backup_drive_enabled: bool = _field(False, "Sauvegarde automatique sur Google Drive", "Clôture & sauvegarde")
    backup_retention_days: int = _field(30, "Rétention des sauvegardes (jours)", "Clôture & sauvegarde", ge=1, le=3650)
    backup_drive_folder_id: str = _field("", "Google Drive – identifiant du dossier de destination",
                                         "Clôture & sauvegarde", "Partie finale de l'URL du dossier Drive. Vide = racine.",
                                         max_length=120)
    # Facturation
    invoice_prefix: str = _field("FAC", "Préfixe des factures", "Facturation", min_length=1, max_length=10)
    pos_prefix: str = _field("POS", "Préfixe des additions", "Facturation", min_length=1, max_length=10)
    invoice_footer: str = _field("Merci de votre visite. À bientôt à Annaba !", "Pied de facture", "Facturation",
                                 widget="textarea", max_length=500)
    # Housekeeping
    restrict_cleaning_to_reception: bool = _field(
        False, "Réserver la modification des statuts de nettoyage à la Réception / Admin", "Housekeeping",
        "Si activé, la gouvernante consulte l'état des chambres sans pouvoir le modifier.")
    # Fiche de police
    police_form_title: str = _field("FICHE DE POLICE – RENSEIGNEMENTS VOYAGEUR", "Titre de la fiche de police",
                                    "Fiche de police", max_length=160)
    police_form_title_ar: str = _field("بطاقة الشرطة – معلومات المسافر", "Titre de la fiche (arabe)",
                                       "Fiche de police", max_length=160)
    police_form_notice: str = _field(
        "Document établi conformément à la réglementation en vigueur relative à l'hébergement des voyageurs. "
        "Informations destinées exclusivement aux services de sécurité.",
        "Mention légale", "Fiche de police", widget="textarea", max_length=1000)
    police_form_footer: str = _field("Je certifie l'exactitude des renseignements ci-dessus.",
                                     "Pied de fiche", "Fiche de police", widget="textarea", max_length=500)
    # Impression
    print_mode: Literal["browser", "network"] = _field(
        "browser", "Mode d'impression", "Impression",
        "« Navigateur » : boîte d'impression du poste. « Réseau » : envoi direct à l'imprimante (port RAW 9100).")
    thermal_printer_host: str = _field("", "Imprimante ticket 80 mm – adresse IP", "Impression", max_length=120)
    thermal_printer_port: int = _field(9100, "Imprimante ticket – port", "Impression", ge=1, le=65535)
    a4_printer_host: str = _field("", "Imprimante A4 – adresse IP (impression PDF directe)", "Impression", max_length=120)
    a4_printer_port: int = _field(9100, "Imprimante A4 – port", "Impression", ge=1, le=65535)
    receipt_width_chars: int = _field(42, "Largeur ticket (caractères)", "Impression", ge=24, le=64)
    # Système
    timezone: str = _field("Africa/Algiers", "Fuseau horaire (IANA)", "Système")

    @field_validator("weekend_days")
    @classmethod
    def _check_weekend(cls, value: str) -> str:
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if not all(p.isdigit() and 0 <= int(p) <= 6 for p in parts):
            raise ValueError("liste de chiffres 0 à 6 séparés par des virgules")
        return ",".join(parts)

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("fuseau horaire inconnu") from exc
        return value

    @property
    def weekend_set(self) -> set[int]:
        return {int(x) for x in self.weekend_days.split(",") if x}

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _field_kind(name: str) -> str:
    info = HotelSettings.model_fields[name]
    extra = info.json_schema_extra or {}
    if extra.get("widget"):
        return extra["widget"]
    annotation = info.annotation
    if annotation is bool:
        return "checkbox"
    if annotation is int:
        return "number"
    if get_origin(annotation) is Literal:
        return "select"
    return "text"


def _serialize(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _deserialize(name: str, raw: str) -> Any:
    annotation = HotelSettings.model_fields[name].annotation
    if annotation is bool:
        return raw in ("1", "true", "True", "on", "oui")
    return raw


async def load_hotel_settings(db: AsyncSession) -> HotelSettings:
    rows = {r.key: r.value for r in (await db.scalars(select(Setting))).all()}
    data = {k: _deserialize(k, v) for k, v in rows.items() if k in HotelSettings.model_fields}
    while True:
        try:
            return HotelSettings.model_validate(data)
        except ValidationError as exc:  # valeur corrompue en base : on retombe sur la valeur par défaut
            bad = {err["loc"][0] for err in exc.errors() if err.get("loc")}
            if not bad:
                return HotelSettings()
            for key in bad:
                data.pop(key, None)


async def save_hotel_settings(db: AsyncSession, form: dict[str, str]) -> HotelSettings:
    """Valide et enregistre les paramètres envoyés par le formulaire d'administration."""
    current = (await load_hotel_settings(db)).model_dump()
    for name in HotelSettings.model_fields:
        key = f"s_{name}"
        if _field_kind(name) == "checkbox":
            current[name] = key in form
        elif key in form:
            current[name] = str(form[key]).strip()
    try:
        validated = HotelSettings.model_validate(current)
    except ValidationError as exc:
        raise BusinessError(_validation_message(exc)) from exc

    existing = {r.key: r for r in (await db.scalars(select(Setting))).all()}
    for name, value in validated.model_dump().items():
        if name in existing:
            existing[name].value = _serialize(value)
        else:
            db.add(Setting(key=name, value=_serialize(value)))
    return validated


def _validation_message(exc: ValidationError) -> str:
    messages = []
    for err in exc.errors():
        name = err["loc"][0] if err.get("loc") else "?"
        title = HotelSettings.model_fields[name].title if name in HotelSettings.model_fields else name
        messages.append(f"{title} : {err['msg']}")
    return "Paramètres invalides – " + " ; ".join(messages)


async def save_settings_subset(db: AsyncSession, values: dict[str, Any]) -> HotelSettings:
    """Enregistre uniquement les clés fournies, sans toucher aux autres paramètres.

    Contrairement à ``save_hotel_settings`` (formulaire complet : toute case absente est décochée), un booléen
    absent de ``values`` reste inchangé. Les valeurs peuvent être typées (bool, int) ou textuelles (formulaire).
    """
    unknown = sorted(name for name in values if name not in HotelSettings.model_fields)
    if unknown:
        raise BusinessError("Paramètre inconnu : " + ", ".join(unknown))
    current = (await load_hotel_settings(db)).model_dump()
    for name, value in values.items():
        current[name] = _deserialize(name, value.strip()) if isinstance(value, str) else value
    try:
        validated = HotelSettings.model_validate(current)
    except ValidationError as exc:
        raise BusinessError(_validation_message(exc)) from exc

    rows = (await db.scalars(select(Setting).where(Setting.key.in_(list(values))))).all()
    existing = {r.key: r for r in rows}
    for name in values:
        serialized = _serialize(getattr(validated, name))
        if name in existing:
            existing[name].value = serialized
        else:
            db.add(Setting(key=name, value=serialized))
    return validated


async def seed_settings(db: AsyncSession) -> None:
    existing = {r.key for r in (await db.scalars(select(Setting))).all()}
    for name, value in HotelSettings().model_dump().items():
        if name not in existing:
            db.add(Setting(key=name, value=_serialize(value)))


def settings_form(hs: HotelSettings) -> list[dict]:
    """Structure du formulaire d'administration, groupée par section."""
    groups: dict[str, list[dict]] = {}
    for name, info in HotelSettings.model_fields.items():
        extra = info.json_schema_extra or {}
        kind = _field_kind(name)
        options = list(get_args(info.annotation)) if kind == "select" else []
        groups.setdefault(extra.get("group", "Divers"), []).append({
            "name": name, "label": info.title, "help": extra.get("help", ""), "kind": kind,
            "value": getattr(hs, name), "options": options,
        })
    return [{"title": title, "fields": fields} for title, fields in groups.items()]


async def seed_modules(db: AsyncSession) -> None:
    existing = {f.key for f in (await db.scalars(select(FeatureFlag))).all()}
    for key, label in MODULES.items():
        if key not in existing:
            db.add(FeatureFlag(key=key, label=label, enabled=False))


async def module_flags(db: AsyncSession) -> list[FeatureFlag]:
    return list((await db.scalars(select(FeatureFlag).order_by(FeatureFlag.key))).all())


async def enabled_modules(db: AsyncSession) -> dict[str, str]:
    return {f.key: f.label for f in await module_flags(db) if f.enabled}


def now_of(hs: HotelSettings) -> datetime:
    """Date-heure locale (naïve) dans le fuseau de l'hôtel."""
    return datetime.now(hs.tz).replace(tzinfo=None)


def today_of(hs: HotelSettings) -> date:
    return now_of(hs).date()
