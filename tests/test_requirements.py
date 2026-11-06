"""Dépendances : une installation neuve (``pip install -r requirements.txt``) doit suffire à démarrer, y compris sous Windows."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Module importé → paquet pip qui le fournit
DISTRIBUTIONS = {
    "PIL": "pillow", "argon2": "argon2-cffi", "bidi": "python-bidi", "arabic_reshaper": "arabic-reshaper",
    "jinja2": "jinja2", "markupsafe": "jinja2", "pydantic_settings": "pydantic-settings", "sqlalchemy": "sqlalchemy",
    "starlette": "fastapi", "google": "google-auth", "googleapiclient": "google-api-python-client",
    "google_auth_oauthlib": "google-auth-oauthlib",
}
IMPORT = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)", re.MULTILINE)


def _packages() -> set[str]:
    names = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(re.split(r"[<>=!~;\[ ]", line, maxsplit=1)[0].lower())
    return names


def _third_party_imports() -> set[str]:
    modules = set()
    files = [ROOT / "main.py", *(ROOT / "app").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]
    for path in files:
        modules.update(IMPORT.findall(path.read_text(encoding="utf-8")))
    return {m for m in modules if m not in sys.stdlib_module_names and m not in {"app", "scripts", "__future__"}}


def test_every_third_party_import_is_in_requirements():
    packages = _packages()
    missing = sorted(m for m in _third_party_imports() if DISTRIBUTIONS.get(m, m).lower().replace("_", "-") not in packages)
    assert not missing, f"Modules importés absents de requirements.txt : {missing}"


def test_tzdata_required_for_windows_timezones():
    # Windows ne fournit pas la base IANA : sans tzdata, ZoneInfo("Africa/Algiers") échoue à l'installation.
    assert "tzdata" in _packages()


def test_timezone_loads_from_tzdata_package():
    from zoneinfo import ZoneInfo

    import tzdata

    assert tzdata.IANA_VERSION
    assert ZoneInfo("Africa/Algiers").key == "Africa/Algiers"
