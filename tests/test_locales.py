"""Cohérence et complétude des catalogues de traduction app/locales/<lang>/*.json.

Ces tests servent de critique de complétude pour l'internationalisation :
- chaque fichier est un objet JSON plat de chaînes non vides ;
- les jeux de clés anglais et arabe sont symétriques ;
- un même texte source traduit dans plusieurs groupes porte la même traduction ;
- toute clé ``_("…")`` des templates existe dans la fusion de chaque langue ;
- les valeurs des dictionnaires de libellés (modèles, licences, paramètres) sont traduites.
"""
import json
import re
from pathlib import Path

import pytest

from app.models.backup import BACKUP_DESTINATIONS, BACKUP_STATUSES
from app.models.booking import CHANNELS, RES_STATUSES
from app.models.customer import GENDERS, GUEST_STATUSES, ID_TYPES
from app.models.invoice import ITEM_KINDS, PAYMENT_KINDS, PAYMENT_METHODS
from app.models.ota import OTA_CHANNELS, OTA_PERMISSIONS
from app.models.pos import ORDER_STATUSES, OUTLETS, STOCK_REASONS
from app.models.room import ROOM_STATUSES
from app.models.user import ROLES
from app.services.i18n import DEFAULT_LANG, LANGS, LOCALES_DIR, catalog, reload_catalogs
from app.services.licensing import FEATURES, LICENSE_TYPES
from app.services.settings import MODULES, HotelSettings

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "app" / "templates"
TRANSLATED_LANGS = sorted(code for code in LANGS if code != DEFAULT_LANG)

# _("…") / _('…') dans les templates ; le « _ » ne doit pas être la fin d'un identifiant (ex. ui._(…) n'existe pas,
# mais foo_(…) ne doit pas être compté). Les guillemets échappés à l'intérieur de la chaîne sont acceptés.
TEMPLATE_KEY_RE = re.compile(r"""(?<![\w.])_\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")
# Commentaires Jinja {# … #} : jamais rendus, donc exclus de l'extraction.
JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)


def _locale_files() -> list[Path]:
    files = sorted(LOCALES_DIR.glob("*/*.json"))
    assert files, f"aucun fichier de traduction trouvé sous {LOCALES_DIR}"
    return files


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _language_files(lang: str) -> list[Path]:
    return sorted((LOCALES_DIR / lang).glob("*.json"))


def _unescape(value: str) -> str:
    return value.replace("\\'", "'").replace('\\"', '"')


def template_keys() -> dict[str, set[str]]:
    """Clés ``_("…")`` par template (chemin relatif → ensemble de textes sources)."""
    found: dict[str, set[str]] = {}
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        text = JINJA_COMMENT_RE.sub("", path.read_text(encoding="utf-8"))
        keys ={_unescape(a or b) for a, b in TEMPLATE_KEY_RE.findall(text)}
        keys.discard("")
        if keys:
            found[path.relative_to(TEMPLATES_DIR).as_posix()] = keys
    return found


def label_dictionaries() -> dict[str, list[str]]:
    """Valeurs françaises des dictionnaires de libellés devant être traduites."""
    groups: dict[str, list[str]] = {
        "ROOM_STATUSES": list(ROOM_STATUSES.values()),
        "RES_STATUSES": list(RES_STATUSES.values()),
        "PAYMENT_METHODS": list(PAYMENT_METHODS.values()),
        "PAYMENT_KINDS": list(PAYMENT_KINDS.values()),
        "ITEM_KINDS": list(ITEM_KINDS.values()),
        "ID_TYPES": list(ID_TYPES.values()),
        "GUEST_STATUSES": list(GUEST_STATUSES.values()),
        "GENDERS": list(GENDERS.values()),
        "OUTLETS": list(OUTLETS.values()),
        "ORDER_STATUSES": list(ORDER_STATUSES.values()),
        "STOCK_REASONS": list(STOCK_REASONS.values()),
        "ROLES": list(ROLES.values()),
        "CHANNELS": list(CHANNELS),
        "OTA_CHANNELS": list(OTA_CHANNELS.values()),
        "OTA_PERMISSIONS": list(OTA_PERMISSIONS.values()),
        "BACKUP_DESTINATIONS": list(BACKUP_DESTINATIONS.values()),
        "BACKUP_STATUSES": list(BACKUP_STATUSES.values()),
        "FEATURES": list(FEATURES.values()),
        "LICENSE_TYPES": list(LICENSE_TYPES.values()),
        "MODULES": list(MODULES.values()),
    }
    titles, helps, group_names = [], [], []
    for info in HotelSettings.model_fields.values():
        extra = info.json_schema_extra or {}
        titles.append(info.title)
        if extra.get("help"):
            helps.append(extra["help"])
        group_names.append(extra.get("group", "Divers"))
    groups["HotelSettings.title"] = titles
    groups["HotelSettings.help"] = helps
    groups["HotelSettings.group"] = sorted(set(group_names))
    return groups


@pytest.fixture(scope="module", autouse=True)
def fresh_catalogs():
    """Recharge les fichiers de traduction : les tests lisent l'état réel du disque, pas un cache antérieur."""
    reload_catalogs()
    yield
    reload_catalogs()


# ---------------------------------------------------------------- (a) structure des fichiers
@pytest.mark.parametrize("path", _locale_files(), ids=lambda p: p.relative_to(LOCALES_DIR).as_posix())
def test_locale_file_is_flat_object_of_non_empty_strings(path: Path):
    data = _load(path)
    assert isinstance(data, dict), f"{path.name} : objet JSON attendu"
    assert data, f"{path.name} : fichier vide"
    bad = {key: value for key, value in data.items()
           if not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip()}
    assert not bad, f"{path.relative_to(LOCALES_DIR)} : clés ou valeurs vides / non textuelles : {bad}"


def test_locale_directories_match_supported_languages():
    present = sorted(p.name for p in LOCALES_DIR.iterdir() if p.is_dir() and not p.name.startswith("_"))
    unknown = [name for name in present if name not in LANGS]
    assert not unknown, f"dossiers de langue inconnus : {unknown}"
    for lang in TRANSLATED_LANGS:
        assert _language_files(lang), f"aucun fichier de traduction pour « {lang} »"


# ---------------------------------------------------------------- (b) symétrie en / ar
def test_language_catalogs_have_same_key_sets():
    merged = {lang: set(catalog(lang)) for lang in TRANSLATED_LANGS}
    reference = TRANSLATED_LANGS[0]
    for lang in TRANSLATED_LANGS[1:]:
        only_ref = sorted(merged[reference] - merged[lang])
        only_lang = sorted(merged[lang] - merged[reference])
        assert not only_ref, f"clés présentes en « {reference} » mais absentes en « {lang} » : {only_ref}"
        assert not only_lang, f"clés présentes en « {lang} » mais absentes en « {reference} » : {only_lang}"


def test_same_group_files_exist_in_every_language():
    names = {lang: sorted(p.name for p in _language_files(lang)) for lang in TRANSLATED_LANGS}
    reference = TRANSLATED_LANGS[0]
    for lang in TRANSLATED_LANGS[1:]:
        assert names[lang] == names[reference], f"groupes différents entre {reference} et {lang} : {names}"


# ---------------------------------------------------------------- cohérence inter-groupes
@pytest.mark.parametrize("lang", TRANSLATED_LANGS)
def test_same_source_text_has_same_translation_across_groups(lang: str):
    seen: dict[str, tuple[str, str]] = {}
    conflicts: list[str] = []
    for path in _language_files(lang):
        for key, value in _load(path).items():
            if key in seen and seen[key][0] != value:
                conflicts.append(f"« {key} » : {seen[key][1]} = {seen[key][0]!r} ≠ {path.name} = {value!r}")
            seen.setdefault(key, (value, path.name))
    assert not conflicts, f"traductions divergentes en « {lang} » :\n" + "\n".join(conflicts)


# ---------------------------------------------------------------- (c) complétude des templates
def test_template_keys_are_extracted():
    keys = template_keys()
    assert keys, "aucune clé _(\"…\") trouvée dans les templates : l'extraction est cassée ou rien n'est traduit"


@pytest.mark.parametrize("lang", TRANSLATED_LANGS)
def test_every_template_key_is_translated(lang: str):
    merged = catalog(lang)
    missing: dict[str, list[str]] = {}
    for template, keys in template_keys().items():
        absent = sorted(key for key in keys if key not in merged)
        if absent:
            missing[template] = absent
    total = sum(len(v) for v in missing.values())
    detail = "\n".join(f"  {template}: {keys}" for template, keys in missing.items())
    assert not missing, f"{total} clé(s) de template sans traduction « {lang} » :\n{detail}"


# ---------------------------------------------------------------- (d) dictionnaires de libellés
@pytest.mark.parametrize("lang", TRANSLATED_LANGS)
def test_label_dictionaries_are_translated(lang: str):
    merged = catalog(lang)
    missing = {group: sorted(v for v in values if v not in merged)
               for group, values in label_dictionaries().items()}
    missing = {group: values for group, values in missing.items() if values}
    assert not missing, f"libellés non traduits en « {lang} » : {missing}"


def test_label_dictionaries_contain_no_empty_value():
    for group, values in label_dictionaries().items():
        assert all(isinstance(v, str) and v for v in values), f"{group} contient une valeur vide"
