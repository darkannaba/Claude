"""Thématisation dynamique (white-label) : palette, typographie, densité, logos, textes des documents.

Principe : la charte est stockée dans ``theme_settings`` (une seule ligne ``id = 1``) ; ``build_css`` en
déduit une feuille de variables CSS servie par ``GET /static/css/theme-custom.css`` (app/routers/theme.py).
Tailwind (compilé localement) référence ces variables : ``brand-*`` → ``--brand-50…950`` et ``accent-*`` →
``--accent-50…950`` sous forme de triplets « r g b » (``rgb(var(--brand-600) / <alpha-value>)``), ce qui
permet de changer la charte sans recompiler la feuille Tailwind. Les valeurs par défaut de ces variables
(charte teal d'origine) sont déclarées dans ``app/static/src/input.css`` : l'interface reste correcte même
si la feuille dynamique n'est pas chargée.

Fichiers de marque (logo, logo pour le mode sombre, favicon) : téléversés, contrôlés et normalisés avec
Pillow, puis rangés sous ``data/media/branding`` (inclus automatiquement dans les sauvegardes). Les SVG ne
sont acceptés qu'après une vérification stricte (aucun script, gestionnaire d'événement, lien
``javascript:``, contenu étranger, entité XML ni ressource externe).

Aucune police n'est téléchargée depuis Internet : les piles CSS se replient sur les polices du système, et
les fichiers ``.woff2`` éventuellement déposés dans ``app/static/fonts/ui/`` (``inter.woff2`` police
variable, ou ``inter-400.woff2``, ``inter-700.woff2``…) sont déclarés automatiquement par ``@font-face``.

Lecture et écriture :
- ``load_theme`` (pages, feuille CSS, fichiers de marque) renvoie un instantané détaché mis en cache dans le
  processus, sans jamais écrire en base (ligne absente : valeurs par défaut en mémoire). Le cache est invalidé
  dès qu'une transaction modifie ``theme_settings`` (écouteurs de session ci-dessous), à chaque enregistrement
  de la charte et après une restauration de sauvegarde (``invalidate_theme_cache``).
- Les modifications (``update_*``, ``reset_colors``, ``save_image``, ``delete_image``, ``reconcile_branding``)
  sont sérialisées par un verrou asyncio pris jusqu'à la fin de la transaction (application mono-processus) ;
  la version est incrémentée en SQL et les fichiers de marque non référencés sont purgés après le commit.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import re
import secrets
import unicodedata
import warnings
import weakref
from collections.abc import Mapping
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import event, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.config import config
from app.database import SessionLocal
from app.models.theme import THEME_FONTS, ThemeSettings
from app.services.common import BusinessError

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
UI_FONTS_DIR = STATIC_DIR / "fonts" / "ui"

THEME_ID = 1
CSS_URL = "/static/css/theme-custom.css"

# ---------------------------------------------------------------- valeurs par défaut (charte d'origine)
COLOR_FIELDS: tuple[str, ...] = (
    "primary_color", "secondary_color", "card_color",
    "status_available_color", "status_occupied_color", "status_dirty_color", "status_maintenance_color",
)
COLOR_LABELS: dict[str, str] = {
    "primary_color": "Couleur primaire",
    "secondary_color": "Couleur secondaire / accentuation",
    "card_color": "Couleur des cartes",
    "status_available_color": "Statut : libre",
    "status_occupied_color": "Statut : occupée",
    "status_dirty_color": "Statut : ménage",
    "status_maintenance_color": "Statut : maintenance",
}
COLOR_HELPS: dict[str, str] = {
    "primary_color": "Boutons principaux, liens, sélection active.",
    "secondary_color": "Éléments d'accentuation et graphiques.",
    "card_color": "Fond des cartes en mode clair (une teinte claire garantit la lisibilité).",
    "status_available_color": "Chambres libres et propres.",
    "status_occupied_color": "Chambres occupées.",
    "status_dirty_color": "Chambres à nettoyer.",
    "status_maintenance_color": "Chambres hors service.",
}

DEFAULT_THEME: dict[str, Any] = {
    "primary_color": "#0d9488",
    "secondary_color": "#0284c7",
    "card_color": "#ffffff",
    "status_available_color": "#10b981",
    "status_occupied_color": "#0ea5e9",
    "status_dirty_color": "#f59e0b",
    "status_maintenance_color": "#f43f5e",
    "font_family": "inter",
    "density": "comfortable",
    "logo_file": "",
    "logo_dark_file": "",
    "favicon_file": "",
    "slogan": "",
    "slogan_ar": "",
    "document_header": "",
    "document_footer": "",
    "show_logo_on_documents": True,
}

SHADES: tuple[int, ...] = (50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950)
# Nuance de base = 600 (couleur des boutons principaux) ; < 600 : mélange vers le blanc, > 600 : vers le noir.
SHADE_MIX: dict[int, tuple[str, float]] = {
    50: ("white", 0.95), 100: ("white", 0.88), 200: ("white", 0.75), 300: ("white", 0.58),
    400: ("white", 0.36), 500: ("white", 0.16), 600: ("white", 0.0),
    700: ("black", 0.18), 800: ("black", 0.34), 900: ("black", 0.48), 950: ("black", 0.68),
}
# Palettes exactes de la charte d'origine (Tailwind teal / sky) : l'apparence par défaut reste identique.
KNOWN_PALETTES: dict[str, dict[int, str]] = {
    "#0d9488": {50: "#f0fdfa", 100: "#ccfbf1", 200: "#99f6e4", 300: "#5eead4", 400: "#2dd4bf", 500: "#14b8a6",
                600: "#0d9488", 700: "#0f766e", 800: "#115e59", 900: "#134e4a", 950: "#042f2e"},
    "#0284c7": {50: "#f0f9ff", 100: "#e0f2fe", 200: "#bae6fd", 300: "#7dd3fc", 400: "#38bdf8", 500: "#0ea5e9",
                600: "#0284c7", 700: "#0369a1", 800: "#075985", 900: "#0c4a6e", 950: "#082f49"},
}

DARK_TEXT = "#0f172a"   # slate-900
LIGHT_TEXT = "#ffffff"
DARK_CARD = "#0f172a"   # slate-900 : cartes en mode sombre (teintées par la couleur primaire)
DARK_SURFACE = "#020617"  # slate-950
LIGHT_SURFACE = "#f1f5f9"  # slate-100
MIN_WHITE_CONTRAST = 3.0  # seuil WCAG 2.x pour les composants d'interface et le texte en gras
MIN_CARD_LUMINANCE = 0.6  # en dessous, le texte foncé des cartes deviendrait illisible
MIN_TEXT_CONTRAST = 4.5  # seuil WCAG 2.x (AA) pour le texte courant : liens, texte des badges
PRIMARY_LIGHT_WARNING = ("Couleur primaire très claire : le texte des boutons passe en foncé et les liens sont "
                         "assombris automatiquement pour rester lisibles.")

# ---------------------------------------------------------------- typographie et densité
ARABIC_STACK = '"Noto Sans Arabic", "Segoe UI", Tahoma, "DejaVu Sans", sans-serif'
FONTS: dict[str, dict[str, str]] = {
    "inter": {"label": THEME_FONTS["inter"], "family": "Inter",
              "stack": 'Inter, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans Arabic", Tahoma, sans-serif'},
    "roboto": {"label": THEME_FONTS["roboto"], "family": "Roboto",
               "stack": 'Roboto, "Segoe UI", "Helvetica Neue", Arial, "Noto Sans Arabic", Tahoma, sans-serif'},
    "tajawal": {"label": THEME_FONTS["tajawal"], "family": "Tajawal",
                "stack": 'Tajawal, "Segoe UI", Tahoma, Arial, "Noto Sans Arabic", sans-serif'},
    "system": {"label": THEME_FONTS["system"], "family": "",
               "stack": 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans Arabic", '
                        'Tahoma, sans-serif'},
}
FONT_WEIGHT_RE = re.compile(r"^(?P<key>[a-z]+)(?:-(?P<weight>[1-9]00))?\.woff2$")

DENSITIES: dict[str, dict[str, str]] = {
    "comfortable": {
        "btn-y": "0.5rem", "btn-x": "0.875rem", "input-y": "0.5rem", "input-x": "0.75rem",
        "card-p": "1.25rem", "cell-y": "0.625rem", "cell-x": "0.75rem", "gap": "1.25rem",
        "btn-sm-y": "0.375rem", "btn-sm-x": "0.625rem", "title-gap": "1rem",
    },
    "compact": {
        "btn-y": "0.3125rem", "btn-x": "0.625rem", "input-y": "0.3125rem", "input-x": "0.5rem",
        "card-p": "0.875rem", "cell-y": "0.375rem", "cell-x": "0.5rem", "gap": "0.75rem",
        "btn-sm-y": "0.25rem", "btn-sm-x": "0.5rem", "title-gap": "0.625rem",
    },
}

# ---------------------------------------------------------------- fichiers de marque
KIND_FIELDS: dict[str, str] = {"logo": "logo_file", "logo-dark": "logo_dark_file", "favicon": "favicon_file"}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000        # protection contre les « decompression bombs »
MAX_IMAGE_SIDE = 8000
LOGO_MAX_SIZE = (1024, 512)          # largeur max, hauteur max (ratio conservé)
FAVICON_SIZES = (64, 32)
RASTER_FORMATS = {"PNG": "PNG", "WEBP": "WEBP", "JPEG": "JPEG", "MPO": "JPEG"}
MEDIA_TYPES = {".png": "image/png", ".svg": "image/svg+xml"}
FILE_NAME_RE = re.compile(r"^(logo|logo-dark|favicon)-[0-9a-f]{12}(-32)?\.(png|svg)$")

SVG_FORBIDDEN_ELEMENTS = frozenset({
    "script", "foreignobject", "iframe", "embed", "object", "handler", "listener", "audio", "video",
    "frame", "frameset", "applet", "base", "link", "meta", "html", "body",
})
SVG_DATA_IMAGE_RE = re.compile(r"^data:image/(png|jpeg|jpg|gif|webp);base64,[a-z0-9+/=\s]+$", re.IGNORECASE)
CSS_URL_RE = re.compile(r"url\(\s*['\"]?\s*(?!#)", re.IGNORECASE)
# Instruction de traitement autre que la déclaration XML initiale (<?xml-stylesheet …?>, <?xml-model …?>…)
SVG_PROCESSING_INSTRUCTION_RE = re.compile(r"<\?(?!xml\s)", re.IGNORECASE)
SVG_XML_DECLARATION_RE = re.compile(r"^\s*<\?xml\s[^>]*\?>", re.IGNORECASE)
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
ElementTree.register_namespace("xlink", "http://www.w3.org/1999/xlink")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x20\x7f]+")
# Éléments d'animation SMIL : ils peuvent réécrire n'importe quel attribut (href d'un lien, gestionnaire…).
SVG_ANIMATION_ELEMENTS = frozenset({
    "set", "animate", "animatemotion", "animatetransform", "animatecolor", "discard",
})
SVG_FRAGMENT_RE = re.compile(r"^#[A-Za-z_][\w.\-]*$")
# Éléments dont le href peut désigner une image embarquée (data:image/…) ; ailleurs, seul un fragment est admis.
SVG_IMAGE_ELEMENTS = frozenset({"image", "feimage"})

TEXT_LIMITS = {"slogan": 200, "slogan_ar": 200, "document_header": 1000, "document_footer": 1000}
# Nombre de lignes maximal des textes de documents : le pied est répété au bas de chaque page A4 et réduit
# d'autant la zone utile (app/services/pdf.py plafonne en outre sa hauteur dessinée).
LINE_LIMITS = {"slogan": 1, "slogan_ar": 1, "document_header": 10, "document_footer": 8}
# Paramètres d'identité de HotelSettings modifiables depuis la page thème (aucune duplication en base).
IDENTITY_FIELDS: tuple[str, ...] = ("hotel_name", "hotel_name_ar", "hotel_nif", "hotel_nis", "hotel_rc", "hotel_ai")


# ================================================================= chargement
async def get_theme(db: AsyncSession, refresh: bool = False) -> ThemeSettings:
    """Ligne de charte attachée à ``db`` (écritures) ; crée la ligne par défaut si elle est absente.

    Réservé aux chemins d'écriture : les lectures (pages, CSS, fichiers de marque) passent par ``load_theme``,
    qui n'écrit jamais. Un appelant qui veut conserver la ligne créée doit valider (``commit``) sa session.
    ``refresh`` relit la ligne en base même si la session en détient déjà une copie.
    """
    theme = await db.get(ThemeSettings, THEME_ID, populate_existing=refresh)
    if theme is None:
        await db.execute(sqlite_insert(ThemeSettings).values(
            id=THEME_ID, updated_at=datetime.now(), version=1, **DEFAULT_THEME,
        ).on_conflict_do_nothing(index_elements=["id"]))
        _mark_changed(db.sync_session)
        theme = await db.get(ThemeSettings, THEME_ID, populate_existing=True)
    return theme


async def ensure_theme() -> None:
    """Crée la ligne de charte par défaut dans une session dédiée (appelé au démarrage)."""
    async with SessionLocal() as db:
        await get_theme(db)
        await db.commit()


# ================================================================= cache de la charte (niveau processus)
_THEME_CHANGED = "theme_settings_changed"  # clé de Session.info : la transaction en cours a modifié la charte
_THEME_CHANGE = "theme_settings_change"    # clé de Session.info : modification en cours (verrou détenu)
_cache: dict[str, Any] = {"theme": None, "generation": 0, "css": None}


def invalidate_theme_cache() -> None:
    """Oublie l'instantané de la charte (et le relevé des polices locales) : relu à la prochaine requête."""
    _cache["theme"] = None
    _cache["css"] = None
    _cache["generation"] += 1
    _font_faces_in.cache_clear()


def _mark_changed(session: Session) -> None:
    session.info[_THEME_CHANGED] = True
    invalidate_theme_cache()


def default_theme() -> ThemeSettings:
    """Charte d'origine en mémoire (non rattachée à une session), utilisée tant que la ligne n'existe pas."""
    return ThemeSettings(id=THEME_ID, version=1, updated_at=None, **DEFAULT_THEME)


def _snapshot(row: Mapping[str, Any]) -> ThemeSettings:
    """Copie détachée d'une ligne ``theme_settings`` : jamais ajoutée à une session, partagée en lecture seule."""
    return ThemeSettings(**{column.key: row[column.name] for column in ThemeSettings.__table__.columns})


async def load_theme(db: AsyncSession) -> ThemeSettings:
    """Charte courante pour l'affichage : instantané en cache, aucune écriture en base.

    Ligne absente (ex. restauration d'une sauvegarde antérieure à la migration 0004) : valeurs par défaut en
    mémoire ; la ligne est créée par ``ensure_theme`` au démarrage ou par le premier enregistrement.
    """
    cached = _cache["theme"]
    if cached is not None:
        return cached
    generation = _cache["generation"]
    table = ThemeSettings.__table__
    row = (await db.execute(select(table).where(table.c.id == THEME_ID))).mappings().first()
    theme = _snapshot(row) if row is not None else default_theme()
    # Pas de mise en cache si la charte a changé pendant la lecture, ni si cette session porte elle-même une
    # modification non validée (la valeur lue pourrait encore être annulée).
    if generation == _cache["generation"] and not db.sync_session.info.get(_THEME_CHANGED):
        _cache["theme"] = theme
    return theme


def theme_css(theme: ThemeSettings) -> tuple[str, str]:
    """(feuille CSS, ETag) de ``theme`` ; mis en cache pour l'instantané courant."""
    cached = _cache["css"]
    if cached is not None and cached[0] is theme:
        return cached[1], cached[2]
    css = build_css(theme)
    etag = css_etag(theme, css)
    if theme is _cache["theme"]:
        _cache["css"] = (theme, css, etag)
    return css, etag


@event.listens_for(Session, "after_flush")
def _theme_flushed(session: Session, _context) -> None:
    """Toute écriture ORM de la charte (y compris hors de ce module) invalide le cache."""
    if any(isinstance(obj, ThemeSettings) for obj in (*session.new, *session.dirty, *session.deleted)):
        _mark_changed(session)


@event.listens_for(Session, "before_commit")
def _theme_before_commit(session: Session) -> None:
    """Noms des fichiers de marque qui seront référencés après le commit (relevés avant l'expiration éventuelle
    des attributs)."""
    change = session.info.get(_THEME_CHANGE)
    if change is not None:
        values = change["theme"].__dict__
        change["referenced"] = {values.get(field) or "" for field in KIND_FIELDS.values()} \
            if all(field in values for field in KIND_FIELDS.values()) else None


@event.listens_for(Session, "after_commit")
def _theme_committed(session: Session) -> None:
    change = session.info.get(_THEME_CHANGE)
    if change is not None:
        change["committed"] = True


@event.listens_for(Session, "after_transaction_end")
def _theme_transaction_end(session: Session, transaction) -> None:
    """Fin de la transaction racine : invalidation du cache, nettoyage des fichiers, libération du verrou."""
    if transaction.parent is not None:
        return
    if session.info.pop(_THEME_CHANGED, False):
        invalidate_theme_cache()
    change = session.info.get(_THEME_CHANGE)
    if change is None or change["transaction"] is not transaction:
        return
    del session.info[_THEME_CHANGE]
    try:
        if change["committed"]:
            if change.get("referenced") is not None:
                _purge_unreferenced(change["referenced"])
        else:
            for name in change["rollback_files"]:
                _remove_files(name)
    except OSError:  # nettoyage au mieux : un fichier verrouillé ne doit pas masquer l'issue réelle
        pass
    finally:
        change["lock"].release()


# ================================================================= modifications sérialisées
_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _theme_lock() -> asyncio.Lock:
    """Verrou des modifications de la charte, propre à la boucle d'événements (application mono-processus)."""
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    return lock


async def _begin_change(db: AsyncSession) -> ThemeSettings:
    """Ouvre une modification de la charte : prend le verrou jusqu'à la fin de la transaction de ``db`` et relit
    la ligne en base (valeurs à jour des fichiers de marque). Réentrant pour une même session."""
    session = db.sync_session
    change = session.info.get(_THEME_CHANGE)
    if change is not None:
        return change["theme"]
    lock = _theme_lock()
    await lock.acquire()
    try:
        await db.flush()  # aucune modification en attente ne doit être écrasée par la relecture
        await db.connection()  # ouvre la transaction dont la fin libère le verrou
        transaction = session.get_transaction()
        theme = await get_theme(db, refresh=True)
    except BaseException:
        lock.release()
        raise
    session.info[_THEME_CHANGE] = {"lock": lock, "transaction": transaction, "theme": theme,
                                   "rollback_files": [], "committed": False, "referenced": None}
    return theme


async def _touch(db: AsyncSession, theme: ThemeSettings) -> None:
    """Nouvelle version de la charte, incrémentée en SQL (jamais deux contenus pour une même URL ?v=N)."""
    await db.flush()
    table = ThemeSettings.__table__
    now = datetime.now()
    version = (await db.execute(
        update(table).where(table.c.id == THEME_ID)
        .values(version=table.c.version + 1, updated_at=now).returning(table.c.version)
    )).scalar_one()
    set_committed_value(theme, "version", version)
    set_committed_value(theme, "updated_at", now)
    _mark_changed(db.sync_session)


# ================================================================= couleurs
HEX_RE = re.compile(r"^#[0-9a-f]{6}$")


def normalize_hex(value: object) -> str:
    """« #RRGGBB » → « #rrggbb » ; tout autre format (#abc, nom de couleur, #zzzzzz) est refusé."""
    text = str(value or "").strip().lower()
    if not HEX_RE.match(text):
        raise BusinessError("Couleur invalide : utilisez le format #rrggbb (ex. #0d9488).")
    return text


def _safe_hex(value: object, default: str) -> str:
    try:
        return normalize_hex(value)
    except BusinessError:
        return default


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = normalize_hex(value)
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{max(0, min(255, int(round(c)))):02x}" for c in rgb)


def mix(color: str, other: str, weight: float) -> str:
    """Mélange ``color`` avec ``other`` (``weight`` = part de ``other``, entre 0 et 1)."""
    a, b = hex_to_rgb(color), hex_to_rgb(other)
    return rgb_to_hex(tuple(ca + (cb - ca) * weight for ca, cb in zip(a, b)))


def palette(value: str) -> dict[int, str]:
    """Nuances 50…950 (« #rrggbb ») dérivées d'une couleur de base placée en 600."""
    base = normalize_hex(value)
    if base in KNOWN_PALETTES:
        return dict(KNOWN_PALETTES[base])
    shades: dict[int, str] = {}
    for shade, (towards, weight) in SHADE_MIX.items():
        shades[shade] = mix(base, "#ffffff" if towards == "white" else "#000000", weight)
    return shades


def rgb_triplet(value: str) -> str:
    r, g, b = hex_to_rgb(value)
    return f"{r} {g} {b}"


def relative_luminance(value: str) -> float:
    """Luminance relative WCAG 2.x (0 = noir, 1 = blanc)."""
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = hex_to_rgb(value)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(first: str, second: str) -> float:
    la, lb = relative_luminance(first), relative_luminance(second)
    light, dark = max(la, lb), min(la, lb)
    return (light + 0.05) / (dark + 0.05)


def contrast_text(value: str) -> str:
    """Texte lisible sur ``value`` : blanc tant qu'il atteint le contraste WCAG 3:1 (composants d'interface,
    texte en gras), sinon la couleur (blanc ou ardoise foncée) offrant le meilleur contraste.

    Le seuil de 3:1 conserve le texte blanc des boutons de la charte d'origine (teal 600 : 3,7:1) et bascule
    en texte foncé pour les couleurs claires (jaune, beige, pastels).
    """
    white = contrast_ratio(value, LIGHT_TEXT)
    if white >= MIN_WHITE_CONTRAST:
        return LIGHT_TEXT
    return LIGHT_TEXT if white >= contrast_ratio(value, DARK_TEXT) else DARK_TEXT


def readable_on(color: str, background: str, minimum: float = MIN_TEXT_CONTRAST) -> str:
    """``color`` assombri (fond clair) ou éclairci (fond foncé) par petits pas jusqu'à atteindre ``minimum``.

    Sert aux couleurs de texte dérivées de la charte (liens, texte des badges) : une couleur primaire claire
    (jaune, pastel) ne doit jamais produire de liens illisibles. La couleur d'origine est conservée si elle
    suffit déjà.
    """
    color, background = normalize_hex(color), normalize_hex(background)
    target = "#000000" if relative_luminance(background) >= 0.18 else "#ffffff"
    candidate = color
    for step in range(1, 21):
        if contrast_ratio(candidate, background) >= minimum:
            return candidate
        candidate = mix(color, target, step / 20)
    return candidate


def _lowest_light_background(card: str) -> str:
    """Fond clair le plus sombre sur lequel s'affichent les liens : carte ou fond de page (mode clair)."""
    return min((card, LIGHT_SURFACE, LIGHT_TEXT), key=relative_luminance)


def text_colors(primary: str, secondary: str, card: str) -> dict[str, str]:
    """Couleurs de texte dérivées de la charte et garanties lisibles (contraste WCAG AA 4,5:1)."""
    brand, accent = palette(primary), palette(secondary)
    light_bg = _lowest_light_background(card)
    dark_card = mix(DARK_CARD, primary, 0.05)
    return {
        "link": readable_on(brand[700], light_bg),
        "link_hover": readable_on(brand[800], light_bg),
        "link_dark": readable_on(brand[400], dark_card),
        "link_hover_dark": readable_on(brand[300], dark_card),
        "brand_strong": readable_on(brand[800], brand[100]),
        "accent_strong": readable_on(accent[800], accent[100]),
    }


def primary_is_light(primary: str) -> bool:
    """Vrai si le texte blanc devient illisible sur la couleur primaire (texte foncé imposé)."""
    return contrast_ratio(palette(primary)[600], LIGHT_TEXT) < MIN_WHITE_CONTRAST


def color_warnings(theme: ThemeSettings) -> list[str]:
    """Avertissements (non bloquants) sur la charte enregistrée, affichés après l'enregistrement."""
    v = _theme_values(theme)
    return [PRIMARY_LIGHT_WARNING] if primary_is_light(v["primary_color"]) else []


async def update_colors(db: AsyncSession, values: Mapping[str, object]) -> ThemeSettings:
    """Enregistre les couleurs fournies (champs absents inchangés) ; tout est validé avant modification."""
    cleaned: dict[str, str] = {}
    for field in COLOR_FIELDS:
        if field in values:
            cleaned[field] = normalize_hex(values[field])
    if not cleaned:
        raise BusinessError("Aucune couleur à enregistrer.")
    card = cleaned.get("card_color")
    if card and relative_luminance(card) < MIN_CARD_LUMINANCE:
        raise BusinessError("La couleur des cartes doit rester claire pour garantir la lisibilité du texte.")
    theme = await _begin_change(db)
    for field, value in cleaned.items():
        setattr(theme, field, value)
    await _touch(db, theme)
    return theme


async def reset_colors(db: AsyncSession) -> ThemeSettings:
    """Rétablit la palette d'origine (teal) sans toucher aux logos, à la typographie ni aux documents."""
    theme = await _begin_change(db)
    for field in COLOR_FIELDS:
        setattr(theme, field, DEFAULT_THEME[field])
    await _touch(db, theme)
    return theme


# ================================================================= typographie, documents
async def update_typography(db: AsyncSession, font_family: str, density: str) -> ThemeSettings:
    font_family = (font_family or "").strip().lower()
    density = (density or "").strip().lower()
    if font_family not in FONTS:
        raise BusinessError("Police inconnue.")
    if density not in DENSITIES:
        raise BusinessError("Densité d'affichage inconnue.")
    theme = await _begin_change(db)
    theme.font_family = font_family
    theme.density = density
    await _touch(db, theme)
    return theme


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "on", "yes", "oui"}


def _strip_controls(text: str) -> str:
    """Retire les caractères de contrôle (catégorie Unicode Cc : ESC, GS, NUL, BEL…) d'un texte de document.

    Seul le saut de ligne est conservé (fins de ligne Windows / Mac normalisées), la tabulation devient une
    espace : un formulaire forgé ne peut pas glisser de commande ESC/POS (tiroir-caisse, réinitialisation…)
    dans les tickets imprimés.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    return "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) != "Cc")


async def update_documents(db: AsyncSession, values: Mapping[str, object]) -> ThemeSettings:
    """Slogans (FR / AR), en-tête et pied de page des documents, affichage du logo sur les PDF.

    ``show_logo_on_documents`` suit la sémantique d'une case à cocher : absente = décochée. Les caractères de
    contrôle sont retirés (``_strip_controls``) : ces textes sont imprimés tels quels sur les tickets ESC/POS.
    """
    cleaned: dict[str, str] = {}
    for field, limit in TEXT_LIMITS.items():
        text = _strip_controls(str(values.get(field, "") or "")).strip()
        if len(text) > limit:
            raise BusinessError("Texte trop long pour les documents émis.")
        if field in ("document_header", "document_footer"):
            text = "\n".join(line.rstrip() for line in text.split("\n"))
            if text.count("\n") + 1 > LINE_LIMITS[field]:
                raise BusinessError("Trop de lignes : 10 au maximum pour l'en-tête et 8 pour le pied des documents.")
        elif "\n" in text:
            text = " ".join(part.strip() for part in text.split("\n") if part.strip())
        cleaned[field] = text
    theme = await _begin_change(db)
    for field, text in cleaned.items():
        setattr(theme, field, text)
    theme.show_logo_on_documents = _truthy(values.get("show_logo_on_documents"))
    await _touch(db, theme)
    return theme


# ================================================================= CSS dynamique
def _font_faces() -> list[str]:
    """Déclarations ``@font-face`` pour les fichiers .woff2 présents localement (aucun accès réseau).

    Le dossier n'est parcouru qu'une fois : résultat en cache, vidé par ``invalidate_theme_cache``.
    """
    return list(_font_faces_in(UI_FONTS_DIR))


@functools.lru_cache(maxsize=8)
def _font_faces_in(folder: Path) -> tuple[str, ...]:
    rules: list[str] = []
    if not folder.is_dir():
        return ()
    for path in sorted(folder.glob("*.woff2")):
        match = FONT_WEIGHT_RE.match(path.name)
        if not match or match["key"] not in FONTS or not FONTS[match["key"]]["family"]:
            continue
        weight = match["weight"] or "100 900"
        rules.append(
            f'@font-face {{ font-family: "{FONTS[match["key"]]["family"]}"; font-style: normal; '
            f'font-weight: {weight}; font-display: swap; src: url("/static/fonts/ui/{path.name}") format("woff2"); }}'
        )
    return tuple(rules)


def font_stack(key: str) -> str:
    return FONTS.get(key, FONTS["inter"])["stack"]


def rtl_font_stack(key: str) -> str:
    """Pile pour l'arabe : Tajawal en tête si elle est choisie, sinon Noto Sans Arabic puis la police latine."""
    if key == "tajawal":
        return 'Tajawal, ' + ARABIC_STACK
    family = FONTS.get(key, FONTS["inter"])["family"]
    return ARABIC_STACK.replace("sans-serif", (f'{family}, ' if family else "system-ui, ") + "sans-serif")


def _theme_values(theme: ThemeSettings) -> dict[str, str]:
    """Valeurs re-validées (une base modifiée à la main ne peut pas injecter de CSS)."""
    values = {field: _safe_hex(getattr(theme, field, ""), DEFAULT_THEME[field]) for field in COLOR_FIELDS}
    values["font_family"] = theme.font_family if theme.font_family in FONTS else DEFAULT_THEME["font_family"]
    values["density"] = theme.density if theme.density in DENSITIES else DEFAULT_THEME["density"]
    return values


def build_css(theme: ThemeSettings) -> str:
    """Feuille de variables CSS de la charte : :root (clair), .dark (sombre), html[dir="rtl"], densité."""
    v = _theme_values(theme)
    primary, secondary, card = v["primary_color"], v["secondary_color"], v["card_color"]
    brand, accent = palette(primary), palette(secondary)
    density = DENSITIES[v["density"]]
    statuses = {
        "available": v["status_available_color"], "occupied": v["status_occupied_color"],
        "dirty": v["status_dirty_color"], "maintenance": v["status_maintenance_color"],
    }
    dark_card = mix(DARK_CARD, primary, 0.05)
    dark_surface = mix(DARK_SURFACE, primary, 0.03)
    text = text_colors(primary, secondary, card)

    lines: list[str] = [
        f"/* Charte graphique de l'établissement – version {int(theme.version or 1)} (générée automatiquement) */",
        *_font_faces(),
        ":root {",
    ]
    lines += [f"  --brand-{shade}: {rgb_triplet(color)};" for shade, color in brand.items()]
    lines += [f"  --accent-{shade}: {rgb_triplet(color)};" for shade, color in accent.items()]
    lines += [
        f"  --color-primary: {primary};",
        f"  --color-primary-rgb: {rgb_triplet(primary)};",
        f"  --color-on-primary: {contrast_text(brand[600])};",
        f"  --color-secondary: {secondary};",
        f"  --color-secondary-rgb: {rgb_triplet(secondary)};",
        f"  --color-on-secondary: {contrast_text(accent[600])};",
        f"  --color-card: {card};",
        f"  --color-on-card: {contrast_text(card)};",
        f"  --color-surface: {LIGHT_SURFACE};",
        f"  --color-link: {text['link']};",
        f"  --color-link-hover: {text['link_hover']};",
        f"  --color-brand-strong: {text['brand_strong']};",
        f"  --color-accent-strong: {text['accent_strong']};",
    ]
    for name, color in statuses.items():
        lines.append(f"  --status-{name}: {color};")
        lines.append(f"  --status-{name}-rgb: {rgb_triplet(color)};")
        lines.append(f"  --status-{name}-on: {contrast_text(color)};")
    lines += [
        f"  --font-family: {font_stack(v['font_family'])};",
        f"  --font-family-rtl: {rtl_font_stack(v['font_family'])};",
    ]
    lines += [f"  --density-{name}: {value};" for name, value in density.items()]
    lines += [
        "}",
        "/* Mode sombre : surfaces foncées teintées par la couleur primaire, charte conservée */",
        ".dark {",
        f"  --color-card: {dark_card};",
        f"  --color-on-card: {contrast_text(dark_card)};",
        f"  --color-surface: {dark_surface};",
        f"  --color-link: {text['link_dark']};",
        f"  --color-link-hover: {text['link_hover_dark']};",
        "}",
        "/* Arabe (RTL) : police adaptée à l'écriture arabe */",
        'html[dir="rtl"] { --font-family: var(--font-family-rtl); }',
        'html[dir="rtl"] body, html[dir="rtl"] .ar, html.ar body { font-family: var(--font-family-rtl); }',
        "body { font-family: var(--font-family); }",
    ]
    # Densité : uniquement les variables --density-* ci-dessus, lues par .btn, .input, .card, .table td/th
    # (app/static/src/input.css). Aucune règle de sélecteur ici : chargée après app.css, elle l'emporterait sur
    # les utilitaires Tailwind de même spécificité (p-0, pl-9, mb-0…) posés sur les mêmes éléments.
    return "\n".join(lines) + "\n"


def css_etag(theme: ThemeSettings, css: str) -> str:
    digest = hashlib.sha256(css.encode("utf-8")).hexdigest()[:12]
    return f'"theme-{int(theme.version or 1)}-{digest}"'


# ================================================================= fichiers de marque : validation
def branding_dir() -> Path:
    return config.data_dir / "media" / "branding"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _attribute_name(value: str) -> str:
    """Nom d'attribut normalisé : sans espace de noms (« {uri}href », « xlink:href » → « href »), en minuscules.

    Sert aussi à la valeur de ``attributeName`` des animations, qui est une chaîne simple (« xlink:href ») et
    non un nom qualifié résolu par l'analyseur XML.
    """
    if not isinstance(value, str):
        return ""
    return value.rsplit("}", 1)[-1].rsplit(":", 1)[-1].strip().lower()


def _dangerous_css(text: str) -> bool:
    """CSS refusé : ressource externe (url() hors fragment, @import, image-set), script, ou échappement CSS.

    Toute barre oblique inverse est refusée : les échappements CSS (``@\\69mport``, ``u\\72l(``) masqueraient
    sinon les mots-clés recherchés ; un logo n'en a jamais besoin.
    """
    compact = CONTROL_CHARS_RE.sub("", text).lower()
    return ("\\" in text or "javascript:" in compact or "expression(" in compact or "@import" in compact
            or "image-set(" in compact or "behavior:" in compact or "-moz-binding" in compact
            or bool(CSS_URL_RE.search(compact)))


def sanitize_svg(data: bytes) -> bytes:
    """Vérifie strictement un SVG ; tout élément ou attribut actif entraîne le REFUS (pas de réécriture)."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BusinessError("Fichier SVG illisible (encodage UTF-8 attendu).") from exc
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise BusinessError("SVG refusé : les déclarations DOCTYPE et entités XML sont interdites.")
    refused = "SVG refusé : il contient du contenu actif (script, événement, lien externe ou contenu étranger)."
    # Les instructions de traitement (<?xml-stylesheet href="…"?>) sont ignorées par ElementTree : elles sont
    # donc refusées sur le texte brut, la déclaration XML initiale exceptée.
    if SVG_PROCESSING_INSTRUCTION_RE.search(SVG_XML_DECLARATION_RE.sub("", text, count=1)):
        raise BusinessError(refused)
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise BusinessError("Fichier SVG invalide ou corrompu.") from exc
    if _local_name(root.tag).lower() != "svg":
        raise BusinessError("Fichier SVG invalide ou corrompu.")
    for element in root.iter():
        name = _local_name(element.tag).lower()
        if name in SVG_FORBIDDEN_ELEMENTS:
            raise BusinessError(refused)
        if name == "style":
            # Le navigateur lit TOUT le texte d'un <style>, y compris celui qui suit un élément enfant : un
            # <style> ne doit contenir que du texte, et ce texte entier est contrôlé.
            if len(element) or _dangerous_css("".join(element.itertext())):
                raise BusinessError(refused)
        if name in SVG_ANIMATION_ELEMENTS:
            # Une animation ne peut viser ni un lien (href / xlink:href, quel que soit le préfixe ou la casse)
            # ni un gestionnaire d'événement : elle remplacerait la cible vérifiée par une URL externe.
            for attribute, value in element.attrib.items():
                if _attribute_name(attribute) == "attributename":
                    target = _attribute_name(value)
                    if target == "href" or target.startswith("on"):
                        raise BusinessError(refused)
        for attribute, value in element.attrib.items():
            attr = _attribute_name(attribute)
            compact = CONTROL_CHARS_RE.sub("", value).lower()
            if attr.startswith("on") or "javascript:" in compact or "vbscript:" in compact:
                raise BusinessError(refused)
            if attr == "href":
                # Lien interne au document uniquement (« #id ») ; une image embarquée (data:image/…) n'est admise
                # que sur <image> / <feImage>. Tout autre lien (https:, //hôte, chemin relatif…) sort du document.
                target = value.strip()
                embedded = name in SVG_IMAGE_ELEMENTS and SVG_DATA_IMAGE_RE.match(target)
                if not (SVG_FRAGMENT_RE.match(target) or embedded):
                    raise BusinessError(refused)
            if attr == "style" and _dangerous_css(value):
                raise BusinessError(refused)
            # Attributs de présentation (fill, filter, mask…) : valeurs CSS, mêmes règles qu'un attribut style.
            presentation = "}" not in attribute  # attribut sans espace de noms : interprété par le navigateur
            if attr != "style" and ((presentation and "\\" in value) or "image-set(" in compact
                                    or CSS_URL_RE.search(compact)):
                raise BusinessError(refused)
    return _serialize_svg(root)


def _serialize_svg(root: ElementTree.Element) -> bytes:
    """Re-sérialise l'arbre VÉRIFIÉ (et non le texte reçu) : commentaires, instructions de traitement et tout
    contenu ignoré par l'analyseur disparaissent du fichier enregistré."""
    prefix = "{" + SVG_NAMESPACE + "}"
    namespaced = root.tag.startswith(prefix)
    # Espace de noms SVG par défaut (balises sans préfixe « ns0: ») : ElementTree n'accepte pas d'option
    # default_namespace avec des attributs non qualifiés, les balises SVG sont donc ramenées à leur nom local.
    for element in root.iter():
        if isinstance(element.tag, str) and element.tag.startswith(prefix):
            element.tag = element.tag[len(prefix):]
    if namespaced:
        root.set("xmlns", SVG_NAMESPACE)
    body = ElementTree.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body).encode("utf-8")


def _sniff(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "raster"
    if data.startswith(b"\xff\xd8\xff"):
        return "raster"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "raster"
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<?xml") or head.startswith(b"<svg") or head.startswith(b"<!--") or b"<svg" in head:
        return "svg"
    return "unknown"


def open_raster(data: bytes) -> Image.Image:
    """Ouvre une image PNG / WEBP / JPEG en refusant les fichiers corrompus et les « decompression bombs »."""
    invalid = "Image invalide ou corrompue."
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as probe:
                if probe.format not in RASTER_FORMATS:
                    raise BusinessError("Format d'image non pris en charge (PNG, WEBP, JPEG ou SVG).")
                width, height = probe.size
                if width * height > MAX_IMAGE_PIXELS or max(width, height) > MAX_IMAGE_SIDE:
                    raise BusinessError("Image trop grande : 25 mégapixels et 8000 pixels de côté au maximum.")
                probe.verify()
            image = Image.open(BytesIO(data))
            image.load()
    except BusinessError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise BusinessError("Image trop grande : 25 mégapixels et 8000 pixels de côté au maximum.") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError) as exc:
        raise BusinessError(invalid) from exc
    return image


def _normalized(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image)
    has_alpha = image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info)
    return image.convert("RGBA" if has_alpha else "RGB")


def _png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def process_logo(data: bytes) -> bytes:
    """Logo raster → PNG (transparence conservée), réduit à 1024 × 512 px au maximum, ratio conservé."""
    image = _normalized(open_raster(data))
    image.thumbnail(LOGO_MAX_SIZE, Image.Resampling.LANCZOS)
    return _png_bytes(image)


def process_favicon(data: bytes) -> dict[int, bytes]:
    """Favicon → PNG carrés 64 × 64 et 32 × 32 (image centrée sur fond transparent)."""
    image = _normalized(open_raster(data)).convert("RGBA")
    side = max(image.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
    return {size: _png_bytes(square.resize((size, size), Image.Resampling.LANCZOS)) for size in FAVICON_SIZES}


def prepare_branding_files(kind: str, data: bytes) -> dict[str, bytes]:
    """Valide et convertit un fichier téléversé ; renvoie {nom de fichier: contenu} (fichier principal en tête)."""
    if kind not in KIND_FIELDS:
        raise BusinessError("Type de fichier de marque inconnu.")
    if not data:
        raise BusinessError("Aucun fichier sélectionné.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise BusinessError("Fichier trop volumineux (2 Mo maximum).")
    stem = f"{kind}-{secrets.token_hex(6)}"
    detected = _sniff(data)
    if detected == "svg":
        if kind == "favicon":
            raise BusinessError("Le favicon doit être une image PNG, WEBP ou JPEG.")
        return {f"{stem}.svg": sanitize_svg(data)}
    if detected != "raster":
        raise BusinessError("Format d'image non pris en charge (PNG, WEBP, JPEG ou SVG).")
    if kind == "favicon":
        icons = process_favicon(data)
        return {f"{stem}.png": icons[64], f"{stem}-32.png": icons[32]}
    return {f"{stem}.png": process_logo(data)}


def _companions(name: str) -> list[Path]:
    """Fichier principal et déclinaisons (favicon 32 px, rendu PNG d'un SVG)."""
    if not name or not FILE_NAME_RE.match(name):
        return []
    stem = name.rsplit(".", 1)[0]
    folder = branding_dir()
    return [folder / name, folder / f"{stem}-32.png", folder / f"{stem}.png"]


def _remove_files(name: str) -> None:
    for path in _companions(name):
        path.unlink(missing_ok=True)


def _write_files(files: dict[str, bytes]) -> None:
    folder = branding_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        temporary = folder / f".{name}.tmp"
        temporary.write_bytes(content)
        temporary.replace(folder / name)


# ================================================================= fichiers de marque : opérations
def _referenced_files(names: set[str]) -> set[str]:
    """Fichiers (principal et déclinaisons) correspondant aux noms référencés par la charte."""
    return {path.name for name in names for path in _companions(name)}


def _purge_unreferenced(names: set[str]) -> list[str]:
    """Supprime de ``data/media/branding`` les fichiers de marque qu'aucune référence ne désigne plus.

    Appelé après un commit réussi, verrou des modifications détenu : aucun téléversement concurrent n'a pu
    écrire de fichier pas encore validé. Seuls les noms produits par ce module (FILE_NAME_RE) sont concernés ;
    un fichier référencé (ou l'une de ses déclinaisons) n'est jamais supprimé.
    """
    keep = _referenced_files(names)
    removed: list[str] = []
    folder = branding_dir()
    if not folder.is_dir():
        return removed
    for path in sorted(folder.iterdir()):
        if path.is_file() and FILE_NAME_RE.match(path.name) and path.name not in keep:
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return removed


def _rollback_files(db: AsyncSession) -> list[str]:
    """Fichiers écrits par la modification en cours, supprimés si sa transaction est annulée."""
    return db.sync_session.info[_THEME_CHANGE]["rollback_files"]


async def save_image(db: AsyncSession, kind: str, upload: Any) -> ThemeSettings:
    """Enregistre un logo / favicon téléversé (``UploadFile`` ou objet ayant ``read``) et remplace l'ancien.

    Conversion hors verrou ; lecture de l'ancien fichier, écriture du nouveau et nouvelle version sous le verrou
    des modifications (libéré à la fin de la transaction). Après un commit réussi, l'ancien fichier et tout
    fichier orphelin sont purgés ; en cas d'échec, c'est le nouveau fichier qui est supprimé.
    """
    if kind not in KIND_FIELDS:
        raise BusinessError("Type de fichier de marque inconnu.")
    if upload is None or not hasattr(upload, "read"):
        raise BusinessError("Aucun fichier sélectionné.")
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    files = await asyncio.to_thread(prepare_branding_files, kind, data)
    theme = await _begin_change(db)
    new_name = next(iter(files))
    _rollback_files(db).append(new_name)
    await asyncio.to_thread(_write_files, files)
    setattr(theme, KIND_FIELDS[kind], new_name)
    await _touch(db, theme)
    return theme


async def delete_image(db: AsyncSession, kind: str) -> ThemeSettings:
    """Retire un fichier de marque ; le fichier n'est supprimé du disque qu'après un commit réussi."""
    if kind not in KIND_FIELDS:
        raise BusinessError("Type de fichier de marque inconnu.")
    theme = await _begin_change(db)
    field = KIND_FIELDS[kind]
    if not (getattr(theme, field) or ""):
        raise BusinessError("Aucun fichier à supprimer.")
    setattr(theme, field, "")
    await _touch(db, theme)
    return theme


def restore_branding_files(archive: Any, prefix: str) -> list[str]:
    """Extrait d'une archive ZIP de sauvegarde (``zipfile.ZipFile`` ouvert) les fichiers de marque rangés sous
    ``<prefix>branding/`` vers ``data/media/branding``. Seuls les noms produits par ce module (FILE_NAME_RE,
    sans sous-dossier) sont acceptés : aucun chemin arbitraire (« zip slip ») ne peut être écrit."""
    folder_prefix = prefix + "branding/"
    restored: dict[str, bytes] = {}
    for info in archive.infolist():
        if info.is_dir() or not info.filename.startswith(folder_prefix):
            continue
        name = info.filename[len(folder_prefix):]
        if not FILE_NAME_RE.match(name) or info.file_size > MAX_UPLOAD_BYTES * 4:
            continue
        restored[name] = archive.read(info)
    if restored:
        _write_files(restored)
    return sorted(restored)


async def reconcile_branding(db: AsyncSession) -> dict[str, list[str]]:
    """Remet en cohérence la base et le dossier des fichiers de marque (après une restauration) :
    vide les champs dont le fichier est absent et supprime les fichiers que la charte ne référence plus."""
    theme = await _begin_change(db)
    cleared: list[str] = []
    for kind, field in KIND_FIELDS.items():
        name = getattr(theme, field) or ""
        if name and branding_file(theme, kind) is None:
            setattr(theme, field, "")
            cleared.append(kind)
    if cleared:
        await _touch(db, theme)
    removed = _purge_unreferenced({getattr(theme, field) or "" for field in KIND_FIELDS.values()})
    return {"cleared": cleared, "removed": removed}


def branding_file(theme: ThemeSettings, kind: str, size: int | None = None) -> Path | None:
    """Chemin du fichier servi pour ``kind`` (``size=32`` : petite déclinaison du favicon), ou None."""
    field = KIND_FIELDS.get(kind)
    name = (getattr(theme, field, "") or "") if field else ""
    if not name or not FILE_NAME_RE.match(name):
        return None
    if kind == "favicon" and size == 32:
        name = name.rsplit(".", 1)[0] + "-32.png"
    path = branding_dir() / name
    return path if path.is_file() else None


def media_type_for(path: Path) -> str:
    return MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def logo_path_for_documents(theme: ThemeSettings) -> Path | None:
    """Logo raster (PNG) utilisable par ReportLab, ou None (logo absent, désactivé, ou SVG sans rendu PNG)."""
    if not theme.show_logo_on_documents:
        return None
    return printable_logo_path(theme)


def printable_logo_path(theme: ThemeSettings) -> Path | None:
    """Logo principal enregistré que les PDF savent imprimer (raster ou SVG doté d'un rendu PNG), quel que soit
    l'état de l'option « logo sur les documents » ; None sinon."""
    path = branding_file(theme, "logo")
    if path is None:
        return None
    if path.suffix.lower() == ".svg":
        rendition = path.with_suffix(".png")
        return rendition if rendition.is_file() else None
    return path


def branding_urls(theme: ThemeSettings) -> dict[str, str]:
    """URL versionnées (cache-busting) des ressources de marque ; chaîne vide si le fichier est absent."""
    version = int(theme.version or 1)
    # Présence vérifiée sur le disque (comme la route /branding) : une base qui référence un fichier absent
    # (restauration partielle, copie manuelle) retombe sur le logo par défaut au lieu d'une image cassée.
    logo = f"/branding/logo?v={version}" if branding_file(theme, "logo") else ""
    logo_dark = f"/branding/logo-dark?v={version}" if branding_file(theme, "logo-dark") else logo
    has_favicon = branding_file(theme, "favicon") is not None and branding_file(theme, "favicon", 32) is not None
    favicon = f"/branding/favicon?v={version}" if has_favicon else ""
    favicon_32 = f"/branding/favicon?size=32&v={version}" if has_favicon else ""
    return {"css": f"{CSS_URL}?v={version}", "logo": logo, "logo_dark": logo_dark,
            "favicon": favicon, "favicon_32": favicon_32}


def theme_context(theme: ThemeSettings) -> dict[str, Any]:
    """Contexte commun à injecter dans tous les templates (``theme`` et ``branding``)."""
    return {"theme": theme, "branding": branding_urls(theme)}


def color_fields(theme: ThemeSettings) -> list[dict[str, str]]:
    """Description des sélecteurs de couleur de la page d'administration."""
    return [{"name": field, "label": COLOR_LABELS[field], "help": COLOR_HELPS[field],
             "value": _safe_hex(getattr(theme, field), DEFAULT_THEME[field]), "default": DEFAULT_THEME[field]}
            for field in COLOR_FIELDS]


def preview(theme: ThemeSettings) -> dict[str, Any]:
    """Données de l'aperçu en direct : nuances calculées et couleurs de texte lisibles."""
    v = _theme_values(theme)
    return {
        "brand": palette(v["primary_color"]),
        "accent": palette(v["secondary_color"]),
        "on_primary": contrast_text(palette(v["primary_color"])[600]),
        "on_secondary": contrast_text(palette(v["secondary_color"])[600]),
        "font_stack": font_stack(v["font_family"]),
        "font_stack_rtl": rtl_font_stack(v["font_family"]),
    }
