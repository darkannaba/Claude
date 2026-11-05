"""Internationalisation FR / AR / EN.

Principe : le code et les templates contiennent des TEXTES SOURCES FRANÇAIS ; ``t(texte, lang)`` renvoie
la traduction demandée, ou le texte source si la clé manque (``"fr"`` = identité).

Les traductions vivent dans ``app/locales/<lang>/<groupe>.json`` : objets JSON plats
``{"Texte source français": "Traduction"}``. Tous les fichiers d'un dossier de langue sont fusionnés au
premier appel (puis mis en cache) ; un même texte présent dans deux groupes doit porter la même traduction.

La langue courante de la requête est portée par une ``ContextVar`` (sûre en asynchrone) : ``activate(request)``
la détecte (session, cookie, en-tête Accept-Language, sinon français) et la positionne ; ``render()`` et
``render_error()`` l'appellent systématiquement, et le middleware optionnel ``app/middlewares/language.py``
permet de la poser dès l'entrée de la requête.
"""
from __future__ import annotations

import json
import threading
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

LANGS: dict[str, str] = {"fr": "Français", "ar": "العربية", "en": "English"}
DEFAULT_LANG = "fr"
RTL_LANGS = frozenset({"ar"})

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"

LANG_SESSION_KEY = "lang"
LANG_COOKIE = "lang"
LANG_COOKIE_MAX_AGE = 365 * 24 * 3600  # un an

# Libellés utilisés par app/static/js/app.js (séries de graphiques, confirmations) : injectés dans chaque page
# sous la forme {texte source: traduction} via window.I18N (voir render() et layout/base.html).
JS_LABELS: tuple[str, ...] = (
    "Hébergement",
    "Services & POS",
    "Taxe de séjour",
    "Taux d'occupation (%)",
    "ADR (DA)",
    "RevPAR (DA)",
    "TO %",
    "Confirmer cette action ?",
    "Annuler cette réservation ?",
    "Déclarer un no-show ?",
    "Clôturer le séjour ? Les nuits seront figées et la facture définitive émise.",
    "Clôturer cette journée ? Les nuitées seront figées.",
    "Clôturer toutes les journées en attente ?",
    "Retirer cet accompagnateur ?",
    "Supprimer ce tarif ?",
    "Annuler cette écriture ?",
    "Annuler cette addition ?",
    "Révoquer définitivement cette clé ?",
)

_current_lang: ContextVar[str] = ContextVar("current_lang", default=DEFAULT_LANG)
_catalogs: dict[str, dict[str, str]] | None = None
_lock = threading.Lock()


# ---------------------------------------------------------------- codes de langue
def normalize_lang(value: Any) -> str | None:
    """Ramène « en-US », « ar_DZ », « FR » à un code pris en charge ; None si la langue est inconnue."""
    if not isinstance(value, str):
        return None
    code = value.strip().lower().replace("_", "-").split("-", 1)[0]
    return code if code in LANGS else None


def is_rtl(lang: str | None = None) -> bool:
    """Vrai pour les langues écrites de droite à gauche (arabe)."""
    return (normalize_lang(lang) if lang is not None else get_lang()) in RTL_LANGS


def direction(lang: str | None = None) -> str:
    """Attribut HTML ``dir`` : « rtl » pour l'arabe, sinon « ltr »."""
    return "rtl" if is_rtl(lang) else "ltr"


# ---------------------------------------------------------------- catalogues
def _load_file(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Fichier de traduction invalide (objet JSON attendu) : {path}")
    entries: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str) and key and value:
            entries[key] = value
    return entries


def _load_language(lang_dir: Path) -> dict[str, str]:
    """Fusionne tous les ``*.json`` d'un dossier de langue (ordre alphabétique, le dernier l'emporte)."""
    catalog: dict[str, str] = {}
    if not lang_dir.is_dir():
        return catalog
    for path in sorted(lang_dir.glob("*.json")):
        catalog.update(_load_file(path))
    return catalog


def load_catalogs(force: bool = False) -> dict[str, dict[str, str]]:
    """Charge (une seule fois) les catalogues de toutes les langues autres que le français."""
    global _catalogs
    if _catalogs is None or force:
        with _lock:
            if _catalogs is None or force:
                _catalogs = {lang: _load_language(LOCALES_DIR / lang) for lang in LANGS if lang != DEFAULT_LANG}
    return _catalogs


def reload_catalogs() -> dict[str, dict[str, str]]:
    """Recharge les fichiers de traduction (utile en développement et dans les tests)."""
    return load_catalogs(force=True)


def catalog(lang: str) -> dict[str, str]:
    """Catalogue fusionné d'une langue (vide pour le français et les langues inconnues)."""
    return load_catalogs().get(normalize_lang(lang) or DEFAULT_LANG, {})


def t(text: str, lang: str | None = None) -> str:
    """Traduit un texte source français vers ``lang`` (langue courante si None) ; repli : le texte source."""
    if not isinstance(text, str) or not text:
        return text
    code = get_lang() if lang is None else (normalize_lang(lang) or DEFAULT_LANG)
    if code == DEFAULT_LANG:
        return text
    return catalog(code).get(text) or text


def js_labels(lang: str | None = None) -> dict[str, str]:
    """Dictionnaire {texte source: traduction} des libellés nécessaires au JavaScript (window.I18N)."""
    return {key: t(key, lang) for key in JS_LABELS}


# ---------------------------------------------------------------- langue courante (contexte asynchrone)
def get_lang() -> str:
    """Langue de la requête en cours (français par défaut)."""
    return _current_lang.get()


def set_lang(lang: str | None) -> Token:
    """Positionne la langue courante ; renvoie le jeton permettant de la rétablir via ``reset_lang``."""
    return _current_lang.set(normalize_lang(lang) or DEFAULT_LANG)


def reset_lang(token: Token) -> None:
    _current_lang.reset(token)


# ---------------------------------------------------------------- détection
def parse_accept_language(header: str | None) -> str | None:
    """Première langue prise en charge de l'en-tête Accept-Language, par qualité décroissante."""
    if not header:
        return None
    candidates: list[tuple[float, int, str]] = []
    for index, part in enumerate(header.split(",")):
        piece = part.strip()
        if not piece:
            continue
        tag, _, params = piece.partition(";")
        quality = 1.0
        for param in params.split(";"):
            name, _, raw = param.strip().partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(raw.strip())
                except ValueError:
                    quality = 0.0
        code = normalize_lang(tag)
        if code and quality > 0:
            candidates.append((-quality, index, code))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


def _session_of(request) -> dict:
    scope = getattr(request, "scope", None) or {}
    session = scope.get("session")
    return session if isinstance(session, dict) else {}


def detect_lang(request) -> str:
    """Langue d'une requête : session « lang », sinon cookie « lang », sinon Accept-Language, sinon français."""
    for candidate in (_session_of(request).get(LANG_SESSION_KEY), request.cookies.get(LANG_COOKIE)):
        code = normalize_lang(candidate)
        if code:
            return code
    return parse_accept_language(request.headers.get("accept-language")) or DEFAULT_LANG


def activate(request) -> str:
    """Détecte la langue de la requête, la pose dans le contexte courant et dans ``request.state.lang``."""
    lang = detect_lang(request)
    set_lang(lang)
    try:
        request.state.lang = lang
    except AttributeError:
        pass
    return lang
