"""Gestion des tarifs par l'administration : mises à jour en masse, prix par chambre, saisons, paliers long séjour,
majoration week-end, historique des modifications (price_changes) et saisons d'exemple.

Les fonctions modifient la session sans committer (le routeur commit via ``attempt``) et lèvent ``BusinessError``
avec des messages FIXES (sans valeur interpolée) afin qu'ils soient traduits à l'affichage (app/locales/*/pricing.json).

Rappel : aucune de ces opérations ne touche aux nuitées déjà postées (folio_items) ni aux factures émises.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from fractions import Fraction

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.room import SEASON_LEVELS, LongStayDiscount, PriceChange, Room, RoomType, Season
from app.models.user import User
from app.services.audit_log import log_action
from app.services.common import BusinessError
from app.services.settings import HotelSettings, load_hotel_settings, now_of, save_settings_subset, today_of

MAX_PRICE = 10_000_000          # DZD TTC / nuit : garde-fou contre les fautes de frappe
PERCENT_MIN, PERCENT_MAX = -90, 300   # ajustements de saison, de masse et majoration week-end
DISCOUNT_MIN, DISCOUNT_MAX = 1, 90    # remise long séjour
NIGHTS_MIN, NIGHTS_MAX = 2, 365       # nuits minimum d'un palier
PRIORITY_MIN, PRIORITY_MAX = -100, 100
MAX_SEASON_DAYS = 366
WEEKDAYS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

# Messages d'erreur (textes sources traduits dans app/locales/*/pricing.json)
MSG_PRICE = "Le prix doit être un nombre entier compris entre 0 et 10 000 000 DA."
MSG_PERCENT = "Le pourcentage doit être un nombre entier compris entre -90 et 300."
MSG_DISCOUNT = "La remise doit être un nombre entier compris entre 1 et 90 %."
MSG_NIGHTS = "Le nombre de nuits minimum doit être compris entre 2 et 365."
MSG_PRIORITY = "La priorité doit être un nombre entier compris entre -100 et 100."
MSG_DATE = "Date invalide (format AAAA-MM-JJ)."
MSG_DATES_ORDER = "La date de fin doit être postérieure à la date de début."
MSG_SEASON_LENGTH = "Une saison ne peut pas dépasser 366 jours."
MSG_NAME = "Le nom de la saison est requis."
MSG_LEVEL = "Niveau de saison invalide."
MSG_ROOM_TYPE = "Type de chambre introuvable."
MSG_ROOM = "Chambre introuvable."
MSG_SEASON = "Saison introuvable."
MSG_TIER = "Palier introuvable."
MSG_TIER_EXISTS = "Un palier existe déjà pour ce nombre de nuits."
MSG_MODE = "Type de mise à jour invalide."
MSG_NOTHING = "Aucun prix modifié."
MSG_WEEKDAYS = "Jours de week-end invalides."
MSG_ZERO_PERCENT = "Indiquez un pourcentage différent de zéro."


# ---------------------------------------------------------------- saisie
def _text(value) -> str:
    return str(value or "").strip()


def _integer(value, message: str, minimum: int, maximum: int, default: int | None = None) -> int:
    raw = _text(value).replace(" ", "").replace(" ", "").replace(" ", "")
    if raw == "":
        if default is not None:
            return default
        raise BusinessError(message)
    try:
        number = float(raw.replace(",", "."))
    except ValueError as exc:
        raise BusinessError(message) from exc
    if not math.isfinite(number) or number != int(number):
        raise BusinessError(message)
    number = int(number)
    if not minimum <= number <= maximum:
        raise BusinessError(message)
    return number


def parse_price(value, allow_empty: bool = False) -> int | None:
    """Prix TTC en DZD : entier ≥ 0 (vide autorisé = « pas de prix spécifique » si ``allow_empty``)."""
    if allow_empty and _text(value) == "":
        return None
    return _integer(value, MSG_PRICE, 0, MAX_PRICE)


def parse_percent(value, default: int | None = None) -> int:
    return _integer(value, MSG_PERCENT, PERCENT_MIN, PERCENT_MAX, default)


def parse_date_value(value) -> date:
    try:
        return date.fromisoformat(_text(value))
    except ValueError as exc:
        raise BusinessError(MSG_DATE) from exc


def round_to_100(amount: int | Fraction) -> int:
    """Arrondi à la centaine de dinars la plus proche (50 arrondi au supérieur)."""
    value = Fraction(amount)
    return max(0, math.floor(value / 100 + Fraction(1, 2)) * 100)


def apply_percent(amount: int, percent: int, round_100: bool) -> int:
    value = Fraction(amount) * Fraction(100 + percent, 100)
    return round_to_100(value) if round_100 else max(0, math.floor(value + Fraction(1, 2)))


def _fmt(value) -> str:
    if value is None:
        return "—"
    return str(value)


# ---------------------------------------------------------------- historique
async def record_change(db: AsyncSession, user: User | None, target_type: str, target_id, target_label: str,
                        field: str, old_value, new_value, reason: str = "", when: datetime | None = None) -> None:
    """Ajoute une ligne à l'historique des prix (aucune ligne si la valeur ne change pas)."""
    old, new = _fmt(old_value), _fmt(new_value)
    if old == new:
        return
    db.add(PriceChange(
        created_at=when or datetime.now(), user_id=user.id if user else None,
        username=user.username if user else "système", target_type=target_type, target_id=str(target_id or "")[:40],
        target_label=(target_label or "")[:160], field=field, old_value=old[:80], new_value=new[:80],
        reason=(reason or "")[:1000],
    ))


async def recent_changes(db: AsyncSession, limit: int = 50) -> list[PriceChange]:
    stmt = select(PriceChange).order_by(PriceChange.created_at.desc(), PriceChange.id.desc()).limit(limit)
    return list((await db.scalars(stmt)).all())


async def _when(db: AsyncSession) -> tuple[HotelSettings, datetime]:
    hs = await load_hotel_settings(db)
    return hs, now_of(hs)


# ---------------------------------------------------------------- mise à jour en masse
@dataclass
class BulkResult:
    changed: int = 0


async def bulk_update(db: AsyncSession, user: User | None, form) -> BulkResult:
    """Mise à jour en masse.

    - ``mode = "base"`` : nouveau prix de base par type (champs ``base_price_<id>``, vide = inchangé) ;
    - ``mode = "percent"`` : +/- ``percent`` % sur tous les types (``room_type_id`` vide) ou un seul type ; les prix
      spécifiques des chambres concernées suivent si ``include_rooms`` est coché.
    ``round_100`` arrondit les nouveaux prix à la centaine de dinars.
    """
    mode = _text(form.get("mode"))
    round_100 = bool(form.get("round_100"))
    reason = _text(form.get("reason"))[:1000]
    _, now = await _when(db)
    types = list((await db.scalars(select(RoomType).order_by(RoomType.code))).all())
    result = BulkResult()
    if mode == "base":
        updates: list[tuple[RoomType, int]] = []
        for room_type in types:
            raw = form.get(f"base_price_{room_type.id}")
            if raw is None or _text(raw) == "":
                continue
            price = parse_price(raw)
            if round_100:
                price = round_to_100(price)
            updates.append((room_type, price))
        for room_type, price in updates:
            if price != room_type.base_price:
                await record_change(db, user, "room_type", room_type.code, room_type.name, "base_price",
                                    room_type.base_price, price, reason, now)
                room_type.base_price = price
                result.changed += 1
        details = ", ".join(f"{rt.code}={price}" for rt, price in updates)
    elif mode == "percent":
        percent = parse_percent(form.get("percent"))
        if percent == 0:
            raise BusinessError(MSG_ZERO_PERCENT)
        type_raw = _text(form.get("room_type_id"))
        if type_raw:
            selected = [rt for rt in types if str(rt.id) == type_raw]
            if not selected:
                raise BusinessError(MSG_ROOM_TYPE)
        else:
            selected = types
        for room_type in selected:
            price = apply_percent(room_type.base_price, percent, round_100)
            if price != room_type.base_price:
                await record_change(db, user, "room_type", room_type.code, room_type.name, "base_price",
                                    room_type.base_price, price, reason, now)
                room_type.base_price = price
                result.changed += 1
        if form.get("include_rooms"):
            ids = [rt.id for rt in selected]
            rooms = (await db.scalars(select(Room).where(Room.room_type_id.in_(ids),
                                                         Room.price_override.is_not(None)))).unique().all()
            for room in rooms:
                price = apply_percent(room.price_override, percent, round_100)
                if price != room.price_override:
                    await record_change(db, user, "room", room.number, f"Chambre {room.number}", "price_override",
                                        room.price_override, price, reason, now)
                    room.price_override = price
                    result.changed += 1
        scope = ",".join(rt.code for rt in selected)
        details = f"{percent:+d} % ({scope})"
    else:
        raise BusinessError(MSG_MODE)
    if not result.changed:
        raise BusinessError(MSG_NOTHING)
    await log_action(db, user, "pricing_bulk_update", "pricing", mode,
                     f"{details} ; arrondi 100 DA : {'oui' if round_100 else 'non'} ; {result.changed} prix ; {reason}",
                     now)
    return result


# ---------------------------------------------------------------- prix par chambre
async def set_room_price(db: AsyncSession, user: User | None, room_id: int, price: int | None,
                         reason: str = "") -> Room:
    """Prix spécifique d'une chambre (TTC / nuit) ; ``None`` = retour au prix du type."""
    room = await db.get(Room, room_id)
    if room is None:
        raise BusinessError(MSG_ROOM)
    _, now = await _when(db)
    old = room.price_override
    if old == price:
        raise BusinessError(MSG_NOTHING)
    room.price_override = price
    await record_change(db, user, "room", room.number, f"Chambre {room.number}", "price_override", old, price,
                        reason, now)
    await log_action(db, user, "room_price_updated", "room", room.number,
                     f"prix spécifique {_fmt(old)} → {_fmt(price)} ; {reason}", now)
    return room


# ---------------------------------------------------------------- saisons
async def _season_values(db: AsyncSession, form) -> dict:
    name = _text(form.get("name"))[:120]
    if not name:
        raise BusinessError(MSG_NAME)
    level = _text(form.get("level")) or "medium"
    if level not in SEASON_LEVELS:
        raise BusinessError(MSG_LEVEL)
    start = parse_date_value(form.get("start_date"))
    end = parse_date_value(form.get("end_date"))
    if end <= start:
        raise BusinessError(MSG_DATES_ORDER)
    if (end - start).days > MAX_SEASON_DAYS:
        raise BusinessError(MSG_SEASON_LENGTH)
    type_raw = _text(form.get("room_type_id"))
    room_type_id = None
    if type_raw:
        room_type = await db.get(RoomType, int(type_raw)) if type_raw.isdigit() else None
        if room_type is None:
            raise BusinessError(MSG_ROOM_TYPE)
        room_type_id = room_type.id
    return {
        "name": name, "level": level, "start_date": start, "end_date": end,
        "adjustment_percent": parse_percent(form.get("adjustment_percent")),
        "priority": _integer(form.get("priority"), MSG_PRIORITY, PRIORITY_MIN, PRIORITY_MAX, default=0),
        "active": bool(form.get("active")), "room_type_id": room_type_id,
    }


def season_summary(season: Season) -> str:
    scope = season.room_type.code if season.room_type is not None else "*"
    state = "actif" if season.active else "inactif"
    return (f"{season.adjustment_percent:+d} % {season.start_date:%d/%m/%Y}→{season.end_date:%d/%m/%Y} "
            f"P{season.priority} {scope} {state}")


async def create_season(db: AsyncSession, user: User | None, form) -> Season:
    values = await _season_values(db, form)
    _, now = await _when(db)
    season = Season(**values)
    db.add(season)
    await db.flush()
    await db.refresh(season, ["room_type"])
    reason = _text(form.get("reason"))
    await record_change(db, user, "season", season.id, season.name, "season", None, season_summary(season), reason, now)
    await log_action(db, user, "season_created", "season", season.id, f"{season.name} : {season_summary(season)}", now)
    return season


async def update_season(db: AsyncSession, user: User | None, season_id: int, form) -> Season:
    season = await db.get(Season, season_id)
    if season is None:
        raise BusinessError(MSG_SEASON)
    values = await _season_values(db, form)
    _, now = await _when(db)
    before = season_summary(season)
    for key, value in values.items():
        setattr(season, key, value)
    await db.flush()
    await db.refresh(season, ["room_type"])
    after = season_summary(season)
    reason = _text(form.get("reason"))
    await record_change(db, user, "season", season.id, season.name, "season", before, after, reason, now)
    await log_action(db, user, "season_updated", "season", season.id, f"{season.name} : {before} → {after}", now)
    return season


async def delete_season(db: AsyncSession, user: User | None, season_id: int, reason: str = "") -> None:
    season = await db.get(Season, season_id)
    if season is None:
        raise BusinessError(MSG_SEASON)
    _, now = await _when(db)
    summary = season_summary(season)
    await record_change(db, user, "season", season.id, season.name, "season", summary, "supprimée", reason, now)
    await log_action(db, user, "season_deleted", "season", season.id, f"{season.name} : {summary}", now)
    await db.delete(season)


# ---------------------------------------------------------------- paliers long séjour
async def _tier_values(db: AsyncSession, form, exclude_id: int | None = None) -> dict:
    min_nights = _integer(form.get("min_nights"), MSG_NIGHTS, NIGHTS_MIN, NIGHTS_MAX)
    discount = _integer(form.get("discount_percent"), MSG_DISCOUNT, DISCOUNT_MIN, DISCOUNT_MAX)
    stmt = select(func.count(LongStayDiscount.id)).where(LongStayDiscount.min_nights == min_nights)
    if exclude_id is not None:
        stmt = stmt.where(LongStayDiscount.id != exclude_id)
    if await db.scalar(stmt):
        raise BusinessError(MSG_TIER_EXISTS)
    return {"min_nights": min_nights, "discount_percent": discount, "active": bool(form.get("active"))}


def tier_summary(tier: LongStayDiscount) -> str:
    return f"≥ {tier.min_nights} n : -{tier.discount_percent} % ({'actif' if tier.active else 'inactif'})"


async def create_tier(db: AsyncSession, user: User | None, form) -> LongStayDiscount:
    values = await _tier_values(db, form)
    _, now = await _when(db)
    tier = LongStayDiscount(**values)
    db.add(tier)
    await db.flush()
    reason = _text(form.get("reason"))
    await record_change(db, user, "long_stay", tier.id, f"{tier.min_nights} nuits", "long_stay", None,
                        tier_summary(tier), reason, now)
    await log_action(db, user, "long_stay_created", "long_stay", tier.id, tier_summary(tier), now)
    return tier


async def update_tier(db: AsyncSession, user: User | None, tier_id: int, form) -> LongStayDiscount:
    tier = await db.get(LongStayDiscount, tier_id)
    if tier is None:
        raise BusinessError(MSG_TIER)
    values = await _tier_values(db, form, exclude_id=tier.id)
    _, now = await _when(db)
    before = tier_summary(tier)
    for key, value in values.items():
        setattr(tier, key, value)
    after = tier_summary(tier)
    reason = _text(form.get("reason"))
    await record_change(db, user, "long_stay", tier.id, f"{tier.min_nights} nuits", "long_stay", before, after,
                        reason, now)
    await log_action(db, user, "long_stay_updated", "long_stay", tier.id, f"{before} → {after}", now)
    return tier


async def delete_tier(db: AsyncSession, user: User | None, tier_id: int, reason: str = "") -> None:
    tier = await db.get(LongStayDiscount, tier_id)
    if tier is None:
        raise BusinessError(MSG_TIER)
    _, now = await _when(db)
    summary = tier_summary(tier)
    await record_change(db, user, "long_stay", tier.id, f"{tier.min_nights} nuits", "long_stay", summary,
                        "supprimé", reason, now)
    await log_action(db, user, "long_stay_deleted", "long_stay", tier.id, summary, now)
    await db.delete(tier)


# ---------------------------------------------------------------- week-end
async def save_weekend(db: AsyncSession, user: User | None, form) -> HotelSettings:
    """Jours de week-end (cases ``weekend_day`` 0 = lundi … 6 = dimanche) et majoration en %."""
    raw_days = form.getlist("weekend_day") if hasattr(form, "getlist") else form.get("weekend_day", [])
    if isinstance(raw_days, str):
        raw_days = [raw_days]
    days: set[int] = set()
    for raw in raw_days:
        value = _text(raw)
        if not value.isdigit() or not 0 <= int(value) <= 6:
            raise BusinessError(MSG_WEEKDAYS)
        days.add(int(value))
    percent = parse_percent(form.get("weekend_surcharge_percent"))
    hs, now = await _when(db)
    old_days, old_percent = hs.weekend_days, hs.weekend_surcharge_percent
    new_days = ",".join(str(d) for d in sorted(days))
    validated = await save_settings_subset(db, {"weekend_days": new_days, "weekend_surcharge_percent": percent})
    reason = _text(form.get("reason"))
    await record_change(db, user, "weekend", "weekend", "Week-end", "weekend_days", old_days or "—",
                        validated.weekend_days or "—", reason, now)
    await record_change(db, user, "weekend", "weekend", "Week-end", "weekend_surcharge_percent", old_percent,
                        validated.weekend_surcharge_percent, reason, now)
    await log_action(db, user, "weekend_pricing_saved", "settings", "weekend",
                     f"jours {old_days} → {validated.weekend_days} ; {old_percent} % → "
                     f"{validated.weekend_surcharge_percent} %", now)
    return validated


# ---------------------------------------------------------------- saisons d'exemple (seed)
def _sample_seasons(today: date) -> list[dict]:
    """Trois saisons d'exemple pour Annaba, INACTIVES : l'administrateur les ajuste puis les active."""
    samples = [
        ("Été – haute saison balnéaire", "high", (7, 1), (9, 1), 0, 20, 10),
        ("Avant-saison estivale", "medium", (6, 1), (7, 1), 0, 10, 5),
        ("Hiver – basse saison", "low", (11, 15), (3, 1), 1, -10, 0),
    ]
    seasons = []
    for name, level, (sm, sd), (em, ed), year_shift, percent, priority in samples:
        year = today.year
        end = date(year + year_shift, em, ed)
        if end <= today:  # saison déjà passée cette année : on propose celle de l'année suivante
            year += 1
            end = date(year + year_shift, em, ed)
        seasons.append({"name": name, "level": level, "start_date": date(year, sm, sd), "end_date": end,
                        "adjustment_percent": percent, "priority": priority, "active": False, "room_type_id": None})
    return seasons


async def ensure_pricing_defaults(db: AsyncSession) -> int:
    """Crée les saisons d'exemple au premier démarrage (idempotent).

    Rien n'est créé si une saison existe déjà ou si l'historique montre que l'administrateur a déjà géré les
    saisons (par exemple tout supprimé) : ses choix ne sont jamais écrasés. Retourne le nombre de saisons créées.
    """
    if await db.scalar(select(func.count(Season.id))):
        return 0
    if await db.scalar(select(func.count(PriceChange.id)).where(PriceChange.target_type == "season")):
        return 0
    hs = await load_hotel_settings(db)
    samples = _sample_seasons(today_of(hs))
    for values in samples:
        db.add(Season(**values))
    await db.flush()
    return len(samples)
