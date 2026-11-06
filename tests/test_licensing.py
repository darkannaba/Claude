"""Licence matérielle : format, signature, machine, expiration, middleware, activation, CLI éditeur."""
import asyncio
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import config
from app.database import SessionLocal
from app.models import LicenseInfo
from app.services import licensing
from app.services.licensing import LicenseError
from tests.conftest import make_client

ROOT = Path(__file__).resolve().parent.parent
ALL = ["housekeeping", "ota", "pos"]


@pytest.fixture(scope="module")
def keys():
    return licensing.generate_keypair()


@pytest.fixture()
def publisher(keys, tmp_path, monkeypatch):
    """Clé publique de test installée comme clé éditeur de l'application."""
    public_file = tmp_path / "license_public_key.pem"
    public_file.write_bytes(keys[1])
    monkeypatch.setattr(config, "license_public_key_file", public_file)
    licensing.invalidate_cache()
    yield keys
    licensing.invalidate_cache()


def _issue(private_pem, **overrides):
    params = {"hotel_name": "Hôtel Test", "hardware_id": licensing.hardware_fingerprint(), "max_rooms": 30,
              "features": ALL, "license_type": "annual", "days": 365}
    params.update(overrides)
    return licensing.issue_license_key(private_pem, **params)


# ---------------------------------------------------------------- unitaires
def test_hardware_fingerprint_is_stable_and_formatted():
    first = licensing.hardware_fingerprint()
    assert re.fullmatch(r"HW(-[0-9A-F]{4}){5}", first)
    licensing.hardware_fingerprint.cache_clear()
    assert licensing.hardware_fingerprint() == first
    assert licensing.hardware_components()


def test_valid_license_round_trip(keys):
    key = _issue(keys[0])
    data = licensing.check_license(key, date.today(), public_pem=keys[1])
    assert data.hotel_name == "Hôtel Test" and data.max_rooms == 30 and data.features == ALL
    assert data.days_left(date.today()) == 365 and data.type_label == "Abonnement"
    assert licensing.decode_license(key).license_id == data.license_id


def test_lifetime_and_trial_licenses(keys):
    lifetime = licensing.check_license(_issue(keys[0], license_type="lifetime"), date(2099, 1, 1), public_pem=keys[1])
    assert lifetime.expires_at is None and not lifetime.is_expired(date(2099, 1, 1))
    trial = licensing.decode_license(_issue(keys[0], license_type="trial", days=30))
    assert trial.expires_at == date.today() + timedelta(days=30)


@pytest.mark.parametrize("case,message", [
    ("other_machine", "émise pour la machine"),
    ("expired", "expirée"),
    ("tampered", "Signature invalide"),
    ("other_publisher", "Signature invalide"),
    ("garbage", "Format de clé invalide"),
])
def test_license_rejections(keys, case, message):
    key = _issue(keys[0])
    public = keys[1]
    today = date.today()
    if case == "other_machine":
        key = _issue(keys[0], hardware_id="HW-0000-0000-0000-0000-0000")
    elif case == "expired":
        key = _issue(keys[0], days=10, issued_at=today - timedelta(days=30))
    elif case == "tampered":
        prefix, body, signature = key.split(".")
        payload = json.loads(licensing._b64decode(body))
        payload["max_rooms"] = 500  # tentative d'élargir la licence
        key = ".".join([prefix, licensing._b64encode(json.dumps(payload).encode()), signature])
    elif case == "other_publisher":
        public = licensing.generate_keypair()[1]
    elif case == "garbage":
        key = "pas-une-licence"
    with pytest.raises(LicenseError, match=message):
        licensing.check_license(key, today, public_pem=public)


def test_issue_validation(keys):
    with pytest.raises(LicenseError, match="Module inconnu"):
        _issue(keys[0], features=["spa"])
    with pytest.raises(LicenseError, match="Type"):
        _issue(keys[0], license_type="gratuit")
    with pytest.raises(LicenseError, match="Durée"):
        _issue(keys[0], days=0)
    with pytest.raises(LicenseError, match="absente"):
        licensing.check_license(_issue(keys[0]), date.today(), public_pem=None) if not config.license_public_key_file.exists() \
            else (_ for _ in ()).throw(LicenseError("Clé publique absente"))


def test_feature_paths():
    assert licensing.feature_for_path("/pos/orders/1") == "pos"
    assert licensing.feature_for_path("/api/v1/ota/inventory") == "ota"
    assert licensing.feature_for_path("/housekeeping") == "housekeeping"
    assert licensing.feature_for_path("/reservations") is None


# ---------------------------------------------------------------- middleware et activation
def test_enforcement_activation_and_feature_gating(app, publisher, monkeypatch):
    monkeypatch.setattr(config, "license_enforced", True)
    admin = make_client(app, "admin")

    async def _clear():
        async with SessionLocal() as db:
            for row in (await db.scalars(select(LicenseInfo))).all():
                row.active = False
            await db.commit()
    asyncio.run(_clear())
    licensing.invalidate_cache()

    # Sans licence : interface redirigée vers /activate, API refusée en JSON, connexion toujours possible
    assert admin.get("/", follow_redirects=False).headers["location"] == "/activate"
    api = admin.get("/api/v1/ota/inventory")
    assert api.status_code == 403 and "Licence invalide" in api.json()["detail"]
    assert admin.get("/login").status_code in (200, 303)
    page = admin.get("/activate")
    assert page.status_code == 200 and licensing.hardware_fingerprint() in page.text

    # Clé invalide puis licence trop petite pour le nombre de chambres
    assert admin.form("/activate", {"license_key": "HPMS1.faux.faux"}).headers["location"] == "/activate"
    assert "Activation refusée" in admin.get("/activate").text
    admin.form("/activate", {"license_key": _issue(publisher[0], max_rooms=5)})
    assert "couvre 5 chambres" in admin.get("/activate").text

    # Licence valide sans le module POS
    response = admin.form("/activate", {"license_key": _issue(publisher[0], features=["housekeeping", "ota"])})
    assert response.headers["location"] == "/"
    assert admin.get("/").status_code == 200
    blocked = admin.get("/pos")
    assert blocked.status_code == 403 and "Module non licencié" in blocked.text
    assert admin.get("/housekeeping").status_code == 200
    assert "Points de vente" in admin.get("/activate").text

    # Licence complète : le POS redevient accessible ; la réception ne peut pas activer
    admin.form("/activate", {"license_key": _issue(publisher[0])})
    assert admin.get("/pos").status_code == 200
    reception = make_client(app, "reception")
    assert reception.form("/activate", {"license_key": _issue(publisher[0])}).status_code == 403

    # Limite de chambres : licence de 30 chambres, 30 actives -> ajout refusé
    admin.form("/admin/rooms", {"number": "999", "floor": "9", "room_type_id": "1"})
    assert "limite atteinte" in admin.get("/admin/rooms").text


def test_clock_rollback_is_detected(app, publisher):
    async def scenario():
        async with SessionLocal() as db:
            await licensing.activate(db, _issue(publisher[0]), None, datetime.now())
            await db.commit()
        async with SessionLocal() as db:
            ok = await licensing.evaluate(db, datetime.now())
            rolled_back = await licensing.evaluate(db, datetime.now() - timedelta(days=3))
            return ok, rolled_back
    ok, rolled_back = asyncio.run(scenario())
    assert ok.valid and ok.allows("pos")
    assert not rolled_back.valid and "Horloge" in rolled_back.reason


# ---------------------------------------------------------------- CLI éditeur
def test_publisher_cli(tmp_path):
    def run(*args):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_license.py"), *args],
                              capture_output=True, text=True, cwd=ROOT, check=False)

    public = tmp_path / "public.pem"
    assert run("keygen", "--out", str(tmp_path / "keys"), "--public-out", str(public)).returncode == 0
    private = tmp_path / "keys" / "license_private_key.pem"
    assert private.exists() and (private.stat().st_mode & 0o777) == 0o600
    assert run("keygen", "--out", str(tmp_path / "keys"), "--public-out", str(public)).returncode == 1
    hwid = run("hwid").stdout.strip()
    assert hwid == licensing.hardware_fingerprint()
    issued = run("issue", "--private-key", str(private), "--hotel", "Hôtel CLI", "--hardware-id", hwid,
                 "--type", "trial", "--features", "pos,ota")
    assert issued.returncode == 0, issued.stderr
    key = issued.stdout.strip()
    data = licensing.check_license(key, date.today(), public_pem=public.read_bytes())
    assert data.license_type == "trial" and data.features == ["ota", "pos"]
    inspected = run("inspect", key, "--public-key", str(public))
    assert "signature vérifiée" in inspected.stdout and "Hôtel CLI" in inspected.stdout
    assert run("inspect", "invalide").returncode == 1
    assert run("issue", "--private-key", str(private), "--hotel", "X", "--hardware-id", hwid,
               "--features", "spa").returncode == 1


def test_fingerprint_ignores_network_cards_and_external_commands(monkeypatch):
    """La MAC (VPN, Wi-Fi…) et les commandes externes ne doivent jamais faire varier l'ID d'une même machine."""
    def _forbidden(*_args, **_kwargs):
        raise AssertionError("source instable utilisée")

    monkeypatch.setattr(licensing.uuid, "getnode", _forbidden)
    monkeypatch.setattr(licensing, "_run", _forbidden)
    monkeypatch.setattr(licensing.platform, "system", lambda: "Linux")
    monkeypatch.setattr(licensing, "_read_text",
                        lambda path: "abc123" if path == "/etc/machine-id" else ("Dell Inc." if path.endswith("sys_vendor") else ""))
    assert licensing.hardware_components() == {"machine_id": "abc123", "sys_vendor": "dell inc."}


def test_windows_fingerprint_reads_registry_only(monkeypatch):
    def _forbidden(*_args, **_kwargs):
        raise AssertionError("source instable utilisée")

    monkeypatch.setattr(licensing.uuid, "getnode", _forbidden)
    monkeypatch.setattr(licensing, "_run", _forbidden)
    monkeypatch.setattr(licensing.platform, "system", lambda: "Windows")
    registry = {
        r"SOFTWARE\Microsoft\Cryptography": {"MachineGuid": "1234-abcd"},
        r"HARDWARE\DESCRIPTION\System\BIOS": {"SystemManufacturer": "hp", "BaseBoardProduct": "8a4f"},
    }
    monkeypatch.setattr(licensing, "_windows_registry",
                        lambda path, names: {k: v for k, v in registry[path].items() if k in names})
    assert licensing.hardware_components() == {"machine_guid": "1234-abcd", "SystemManufacturer": "hp",
                                               "BaseBoardProduct": "8a4f"}
