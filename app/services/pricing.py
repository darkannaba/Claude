"""Moteur de tarification dynamique.

Ordre de calcul d'une nuitée (prix TTC en DZD) :

1. Prix de référence, le premier disponible parmi :
   a. ``Reservation.nightly_rate_override`` (prix négocié, notamment par un canal OTA) : il est appliqué TEL QUEL,
      AUCUN des ajustements suivants ne s'applique (ni saison, ni week-end, ni remise) ;
   b. ``Room.price_override`` : prix spécifique de la chambre ;
   c. ``RatePeriod`` : tarif saisonnier fixe du type de chambre (la période de plus haute priorité couvrant la nuit) ;
   d. ``RoomType.base_price`` : prix de base du type.
2. Ajustement de saison (``Season``) : la saison ACTIVE de plus haute priorité (à égalité, la plus récente) qui
   couvre la nuit [début, fin) et le type de chambre (ou tous les types) : montant × (100 + %) / 100.
3. Majoration week-end (``HotelSettings.weekend_days`` / ``weekend_surcharge_percent``) : × (100 + %) / 100.
4. Remise long séjour (``LongStayDiscount``) : palier actif au plus grand nombre de nuits minimum inférieur ou égal
   au nombre TOTAL de nuits du séjour : × (100 − %) / 100.
5. Remise entreprise (``Company.discount_percent``) : × (100 − %) / 100.
6. Arrondi à l'entier DZD (demi-dinar arrondi au supérieur), jamais négatif.

Les étapes sont calculées en fractions exactes (aucun arrondi intermédiaire) : seul le résultat final est arrondi.
La décomposition HT / TVA (``split_vat``, taux d'hébergement des paramètres) est celle du folio et des factures.

Immuabilité : ce module ne fait que CALCULER. Les nuitées déjà postées par l'audit de nuit (``folio_items``, clé
unique ``night_key``) et les factures émises conservent leur montant ; ``build_folio`` ne projette au tarif courant
que les nuits non encore postées. Un changement de tarif s'applique donc aux nouvelles réservations et aux nuits
pas encore clôturées uniquement.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.room import LongStayDiscount, RatePeriod, Room, RoomType, Season
from app.services.common import split_vat
from app.services.settings import HotelSettings

# Libellés des étapes (textes sources français, traduits à l'affichage via |tr ou t()).
STEP_LABELS = {
    "override": "Prix négocié (réservation)",
    "room": "Prix spécifique de la chambre",
    "rate_period": "Tarif saisonnier fixe",
    "base": "Prix de base du type",
    "season": "Ajustement de saison",
    "weekend": "Majoration week-end",
    "long_stay": "Remise long séjour",
    "company": "Remise entreprise",
}


def round_dzd(amount: Fraction | int | float) -> int:
    """Arrondi à l'entier DZD le plus proche (0,5 arrondi au supérieur), jamais négatif."""
    value = amount if isinstance(amount, Fraction) else Fraction(amount)
    return max(0, math.floor(value + Fraction(1, 2)))


@dataclass
class PriceStep:
    code: str           # override / room / rate_period / base / season / weekend / long_stay / company
    label: str          # libellé source français
    detail: str = ""    # nom de la saison, de la période…
    percent: int | None = None
    amount: int = 0     # montant TTC arrondi après l'étape
    delta: int = 0      # variation arrondie due à l'étape

    def as_dict(self) -> dict:
        return {"code": self.code, "label": self.label, "detail": self.detail, "percent": self.percent,
                "amount": self.amount, "delta": self.delta}


@dataclass
class NightPrice:
    """Détail du prix d'une nuitée : étapes successives puis HT / TVA / TTC."""

    night: date
    source: str
    steps: list[PriceStep] = field(default_factory=list)
    total_ttc: int = 0
    vat_rate: int = 0
    total_ht: int = 0
    total_vat: int = 0
    room_number: str = ""

    @property
    def reference(self) -> int:
        return self.steps[0].amount if self.steps else 0

    def as_dict(self) -> dict:
        return {"night": self.night.isoformat(), "source": self.source, "room": self.room_number,
                "steps": [s.as_dict() for s in self.steps], "total_ttc": self.total_ttc,
                "total_ht": self.total_ht, "total_vat": self.total_vat, "vat_rate": self.vat_rate}


@dataclass
class StayQuote:
    """Estimation d'un séjour : détail par nuit et totaux."""

    check_in: date
    check_out: date
    nights: list[NightPrice] = field(default_factory=list)
    long_stay_percent: int = 0

    @property
    def nights_count(self) -> int:
        return len(self.nights)

    @property
    def total_ttc(self) -> int:
        return sum(n.total_ttc for n in self.nights)

    @property
    def total_ht(self) -> int:
        return sum(n.total_ht for n in self.nights)

    @property
    def total_vat(self) -> int:
        return sum(n.total_vat for n in self.nights)

    def as_dict(self) -> dict:
        return {"check_in": self.check_in.isoformat(), "check_out": self.check_out.isoformat(),
                "nights_count": self.nights_count, "long_stay_percent": self.long_stay_percent,
                "total_ttc": self.total_ttc, "total_ht": self.total_ht, "total_vat": self.total_vat,
                "nights": [n.as_dict() for n in self.nights]}


class RateCalendar:
    """Calendrier tarifaire chargé une fois puis évalué en mémoire (aucune requête par nuit).

    ``RateCalendar(periods, hs)`` reste valable : saisons et paliers long séjour sont facultatifs.
    """

    def __init__(self, periods: list[RatePeriod], hs: HotelSettings, seasons: list[Season] | None = None,
                 long_stay: list[LongStayDiscount] | None = None):
        self.periods = sorted(periods, key=lambda p: (p.priority, p.id or 0), reverse=True)
        self.seasons = sorted((s for s in (seasons or []) if s.active),
                              key=lambda s: (s.priority, s.id or 0), reverse=True)
        self.long_stay = sorted((t for t in (long_stay or []) if t.active), key=lambda t: t.min_nights, reverse=True)
        self.hs = hs

    # ------------------------------------------------------------ éléments du calcul
    def rate_period(self, room_type: RoomType, night: date) -> RatePeriod | None:
        for period in self.periods:
            if period.room_type_id == room_type.id and period.start_date <= night < period.end_date:
                return period
        return None

    def type_price(self, room_type: RoomType, night: date) -> int:
        """Prix du type pour la nuit : tarif saisonnier fixe, sinon prix de base (sans prix de chambre)."""
        period = self.rate_period(room_type, night)
        return period.price if period is not None else room_type.base_price

    def base_price(self, room_type: RoomType, night: date, room: Room | None = None) -> int:
        """Prix de référence hors prix négocié : prix de la chambre, sinon tarif fixe, sinon prix de base."""
        return self._reference(room_type, night, room)[0]

    def _reference(self, room_type: RoomType, night: date, room: Room | None) -> tuple[int, str, str]:
        if room is not None and room.price_override is not None:
            return room.price_override, "room", room.number
        period = self.rate_period(room_type, night)
        if period is not None:
            return period.price, "rate_period", period.name
        return room_type.base_price, "base", room_type.name

    def season_for(self, room_type: RoomType, night: date) -> Season | None:
        for season in self.seasons:
            if season.covers(night, room_type.id):
                return season
        return None

    def long_stay_for(self, stay_nights: int | None) -> LongStayDiscount | None:
        if not stay_nights:
            return None
        for tier in self.long_stay:
            if stay_nights >= tier.min_nights:
                return tier
        return None

    def is_weekend(self, night: date) -> bool:
        return night.weekday() in self.hs.weekend_set

    # ------------------------------------------------------------ calcul
    def breakdown(self, room_type: RoomType, night: date, company=None, room: Room | None = None,
                  stay_nights: int | None = None, override: int | None = None) -> NightPrice:
        """Détail complet du prix d'une nuitée (voir l'ordre de calcul dans la docstring du module)."""
        if room is not None and room_type is None:
            room_type = room.room_type
        result = NightPrice(night=night, source="", vat_rate=self.hs.vat_rate_accommodation,
                            room_number=room.number if room is not None else "")
        if override is not None:
            result.source = "override"
            amount = Fraction(override)
            result.steps.append(PriceStep("override", STEP_LABELS["override"], amount=round_dzd(amount),
                                          delta=round_dzd(amount)))
        else:
            reference, source, detail = self._reference(room_type, night, room)
            result.source = source
            amount = Fraction(reference)
            result.steps.append(PriceStep(source, STEP_LABELS[source], detail, amount=reference, delta=reference))

            def apply(code: str, percent: int, detail: str = "") -> None:
                nonlocal amount
                before = round_dzd(amount)
                amount = amount * Fraction(100 + percent, 100)
                after = round_dzd(amount)
                result.steps.append(PriceStep(code, STEP_LABELS[code], detail, percent, after, after - before))

            season = self.season_for(room_type, night)
            if season is not None and season.adjustment_percent:
                apply("season", season.adjustment_percent, season.name)
            if self.is_weekend(night) and self.hs.weekend_surcharge_percent:
                apply("weekend", self.hs.weekend_surcharge_percent)
            tier = self.long_stay_for(stay_nights)
            if tier is not None and tier.discount_percent:
                apply("long_stay", -tier.discount_percent, str(tier.min_nights))
            if company is not None and company.discount_percent:
                apply("company", -company.discount_percent, getattr(company, "name", "") or "")
        result.total_ttc = round_dzd(amount)
        result.total_ht, result.total_vat = split_vat(result.total_ttc, result.vat_rate)
        return result

    def price(self, room_type: RoomType, night: date, company=None, room: Room | None = None,
              stay_nights: int | None = None) -> int:
        """Prix TTC arrondi d'une nuitée (hors prix négocié de réservation)."""
        return self.breakdown(room_type, night, company, room=room, stay_nights=stay_nights).total_ttc

    def night_breakdown(self, res, segment, night: date) -> NightPrice:
        """Détail d'une nuit d'un séjour : la chambre du segment, le séjour complet et le prix négocié comptent."""
        room = segment.room
        return self.breakdown(room.room_type, night, res.company, room=room, stay_nights=res.nights,
                              override=res.nightly_rate_override)

    def night_price(self, res, segment, night: date) -> int:
        """Prix TTC d'une nuit d'un séjour (utilisé par l'audit de nuit et la projection du folio)."""
        return self.night_breakdown(res, segment, night).total_ttc

    def quote(self, room: Room, check_in: date, check_out: date, company=None,
              override: int | None = None) -> StayQuote:
        """Estimation d'un séjour [check_in, check_out) dans une chambre donnée."""
        stay_nights = max(0, (check_out - check_in).days)
        quote = StayQuote(check_in=check_in, check_out=check_out)
        tier = self.long_stay_for(stay_nights) if override is None else None
        quote.long_stay_percent = tier.discount_percent if tier is not None else 0
        night = check_in
        while night < check_out:
            quote.nights.append(self.breakdown(room.room_type, night, company, room=room, stay_nights=stay_nights,
                                               override=override))
            night += timedelta(days=1)
        return quote


async def load_calendar(db: AsyncSession, hs: HotelSettings, start: date | None = None,
                        end: date | None = None) -> RateCalendar:
    """Charge tarifs fixes, saisons actives et paliers long séjour actifs (sur [start, end) si précisé)."""
    periods_stmt = select(RatePeriod)
    seasons_stmt = select(Season).where(Season.active.is_(True))
    if start is not None and end is not None:
        periods_stmt = periods_stmt.where(RatePeriod.start_date < end, RatePeriod.end_date > start)
        seasons_stmt = seasons_stmt.where(Season.start_date < end, Season.end_date > start)
    periods = list((await db.scalars(periods_stmt)).unique().all())
    seasons = list((await db.scalars(seasons_stmt)).unique().all())
    tiers = list((await db.scalars(select(LongStayDiscount).where(LongStayDiscount.active.is_(True)))).all())
    return RateCalendar(periods, hs, seasons, tiers)
