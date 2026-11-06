"""Dépendances indispensables sous Windows (poste de réception)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _packages() -> set[str]:
    names = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(re.split(r"[<>=!~;\[ ]", line, maxsplit=1)[0].lower())
    return names


def test_tzdata_required_for_windows_timezones():
    # Windows ne fournit pas la base IANA : sans tzdata, ZoneInfo("Africa/Algiers") échoue à l'installation.
    assert "tzdata" in _packages()


def test_timezone_loads_from_tzdata_package():
    from zoneinfo import ZoneInfo

    import tzdata

    assert tzdata.IANA_VERSION
    assert ZoneInfo("Africa/Algiers").key == "Africa/Algiers"
