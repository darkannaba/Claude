"""Licence d'exploitation liée au matériel (Hardware Bound Licensing), signée Ed25519.

Format de clé : ``HPMS1.<charge utile base64url>.<signature base64url>``.
La charge utile JSON contient l'hôtel, l'ID matériel, le nombre de chambres, les modules, les dates.
Seul l'éditeur détient la clé privée (scripts/generate_license.py) ; l'application ne contient que
la clé publique et ne peut donc pas fabriquer de licence.
"""
from __future__ import annotations

import base64
import hashlib
import json
import platform
import secrets
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import config
from app.models.licensing import LicenseInfo
from app.models.room import Room
from app.models.user import User

KEY_PREFIX = "HPMS1"
FEATURES = {"housekeeping": "Housekeeping", "pos": "Points de vente", "ota": "Channel manager OTA"}
LICENSE_TYPES = {"trial": "Essai", "annual": "Abonnement", "lifetime": "À vie"}
CLOCK_TOLERANCE = timedelta(hours=24)
CACHE_SECONDS = 60
FEATURE_PATHS = (
    ("/housekeeping", "housekeeping"),
    ("/pos", "pos"),
    ("/api/v1/ota", "ota"),
    ("/admin/ota", "ota"),
)


class LicenseError(Exception):
    """Clé illisible, signature invalide, machine différente, licence expirée…"""


# ---------------------------------------------------------------- empreinte matérielle
def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""


def _run(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# Valeurs du BIOS lues dans le registre Windows (lisibles sans droits administrateur, sans sous-processus)
_WINDOWS_BIOS_VALUES = ("SystemManufacturer", "SystemProductName", "BaseBoardManufacturer", "BaseBoardProduct")
# Fichiers DMI lisibles par tout utilisateur sous Linux (product_uuid et board_serial exigent root : exclus)
_LINUX_DMI_FILES = ("sys_vendor", "product_name", "board_vendor", "board_name")


def _windows_registry(path: str, names: tuple[str, ...]) -> dict[str, str]:
    try:
        import winreg  # noqa: PLC0415 – disponible uniquement sous Windows

        values = {}
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            for name in names:
                try:
                    values[name] = str(winreg.QueryValueEx(key, name)[0]).strip().lower()
                except OSError:
                    continue
        return {k: v for k, v in values.items() if v}
    except (ImportError, OSError):
        return {}


def hardware_components() -> dict[str, str]:
    """Identifiants STABLES de la machine.

    Seules des sources déterministes sont retenues : le même PC donne toujours le même ID, que l'application soit
    lancée par ``run.bat``, en service, en ligne de commande, avec ou sans droits administrateur.

    - Windows : MachineGuid (registre, fixé à l'installation de Windows) + modèle de carte mère et de PC (registre).
    - Linux : /etc/machine-id + modèle DMI lisible sans droits root.
    - macOS : IOPlatformUUID.

    L'adresse MAC est exclue : la carte réseau « principale » change avec un VPN, le Wi-Fi, un câble ou une station
    d'accueil. Les commandes externes (PowerShell…) aussi : leur échec ou leur lenteur changerait l'ID d'un lancement à
    l'autre. La MAC et le nom d'hôte ne servent qu'en tout dernier recours, si aucune source stable n'existe.
    """
    components: dict[str, str] = {}
    system = platform.system()
    if system == "Windows":
        guid = _windows_registry(r"SOFTWARE\Microsoft\Cryptography", ("MachineGuid",))
        if guid:
            components["machine_guid"] = guid["MachineGuid"]
        components.update(_windows_registry(r"HARDWARE\DESCRIPTION\System\BIOS", _WINDOWS_BIOS_VALUES))
    elif system == "Linux":
        machine_id = _read_text("/etc/machine-id") or _read_text("/var/lib/dbus/machine-id")
        if machine_id:
            components["machine_id"] = machine_id
        for name in _LINUX_DMI_FILES:
            value = _read_text(f"/sys/class/dmi/id/{name}")
            if value:
                components[name] = value.lower()
    elif system == "Darwin":
        for line in _run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]).splitlines():
            if "IOPlatformUUID" in line:
                components["platform_uuid"] = line.split("=")[-1].strip().strip('"').lower()
    if not components:
        node = uuid.getnode()
        if not (node >> 40) & 1:  # bit « multicast » : MAC générée aléatoirement par Python, non fiable
            components["mac"] = f"{node:012x}"
        components["hostname"] = platform.node().lower()
    return components


@lru_cache(maxsize=1)
def hardware_fingerprint() -> str:
    """ID matériel lisible, ex. ``HW-3F2A-91C0-7B4D-0E18-A6C2`` (SHA-256 des composants)."""
    components = hardware_components()
    material = "|".join(f"{key}={value}" for key, value in sorted(components.items()))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20].upper()
    return "HW-" + "-".join(digest[i:i + 4] for i in range(0, 20, 4))


# ---------------------------------------------------------------- format et signature
def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class LicenseData:
    license_id: str
    hotel_name: str
    hardware_id: str
    max_rooms: int
    features: list[str] = field(default_factory=list)
    license_type: str = "annual"
    issued_at: date = field(default_factory=date.today)
    expires_at: date | None = None

    def is_expired(self, today: date) -> bool:
        return self.expires_at is not None and today > self.expires_at

    def days_left(self, today: date) -> int | None:
        return None if self.expires_at is None else (self.expires_at - today).days

    @property
    def type_label(self) -> str:
        return LICENSE_TYPES.get(self.license_type, self.license_type)

    def to_payload(self) -> dict:
        return {
            "v": 1, "license_id": self.license_id, "hotel_name": self.hotel_name, "hardware_id": self.hardware_id,
            "max_rooms": self.max_rooms, "features": sorted(self.features), "license_type": self.license_type,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "LicenseData":
        try:
            features = [str(f) for f in payload["features"]]
            unknown = set(features) - set(FEATURES)
            if unknown:
                raise LicenseError(f"Module inconnu dans la licence : {', '.join(sorted(unknown))}.")
            if payload["license_type"] not in LICENSE_TYPES:
                raise LicenseError("Type de licence inconnu.")
            return cls(
                license_id=str(payload["license_id"]), hotel_name=str(payload["hotel_name"]),
                hardware_id=str(payload["hardware_id"]).upper(), max_rooms=int(payload["max_rooms"]),
                features=features, license_type=str(payload["license_type"]),
                issued_at=date.fromisoformat(payload["issued_at"]),
                expires_at=date.fromisoformat(payload["expires_at"]) if payload.get("expires_at") else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LicenseError("Contenu de licence incomplet ou corrompu.") from exc


def generate_keypair() -> tuple[bytes, bytes]:
    """Nouvelle paire de clés éditeur : (clé privée PEM, clé publique PEM)."""
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption())
    public_pem = private_key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo)
    return private_pem, public_pem


def issue_license_key(private_pem: bytes, *, hotel_name: str, hardware_id: str, max_rooms: int, features: list[str],
                      license_type: str = "annual", days: int | None = 365, issued_at: date | None = None) -> str:
    """Génère une clé signée (usage éditeur uniquement)."""
    if license_type not in LICENSE_TYPES:
        raise LicenseError("Type de licence inconnu.")
    if license_type == "lifetime":
        days = None
    elif not days or days < 1:
        raise LicenseError("Durée de validité invalide.")
    issued = issued_at or date.today()
    data = LicenseData(
        license_id=f"LIC-{issued:%Y%m%d}-{secrets.token_hex(4).upper()}", hotel_name=hotel_name.strip(),
        hardware_id=hardware_id.strip().upper(), max_rooms=max_rooms, features=sorted(set(features)),
        license_type=license_type, issued_at=issued,
        expires_at=issued + timedelta(days=days) if days else None,
    )
    LicenseData.from_payload(data.to_payload())  # contrôle des modules et du type
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise LicenseError("La clé privée doit être une clé Ed25519.")
    body = _b64encode(json.dumps(data.to_payload(), separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = private_key.sign(f"{KEY_PREFIX}.{body}".encode("ascii"))
    return f"{KEY_PREFIX}.{body}.{_b64encode(signature)}"


def _split(key: str) -> tuple[str, str]:
    cleaned = "".join(key.split())
    parts = cleaned.split(".")
    if len(parts) != 3 or parts[0] != KEY_PREFIX:
        raise LicenseError("Format de clé invalide (attendu : HPMS1.xxxxx.yyyyy).")
    return parts[1], parts[2]


def decode_license(key: str) -> LicenseData:
    """Lit le contenu SANS vérifier la signature (inspection uniquement)."""
    body, _signature = _split(key)
    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, UnicodeDecodeError) as exc:
        raise LicenseError("Contenu de licence illisible.") from exc
    return LicenseData.from_payload(payload)


def verify_license(key: str, public_pem: bytes) -> LicenseData:
    """Vérifie la signature Ed25519 et retourne le contenu. N'examine ni la machine ni les dates."""
    body, signature = _split(key)
    public_key = serialization.load_pem_public_key(public_pem)
    if not isinstance(public_key, Ed25519PublicKey):
        raise LicenseError("La clé publique de l'éditeur doit être une clé Ed25519.")
    try:
        public_key.verify(_b64decode(signature), f"{KEY_PREFIX}.{body}".encode("ascii"))
    except (InvalidSignature, ValueError) as exc:
        raise LicenseError("Signature invalide : clé falsifiée ou émise par un autre éditeur.") from exc
    return decode_license(key)


def load_public_key() -> bytes | None:
    path = config.license_public_key_file
    return path.read_bytes() if path.exists() else None


def check_license(key: str, today: date, hardware_id: str | None = None, public_pem: bytes | None = None) -> LicenseData:
    """Contrôle complet : signature, machine, expiration."""
    public_pem = public_pem if public_pem is not None else load_public_key()
    if public_pem is None:
        raise LicenseError("Clé publique de l'éditeur absente (app/license_public_key.pem) : "
                           "exécutez « python scripts/generate_license.py keygen ».")
    data = verify_license(key, public_pem)
    current = (hardware_id or hardware_fingerprint()).upper()
    if data.hardware_id != current:
        raise LicenseError(f"Licence émise pour la machine {data.hardware_id}, ce serveur est {current}.")
    if data.is_expired(today):
        raise LicenseError(f"Licence expirée depuis le {data.expires_at:%d/%m/%Y}.")
    return data


# ---------------------------------------------------------------- état courant (base + cache)
@dataclass
class LicenseStatus:
    valid: bool
    reason: str
    hardware_id: str
    data: LicenseData | None = None
    record_id: int | None = None

    def allows(self, feature: str) -> bool:
        return bool(self.valid and self.data and feature in self.data.features)


_cache: dict[str, object] = {"status": None, "at": 0.0}


def invalidate_cache() -> None:
    _cache["status"] = None
    _cache["at"] = 0.0


async def evaluate(db: AsyncSession, now: datetime) -> LicenseStatus:
    hardware_id = hardware_fingerprint()
    record = await db.scalar(select(LicenseInfo).where(LicenseInfo.active.is_(True)).order_by(LicenseInfo.id.desc()))
    if record is None:
        return LicenseStatus(False, "Aucune licence activée sur ce serveur.", hardware_id)
    try:
        data = check_license(record.license_key, now.date(), hardware_id)
    except LicenseError as exc:
        record.status = "invalid"
        await db.commit()
        return LicenseStatus(False, str(exc), hardware_id, record_id=record.id)
    if record.last_checked_at and now < record.last_checked_at - CLOCK_TOLERANCE:
        return LicenseStatus(False, "Horloge système antérieure au dernier contrôle de licence : "
                                    "corrigez la date et l'heure du serveur.", hardware_id, data, record.id)
    if record.last_checked_at is None or now > record.last_checked_at:
        record.last_checked_at = now
    record.status = "active"
    await db.commit()
    return LicenseStatus(True, "Licence valide.", hardware_id, data, record.id)


async def current_status(session_factory) -> LicenseStatus:
    """État de la licence, mis en cache 60 s pour ne pas interroger la base à chaque requête."""
    cached = _cache["status"]
    if isinstance(cached, LicenseStatus) and time.monotonic() - float(_cache["at"]) < CACHE_SECONDS:
        return cached
    async with session_factory() as db:
        status = await evaluate(db, datetime.now())
    _cache["status"], _cache["at"] = status, time.monotonic()
    return status


async def activate(db: AsyncSession, key: str, user: User | None, now: datetime) -> LicenseData:
    """Active une clé sur ce serveur (remplace la licence précédente)."""
    data = check_license(key, now.date())
    room_count = await db.scalar(select(func.count(Room.id)).where(Room.active.is_(True)))
    if room_count and room_count > data.max_rooms:
        raise LicenseError(f"Cette licence couvre {data.max_rooms} chambres, l'hôtel en compte {room_count} actives.")
    await db.execute(update(LicenseInfo).values(active=False, status="replaced").where(LicenseInfo.active.is_(True)))
    db.add(LicenseInfo(
        license_key="".join(key.split()), license_id=data.license_id, hotel_name=data.hotel_name,
        hardware_id=data.hardware_id, license_type=data.license_type, max_rooms=data.max_rooms,
        features=",".join(data.features), issued_at=data.issued_at, expires_at=data.expires_at,
        status="active", active=True, activated_at=now, activated_by_id=user.id if user else None,
        last_checked_at=now,
    ))
    invalidate_cache()
    return data


def feature_for_path(path: str) -> str | None:
    for prefix, feature in FEATURE_PATHS:
        if path == prefix or path.startswith(prefix + "/"):
            return feature
    return None
