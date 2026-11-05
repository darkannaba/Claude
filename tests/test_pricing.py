"""Gestion dynamique des tarifs : ordre de calcul, détail HT/TVA/TTC, immuabilité des nuitées postées et des
factures, routes d'administration (droits, CSRF, validations, historique), page en fr / en / ar."""
import asyncio
import re
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

from app.database import SessionLocal
from app.models import (
    AuditLog, Company, Guest, LongStayDiscount, PriceChange, RatePeriod, Room, RoomType, Season, Setting,
)
from app.services import booking, pricing_admin
from app.services.common import BusinessError, split_vat
from app.services.folio import build_folio
from app.services.invoicing import invoice_view
from app.services.loaders import load_reservation
from app.services.night_audit import post_stay_nights
from app.services.pricing import RateCalendar, load_calendar, round_dzd
from app.services.settings import HotelSettings, load_hotel_settings, now_of, today_of
from app.services.seed import ROOM_PLAN


def run(coro):
    return asyncio.run(coro)


async def _scalar(stmt):
    async with SessionLocal() as db:
        return await db.scalar(stmt)


async def _all(stmt):
    async with SessionLocal() as db:
        return list((await db.scalars(stmt)).unique().all())


# ---------------------------------------------------------------- outils de construction (sans base)
HS = HotelSettings(weekend_days="4,5", weekend_surcharge_percent=10, vat_rate_accommodation=19)
MONDAY = date(2031, 6, 30)       # lundi
FRIDAY = date(2031, 7, 4)        # vendredi (week-end)


def _type(price=10000, type_id=1):
    return RoomType(id=type_id, code=f"T{type_id}", name=f"Type {type_id}", base_price=price, max_occupancy=2)


def _room(room_type, override=None, number="101"):
    room = Room(id=int(number), number=number, floor=1, room_type_id=room_type.id, price_override=override)
    room.room_type = room_type
    return room


def _season(percent, start, end, priority=0, active=True, type_id=None, season_id=1, name="Saison"):
    return Season(id=season_id, name=name, level="high", start_date=start, end_date=end,
                  adjustment_percent=percent, priority=priority, active=active, room_type_id=type_id)


# ---------------------------------------------------------------- ordre de calcul (cas par cas)
def test_reference_price_order():
    room_type = _type(10000)
    period = RatePeriod(id=1, name="Fixe", room_type_id=1, start_date=MONDAY, end_date=MONDAY + timedelta(days=1),
                        price=12000, priority=0)
    calendar = RateCalendar([period], HS)
    plain = _room(room_type)
    special = _room(room_type, override=9000, number="102")
    # Prix de base du type, puis tarif saisonnier fixe, puis prix spécifique de la chambre (prioritaire)
    assert calendar.breakdown(room_type, MONDAY + timedelta(days=1), room=plain).source == "base"
    assert calendar.price(room_type, MONDAY + timedelta(days=1), room=plain) == 10000
    assert calendar.breakdown(room_type, MONDAY, room=plain).source == "rate_period"
    assert calendar.price(room_type, MONDAY, room=plain) == 12000
    detail = calendar.breakdown(room_type, MONDAY, room=special)
    assert detail.source == "room" and detail.total_ttc == 9000
    # Compatibilité : sans chambre, le calcul historique (tarif fixe > prix de base) est inchangé
    assert calendar.price(room_type, MONDAY) == 12000 and calendar.base_price(room_type, MONDAY) == 12000


def test_negotiated_rate_overrides_everything():
    room_type = _type(10000)
    calendar = RateCalendar([], HS, [_season(50, MONDAY, FRIDAY + timedelta(days=3))],
                            [LongStayDiscount(id=1, min_nights=2, discount_percent=20, active=True)])
    room = _room(room_type, override=9000)
    company = Company(name="Remise", discount_percent=10)
    detail = calendar.breakdown(room_type, FRIDAY, company, room=room, stay_nights=10, override=7500)
    assert detail.source == "override" and detail.total_ttc == 7500
    assert [step.code for step in detail.steps] == ["override"]   # aucun ajustement
    res = SimpleNamespace(company=company, nights=10, nightly_rate_override=7500)
    segment = SimpleNamespace(room=room)
    assert calendar.night_price(res, segment, FRIDAY) == 7500
    res.nightly_rate_override = None
    assert calendar.night_price(res, segment, FRIDAY) != 7500


def test_adjustments_order_season_weekend_long_stay_company():
    room_type = _type(10000)
    calendar = RateCalendar([], HS, [_season(20, MONDAY, FRIDAY + timedelta(days=3))],
                            [LongStayDiscount(id=1, min_nights=5, discount_percent=10, active=True)])
    company = Company(name="Sarl Test", discount_percent=5)
    detail = calendar.breakdown(room_type, FRIDAY, company, stay_nights=7)
    assert [s.code for s in detail.steps] == ["base", "season", "weekend", "long_stay", "company"]
    assert [s.amount for s in detail.steps] == [10000, 12000, 13200, 11880, 11286]
    assert [s.delta for s in detail.steps] == [10000, 2000, 1200, -1320, -594]
    assert [s.percent for s in detail.steps] == [None, 20, 10, -10, -5]
    assert detail.total_ttc == 11286
    # Lundi : pas de week-end ; séjour trop court : pas de remise long séjour
    monday = calendar.breakdown(room_type, MONDAY, company, stay_nights=4)
    assert [s.code for s in monday.steps] == ["base", "season", "company"]
    assert monday.total_ttc == 11400
    # Prix spécifique de la chambre : les mêmes ajustements s'appliquent ensuite
    room = _room(room_type, override=8000)
    assert calendar.price(room_type, FRIDAY, room=room) == round_dzd(8000 * 1.2 * 1.1)


def test_negative_season_and_selection_rules():
    std, sup = _type(10000, 1), _type(20000, 2)
    seasons = [
        _season(-10, MONDAY, MONDAY + timedelta(days=30), priority=0, season_id=1, name="Basse"),
        _season(30, MONDAY + timedelta(days=1), MONDAY + timedelta(days=2), priority=5, season_id=2, name="Fête"),
        _season(80, MONDAY, MONDAY + timedelta(days=30), priority=99, active=False, season_id=3, name="Inactive"),
        _season(15, MONDAY, MONDAY + timedelta(days=30), priority=3, type_id=2, season_id=4, name="Suites"),
    ]
    calendar = RateCalendar([], HS, seasons)
    assert calendar.price(std, MONDAY) == 9000                                 # saison négative, tous types
    assert calendar.season_for(std, MONDAY + timedelta(days=1)).name == "Fête"  # priorité la plus haute
    assert calendar.price(std, MONDAY + timedelta(days=1)) == 13000
    assert calendar.season_for(sup, MONDAY).name == "Suites"                   # saison propre au type
    assert calendar.price(sup, MONDAY) == 23000
    assert calendar.season_for(std, MONDAY + timedelta(days=30)) is None      # date de fin exclue
    assert all(s.name != "Inactive" for s in calendar.seasons)


def test_long_stay_tiers_by_total_nights():
    tiers = [LongStayDiscount(id=1, min_nights=7, discount_percent=5, active=True),
             LongStayDiscount(id=2, min_nights=14, discount_percent=10, active=True),
             LongStayDiscount(id=3, min_nights=30, discount_percent=40, active=False)]
    calendar = RateCalendar([], HS, [], tiers)
    assert calendar.long_stay_for(6) is None
    assert calendar.long_stay_for(7).discount_percent == 5
    assert calendar.long_stay_for(20).discount_percent == 10
    assert calendar.long_stay_for(45).discount_percent == 10    # palier inactif ignoré
    room = _room(_type(10000))
    quote = calendar.quote(room, MONDAY, MONDAY + timedelta(days=7))
    assert quote.nights_count == 7 and quote.long_stay_percent == 5
    # 5 nuits de semaine à 9 500 + vendredi et samedi : 10 000 × 1,10 × 0,95 = 10 450
    assert quote.total_ttc == 5 * 9500 + 2 * 10450
    assert quote.total_ht + quote.total_vat == quote.total_ttc


def test_rounding_only_at_the_end_half_up():
    room_type = _type(105)
    calendar = RateCalendar([], HS, [_season(10, MONDAY, FRIDAY + timedelta(days=3))])
    # 105 × 1,1 × 1,1 = 127,05 → 127 (un arrondi intermédiaire donnerait 116 × 1,1 = 127,6 → 128)
    assert calendar.price(room_type, FRIDAY) == 127
    assert round_dzd(499.5) == 500 and round_dzd(10.49) == 10 and round_dzd(-3) == 0
    assert pricing_admin.round_to_100(8849) == 8800 and pricing_admin.round_to_100(8850) == 8900
    assert pricing_admin.apply_percent(8000, 10, True) == 8800
    assert pricing_admin.apply_percent(8050, -3, False) == 7809       # 7 808,5 → 7 809
    assert pricing_admin.apply_percent(8050, -3, True) == 7800


def test_breakdown_ht_vat_ttc():
    room_type = _type(11900)
    calendar = RateCalendar([], HS)
    detail = calendar.breakdown(room_type, MONDAY)
    assert (detail.total_ttc, detail.total_ht, detail.total_vat, detail.vat_rate) == (11900, 10000, 1900, 19)
    weekend = calendar.breakdown(room_type, FRIDAY)
    assert weekend.total_ttc == 13090
    assert (weekend.total_ht, weekend.total_vat) == split_vat(13090, 19)
    assert weekend.total_ht + weekend.total_vat == weekend.total_ttc
    data = weekend.as_dict()
    assert data["steps"][-1]["code"] == "weekend" and data["total_ttc"] == 13090
    assert sum(step.delta for step in weekend.steps) == weekend.total_ttc
    no_vat = RateCalendar([], HotelSettings(vat_rate_accommodation=0)).breakdown(room_type, MONDAY)
    assert (no_vat.total_ht, no_vat.total_vat) == (11900, 0)


# ---------------------------------------------------------------- immuabilité (service)
async def _guest(db, id_number):
    guest = Guest(first_name="Tarif", last_name="Test", id_type="cni", id_number=id_number,
                  id_number_norm=id_number.upper().replace("-", ""))
    db.add(guest)
    await db.flush()
    return guest


async def _room_db(db, number):
    return await db.scalar(select(Room).where(Room.number == number))


async def test_posted_nights_stay_frozen_and_future_nights_follow(db):
    hs = (await load_hotel_settings(db)).model_copy(update={"weekend_surcharge_percent": 0})
    today = today_of(hs)
    now = now_of(hs)
    room = await _room_db(db, "104")
    old_type_price = room.room_type.base_price
    res = await booking.create_reservation(db, guest=await _guest(db, "PRICING-FREEZE-1"), room=room,
                                           check_in=today - timedelta(days=2), check_out=today + timedelta(days=3),
                                           hs=hs, user=None)
    res = await load_reservation(db, res.id)
    await booking.check_in(db, res, hs, None)
    calendar = await load_calendar(db, hs)
    post_stay_nights(res, calendar, hs, today, "night_audit", None, now)      # audit de nuit des 2 nuits passées
    posted = sorted((i.business_date, i.amount) for i in res.folio_items if i.kind == "room")
    assert len(posted) == 2 and all(amount == calendar.night_price(res, res.segments[0], night)
                                    for night, amount in posted)

    # Changements de tarif : prix spécifique de la chambre, saison active, palier long séjour, prix du type
    await pricing_admin.set_room_price(db, None, room.id, 20000, "test")
    db.add(Season(name="Test immuabilité", level="high", start_date=today - timedelta(days=10),
                  end_date=today + timedelta(days=10), adjustment_percent=50, priority=50, active=True))
    db.add(LongStayDiscount(min_nights=5, discount_percent=10, active=True))
    room.room_type.base_price = 1
    await db.flush()

    calendar = await load_calendar(db, hs)
    folio = build_folio(res, hs, calendar)
    assert sorted((line.business_date, line.amount) for line in folio.lines
                  if line.kind == "room" and line.posted) == posted           # nuitées postées figées
    future = [line for line in folio.lines if line.kind == "room" and not line.posted]
    assert len(future) == 3 and all(line.amount == 27000 for line in future)  # 20 000 × 1,5 × 0,9

    # Un nouvel audit ne re-poste pas les nuits déjà figées et poste la nuit du jour au nouveau tarif
    post_stay_nights(res, calendar, hs, today + timedelta(days=1), "night_audit", None, now)
    amounts = {i.business_date: i.amount for i in res.folio_items if i.kind == "room"}
    assert len(amounts) == 3 and amounts[today] == 27000
    assert all(amounts[night] == amount for night, amount in posted)

    # Le prix négocié de la réservation reste prioritaire pour les nuits non postées
    res.nightly_rate_override = 6500
    folio = build_folio(res, hs, calendar)
    assert [line.amount for line in folio.lines if line.kind == "room" and not line.posted] == [6500, 6500]
    assert sorted((line.business_date, line.amount) for line in folio.lines
                  if line.kind == "room" and line.posted)[:2] == posted
    room.room_type.base_price = old_type_price


async def test_issued_invoice_unchanged_after_price_change(db):
    hs = await load_hotel_settings(db)
    today = today_of(hs)
    room = await _room_db(db, "105")
    old_type_price = room.room_type.base_price
    res = await booking.create_reservation(db, guest=await _guest(db, "PRICING-INVOICE-1"), room=room,
                                           check_in=today - timedelta(days=2), check_out=today + timedelta(days=1), hs=hs,
                                           user=None)
    res = await load_reservation(db, res.id)
    await booking.check_in(db, res, hs, None)
    await booking.add_payment(db, res, amount=500000, kind="paiement", method="especes", hs=hs, user=None)
    invoice = await booking.check_out(db, res, hs, None)
    items_before = [(i.id, i.amount) for i in res.folio_items]
    totals_before = (invoice.total_ht, invoice.total_vat, invoice.total_ttc)

    room.room_type.base_price = 99999
    await pricing_admin.set_room_price(db, None, room.id, 55555, "après facture")
    db.add(Season(name="Après facture", level="high", start_date=today - timedelta(days=30),
                  end_date=today + timedelta(days=30), adjustment_percent=100, priority=60, active=True))
    await db.flush()
    calendar = await load_calendar(db, hs)
    view = invoice_view(res, hs, calendar)
    assert view.is_final and (view.total_ht, view.total_vat, view.total_ttc) == totals_before
    assert [(i.id, i.amount) for i in res.folio_items] == items_before
    assert build_folio(res, hs, calendar).total_ttc == invoice.total_ttc
    room.room_type.base_price = old_type_price


async def test_default_seasons_seed_is_idempotent(db):
    seeded = (await db.scalars(select(Season).where(Season.name.in_(
        ["Été – haute saison balnéaire", "Avant-saison estivale", "Hiver – basse saison"])))).all()
    assert len(seeded) == 3 and not any(s.active for s in seeded)      # exemples inactifs : prix inchangés
    assert {s.level for s in seeded} == {"high", "medium", "low"}
    assert all(s.end_date > s.start_date for s in seeded)
    assert await pricing_admin.ensure_pricing_defaults(db) == 0
    await db.execute(delete(Season))
    await db.execute(delete(PriceChange).where(PriceChange.target_type == "season"))
    assert await pricing_admin.ensure_pricing_defaults(db) == 3
    # L'administrateur a supprimé toutes les saisons : rien n'est recréé
    await db.execute(delete(Season))
    db.add(PriceChange(target_type="season", target_id="1", field="season", old_value="x", new_value="supprimée"))
    await db.flush()
    assert await pricing_admin.ensure_pricing_defaults(db) == 0


async def test_service_validation_errors(db):
    with pytest.raises(BusinessError, match="entre 0 et 10 000 000"):
        pricing_admin.parse_price("-1")
    with pytest.raises(BusinessError, match="entre 0 et 10 000 000"):
        pricing_admin.parse_price("12,5")
    assert pricing_admin.parse_price("", allow_empty=True) is None
    assert pricing_admin.parse_price("9 500") == 9500
    with pytest.raises(BusinessError, match="-90 et 300"):
        pricing_admin.parse_percent("301")
    with pytest.raises(BusinessError, match="postérieure"):
        await pricing_admin.create_season(db, None, {"name": "X", "level": "high", "start_date": "2031-07-10",
                                                     "end_date": "2031-07-01", "adjustment_percent": "10"})
    with pytest.raises(BusinessError, match="Niveau"):
        await pricing_admin.create_season(db, None, {"name": "X", "level": "extrême", "start_date": "2031-07-01",
                                                     "end_date": "2031-07-10", "adjustment_percent": "10"})
    with pytest.raises(BusinessError, match="366"):
        await pricing_admin.create_season(db, None, {"name": "X", "level": "low", "start_date": "2031-01-01",
                                                     "end_date": "2033-01-01", "adjustment_percent": "10"})
    with pytest.raises(BusinessError, match="1 et 90"):
        await pricing_admin.create_tier(db, None, {"min_nights": "7", "discount_percent": "95"})
    with pytest.raises(BusinessError, match="2 et 365"):
        await pricing_admin.create_tier(db, None, {"min_nights": "1", "discount_percent": "5"})


# ---------------------------------------------------------------- routes d'administration (HTTP)
def _snapshot_pricing():
    async def snap():
        async with SessionLocal() as db:
            types = {rt.id: rt.base_price for rt in (await db.scalars(select(RoomType))).all()}
            rooms = {r.id: r.price_override for r in (await db.scalars(select(Room))).unique().all()}
            seasons = {s.id for s in (await db.scalars(select(Season))).unique().all()}
            tiers = {t.id for t in (await db.scalars(select(LongStayDiscount))).all()}
            settings = {s.key: s.value for s in (await db.scalars(select(Setting).where(
                Setting.key.in_(["weekend_days", "weekend_surcharge_percent"])))).all()}
            return types, rooms, seasons, tiers, settings
    return run(snap())


def _restore_pricing(snapshot):
    types, rooms, seasons, tiers, settings = snapshot

    async def restore():
        async with SessionLocal() as db:
            for rt in (await db.scalars(select(RoomType))).all():
                rt.base_price = types.get(rt.id, rt.base_price)
            for room in (await db.scalars(select(Room))).unique().all():
                room.price_override = rooms.get(room.id)
            await db.execute(delete(Season).where(Season.id.not_in(seasons)))
            await db.execute(delete(LongStayDiscount).where(LongStayDiscount.id.not_in(tiers)))
            for row in (await db.scalars(select(Setting).where(Setting.key.in_(list(settings))))).all():
                row.value = settings[row.key]
            await db.commit()
    run(restore())


@pytest.fixture()
def pricing_state(app):
    """Les routes committent : on restaure prix, saisons, paliers et week-end pour les autres tests."""
    snapshot = _snapshot_pricing()
    yield snapshot
    _restore_pricing(snapshot)


def _ids():
    async def fetch():
        async with SessionLocal() as db:
            types = {rt.code: rt.id for rt in (await db.scalars(select(RoomType))).all()}
            rooms = {r.number: r.id for r in (await db.scalars(select(Room))).unique().all()}
            return types, rooms
    return run(fetch())


def test_reception_gets_403_on_every_pricing_route(reception, housekeeper, anonymous):
    for path in ["/admin/pricing", "/admin/pricing/simulate?room_id=1&check_in=2031-01-01&check_out=2031-01-02"]:
        assert reception.get(path, follow_redirects=False).status_code == 403
        assert housekeeper.get(path, follow_redirects=False).status_code == 403
        assert anonymous.get(path, follow_redirects=False).status_code == 303   # connexion requise
    posts = [
        ("/admin/pricing/bulk", {"mode": "percent", "percent": "10"}),
        ("/admin/pricing/rooms/1", {"price_override": "1"}),
        ("/admin/pricing/seasons", {"name": "X", "start_date": "2031-01-01", "end_date": "2031-01-02",
                                    "adjustment_percent": "10"}),
        ("/admin/pricing/seasons/1", {"name": "X"}),
        ("/admin/pricing/seasons/1/delete", {}),
        ("/admin/pricing/long-stay", {"min_nights": "7", "discount_percent": "5"}),
        ("/admin/pricing/long-stay/1", {"min_nights": "7", "discount_percent": "5"}),
        ("/admin/pricing/long-stay/1/delete", {}),
        ("/admin/pricing/weekend", {"weekend_day": "4", "weekend_surcharge_percent": "50"}),
    ]
    changes_before = run(_scalar(select(func.count(PriceChange.id))))
    for url, data in posts:
        assert reception.form(url, data).status_code == 403, url
        assert housekeeper.form(url, data).status_code == 403, url
    assert run(_scalar(select(func.count(PriceChange.id)))) == changes_before


def test_csrf_required_on_pricing_routes(admin, pricing_state):
    _, rooms = _ids()
    for url in ["/admin/pricing/bulk", f"/admin/pricing/rooms/{rooms['106']}", "/admin/pricing/seasons",
                "/admin/pricing/long-stay", "/admin/pricing/weekend"]:
        assert admin.post(url, data={"mode": "percent", "percent": "50", "price_override": "1"},
                          follow_redirects=False).status_code == 403, url
        assert admin.post(url, data={"csrf_token": "faux", "percent": "50"}, follow_redirects=False).status_code == 403
    assert run(_scalar(select(Room.price_override).where(Room.id == rooms["106"]))) is None


def test_admin_bulk_updates(admin, pricing_state):
    types, rooms = _ids()
    base = pricing_state[0]
    # +10 % sur le type STD, arrondi à 100 DA, prix spécifiques inclus
    assert admin.form(f"/admin/pricing/rooms/{rooms['107']}", {"price_override": "9050"}).status_code == 303
    response = admin.form("/admin/pricing/bulk", {"mode": "percent", "percent": "10", "room_type_id": str(types["STD"]),
                                                  "round_100": "1", "include_rooms": "1", "reason": "Révision"})
    assert response.status_code == 303
    expected_std = pricing_admin.apply_percent(base[types["STD"]], 10, True)
    assert run(_scalar(select(RoomType.base_price).where(RoomType.id == types["STD"]))) == expected_std
    assert run(_scalar(select(RoomType.base_price).where(RoomType.id == types["SUP"]))) == base[types["SUP"]]
    assert run(_scalar(select(Room.price_override).where(Room.id == rooms["107"]))) == 10000   # 9 955 → 10 000
    change = run(_scalar(select(PriceChange).where(PriceChange.target_type == "room_type", PriceChange.target_id == "STD")
                         .order_by(PriceChange.id.desc())))
    assert change.old_value == str(base[types["STD"]]) and change.new_value == str(expected_std)
    assert change.reason == "Révision" and change.username == "admin"
    assert run(_scalar(select(AuditLog.id).where(AuditLog.action == "pricing_bulk_update")))

    # -5 % global, sans arrondi
    before = {code: run(_scalar(select(RoomType.base_price).where(RoomType.id == type_id)))
              for code, type_id in types.items()}
    assert admin.form("/admin/pricing/bulk", {"mode": "percent", "percent": "-5"}).status_code == 303
    for code, type_id in types.items():
        assert run(_scalar(select(RoomType.base_price).where(RoomType.id == type_id))) == \
            pricing_admin.apply_percent(before[code], -5, False)

    # Prix de base par type, arrondi à 100 DA
    assert admin.form("/admin/pricing/bulk", {"mode": "base", f"base_price_{types['SUP']}": "12345",
                                              "round_100": "1"}).status_code == 303
    assert run(_scalar(select(RoomType.base_price).where(RoomType.id == types["SUP"]))) == 12300

    # Validations : pourcentage hors bornes, prix négatif, mode inconnu → rien ne change, message d'erreur
    sup_price = 12300
    admin.form("/admin/pricing/bulk", {"mode": "percent", "percent": "500"})
    assert "entre -90 et 300" in admin.get("/admin/pricing").text
    admin.form("/admin/pricing/bulk", {"mode": "base", f"base_price_{types['SUP']}": "-10"})
    assert "entre 0 et 10 000 000" in admin.get("/admin/pricing").text
    admin.form("/admin/pricing/bulk", {"mode": "autre"})
    assert "Type de mise à jour invalide." in admin.get("/admin/pricing").text
    assert run(_scalar(select(RoomType.base_price).where(RoomType.id == types["SUP"]))) == sup_price


def test_admin_room_by_room_price(admin, pricing_state):
    _, rooms = _ids()
    room_id = rooms["108"]
    assert admin.form(f"/admin/pricing/rooms/{room_id}", {"price_override": "9500", "reason": "Vue mer"}).status_code == 303
    assert run(_scalar(select(Room.price_override).where(Room.id == room_id))) == 9500
    page = admin.get("/admin/pricing").text
    assert 'value="9500"' in page
    assert admin.form(f"/admin/pricing/rooms/{room_id}", {"price_override": "-1"}).status_code == 303
    assert run(_scalar(select(Room.price_override).where(Room.id == room_id))) == 9500
    assert admin.form(f"/admin/pricing/rooms/{room_id}", {"action": "reset", "price_override": "9500"}).status_code == 303
    assert run(_scalar(select(Room.price_override).where(Room.id == room_id))) is None
    history = run(_all(select(PriceChange).where(PriceChange.target_type == "room", PriceChange.target_id == "108")
                       .order_by(PriceChange.id)))
    assert [(h.old_value, h.new_value) for h in history[-2:]] == [("—", "9500"), ("9500", "—")]
    assert history[-2].reason == "Vue mer"
    assert admin.form("/admin/pricing/rooms/999999", {"price_override": "1"}).status_code == 303
    assert "Chambre introuvable." in admin.get("/admin/pricing").text


def test_admin_seasons_crud(admin, pricing_state):
    types, _ = _ids()
    data = {"name": "Festival d'Annaba", "level": "high", "start_date": "2031-07-10", "end_date": "2031-07-20",
            "adjustment_percent": "25", "priority": "7", "room_type_id": str(types["SUITE"]), "active": "1"}
    assert admin.form("/admin/pricing/seasons", data).status_code == 303
    season = run(_scalar(select(Season).where(Season.name == "Festival d'Annaba")))
    assert season.adjustment_percent == 25 and season.room_type_id == types["SUITE"] and season.active
    assert "Festival d&#39;Annaba" in admin.get("/admin/pricing").text

    update = {key: value for key, value in data.items() if key != "active"}   # case « Active » décochée
    update.update(adjustment_percent="-15", room_type_id="")
    assert admin.form(f"/admin/pricing/seasons/{season.id}", update).status_code == 303
    season = run(_scalar(select(Season).where(Season.id == season.id)))
    assert season.adjustment_percent == -15 and season.room_type_id is None and not season.active

    # Dates incohérentes et pourcentage hors bornes : refusés
    admin.form("/admin/pricing/seasons", {**data, "name": "Invalide", "end_date": "2031-07-01"})
    assert "La date de fin doit être postérieure à la date de début." in admin.get("/admin/pricing").text
    admin.form("/admin/pricing/seasons", {**data, "name": "Invalide", "adjustment_percent": "-95"})
    assert run(_scalar(select(Season.id).where(Season.name == "Invalide"))) is None

    assert admin.form(f"/admin/pricing/seasons/{season.id}/delete", {"reason": "annulé"}).status_code == 303
    assert run(_scalar(select(Season.id).where(Season.id == season.id))) is None
    logs = run(_all(select(PriceChange).where(PriceChange.target_type == "season",
                                              PriceChange.target_id == str(season.id)).order_by(PriceChange.id)))
    assert len(logs) == 3 and logs[-1].new_value == "supprimée" and logs[-1].reason == "annulé"
    assert run(_scalar(select(AuditLog.id).where(AuditLog.action == "season_deleted")))


def test_admin_long_stay_tiers_crud(admin, pricing_state):
    assert admin.form("/admin/pricing/long-stay", {"min_nights": "7", "discount_percent": "5", "active": "1"}).status_code == 303
    tier = run(_scalar(select(LongStayDiscount).where(LongStayDiscount.min_nights == 7)))
    assert tier.discount_percent == 5 and tier.active
    admin.form("/admin/pricing/long-stay", {"min_nights": "7", "discount_percent": "8", "active": "1"})
    assert "Un palier existe déjà pour ce nombre de nuits." in admin.get("/admin/pricing").text
    admin.form("/admin/pricing/long-stay", {"min_nights": "10", "discount_percent": "0"})
    assert run(_scalar(select(LongStayDiscount.id).where(LongStayDiscount.min_nights == 10))) is None
    assert admin.form(f"/admin/pricing/long-stay/{tier.id}", {"min_nights": "8", "discount_percent": "6"}).status_code == 303
    tier = run(_scalar(select(LongStayDiscount).where(LongStayDiscount.id == tier.id)))
    assert (tier.min_nights, tier.discount_percent, tier.active) == (8, 6, False)
    assert admin.form(f"/admin/pricing/long-stay/{tier.id}/delete").status_code == 303
    assert run(_scalar(select(LongStayDiscount.id).where(LongStayDiscount.id == tier.id))) is None
    assert run(_scalar(select(func.count(PriceChange.id)).where(PriceChange.target_type == "long_stay",
                                                                PriceChange.target_id == str(tier.id)))) == 3


def test_admin_weekend_surcharge(admin, pricing_state):
    response = admin.post("/admin/pricing/weekend", data={"csrf_token": admin.csrf, "weekend_day": ["4", "5", "6"],
                                                          "weekend_surcharge_percent": "15", "reason": "Été"},
                          follow_redirects=False)
    assert response.status_code == 303

    async def weekend():
        async with SessionLocal() as db:
            hs = await load_hotel_settings(db)
            return hs.weekend_days, hs.weekend_surcharge_percent
    assert run(weekend()) == ("4,5,6", 15)
    changes = run(_all(select(PriceChange).where(PriceChange.target_type == "weekend").order_by(PriceChange.id)))
    assert {(c.field, c.new_value) for c in changes[-2:]} == {("weekend_days", "4,5,6"), ("weekend_surcharge_percent", "15")}
    admin.form("/admin/pricing/weekend", {"weekend_day": "4", "weekend_surcharge_percent": "400"})
    assert "entre -90 et 300" in admin.get("/admin/pricing").text
    admin.form("/admin/pricing/weekend", {"weekend_day": "9", "weekend_surcharge_percent": "10"})
    assert run(weekend()) == ("4,5,6", 15)
    # Aucun jour coché : pas de majoration
    assert admin.form("/admin/pricing/weekend", {"weekend_surcharge_percent": "10"}).status_code == 303
    assert run(weekend()) == ("", 10)


def _next_weekday(start: date, weekday: int) -> date:
    return start + timedelta(days=(weekday - start.weekday()) % 7)


def test_simulator_and_reservation_estimates_use_room_price(admin, reception, pricing_state):
    _, rooms = _ids()
    monday = _next_weekday(date.today() + timedelta(days=60), 0)
    assert admin.form(f"/admin/pricing/rooms/{rooms['106']}", {"price_override": "7777"}).status_code == 303
    url = f"/admin/pricing/simulate?room_id={rooms['106']}&check_in={monday}&check_out={monday + timedelta(days=2)}"
    data = admin.get(url).json()
    assert data["ok"] and data["room"] == "106" and data["nights_count"] == 2
    assert [n["total_ttc"] for n in data["nights"]] == [7777, 7777]          # lundi + mardi, aucune saison active
    assert data["total_ttc"] == 15554 and data["total_ht"] + data["total_vat"] == 15554
    assert data["nights"][0]["steps"][0]["code"] == "room"

    async def add_company():
        async with SessionLocal() as db:
            company = Company(name="Simulation tarifs SARL", discount_percent=10)
            db.add(company)
            await db.commit()
            return company.id
    company_id = run(add_company())
    try:
        data = admin.get(f"{url}&company_id={company_id}").json()
        assert data["company"] == "Simulation tarifs SARL"
        assert [s["code"] for s in data["nights"][0]["steps"]] == ["room", "company"]
        assert data["nights"][0]["total_ttc"] == 6999                          # 7 777 × 0,9 = 6 999,3
    finally:
        async def drop_company():
            async with SessionLocal() as db:
                await db.execute(delete(Company).where(Company.id == company_id))
                await db.commit()
        run(drop_company())

    bad = admin.get(f"/admin/pricing/simulate?room_id={rooms['106']}&check_in={monday}&check_out={monday}")
    assert bad.status_code == 400 and not bad.json()["ok"]
    assert admin.get("/admin/pricing/simulate?room_id=999999&check_in=2031-01-01&check_out=2031-01-02").status_code == 400

    page = admin.get(f"/admin/pricing?sim_room={rooms['106']}&sim_in={monday}&sim_out={monday + timedelta(days=2)}").text
    assert 'id="simulation-result"' in page and 'data-total="15554"' in page
    page = admin.get(f"/admin/pricing?sim_room={rooms['106']}&sim_in={monday}&sim_out=bad").text
    assert "Date invalide (format AAAA-MM-JJ)." in page

    # Formulaire de réservation : l'estimation tient compte du prix spécifique de la chambre
    form = reception.get(f"/reservations/new?check_in={monday}&check_out={monday + timedelta(days=1)}").text
    assert "7\u00a0777\u00a0DA" in form


def test_pricing_page_lists_rooms_with_final_price(admin, pricing_state):
    _, rooms = _ids()
    total_rooms = sum(len(numbers) for _, _, numbers in ROOM_PLAN)
    monday = _next_weekday(date.today() + timedelta(days=60), 0)
    assert admin.form(f"/admin/pricing/rooms/{rooms['106']}", {"price_override": "11900"}).status_code == 303
    page = admin.get(f"/admin/pricing?day={monday}").text
    assert page.count('x-data="pricingRow(') >= total_rooms == 30
    assert 'id="pricing-data"' in page and "<script>" not in page
    row = page[page.index(f"pricingRow('{rooms['106']}'"):]
    row = row[:row.index("</tr>")]
    # 11 900 TTC = 10 000 HT + 1 900 TVA (19 %)
    assert "11 900 DA" in row and "10 000 DA" in row and "1 900 DA" in row
    data = re.search(r'<script type="application/json" id="pricing-data">(.*?)</script>', page, re.S).group(1)
    assert f'"{rooms["106"]}"' in data
    assert 'href="/admin/pricing"' in page and 'href="/admin/rates"' in page   # lien ajouté, ancien conservé


@pytest.mark.parametrize("lang, title, rtl", [("fr", "Gestion des tarifs", False), ("en", "Rate management", False),
                                              ("ar", "إدارة الأسعار", True)])
def test_pricing_page_renders_in_each_language(app, lang, title, rtl):
    from tests.conftest import make_client
    client = make_client(app, "admin")
    assert client.form("/lang", {"lang": lang, "next": "/admin/pricing"}).status_code == 303
    response = client.get("/admin/pricing")
    assert response.status_code == 200
    assert title in response.text
    assert f'<html lang="{lang}" dir="{'rtl' if rtl else 'ltr'}"' in response.text
    if lang != "fr":
        assert "Simulateur de prix" not in response.text and "Mise à jour globale rapide" not in response.text
    # Message d'erreur métier traduit
    client.form("/admin/pricing/long-stay", {"min_nights": "1", "discount_percent": "5"})
    message = {"fr": "Le nombre de nuits minimum doit être compris entre 2 et 365.",
               "en": "The minimum number of nights must be between 2 and 365.",
               "ar": "يجب أن يكون الحد الأدنى لعدد الليالي بين 2 و365."}[lang]
    assert message in client.get("/admin/pricing").text
