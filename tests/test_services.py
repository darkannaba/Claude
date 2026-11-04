"""Règles métier testées au niveau des services (sans HTTP)."""
import socket
import threading
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models import Company, Guest, PosItem, RatePeriod, Room, RoomType
from app.services import backup, booking, crm, night_audit, pos, printing, reports, scheduler, seed
from app.services.amount_words import amount_in_words, number_to_words_fr
from app.services.common import BusinessError, like_pattern, split_vat, to_date, to_int
from app.services.folio import build_folio
from app.services.invoicing import invoice_view
from app.services.loaders import load_pos_order, load_reservation
from app.services.pricing import RateCalendar, load_calendar
from app.services.security import LoginThrottle, hash_password, needs_rehash, password_problems, verify_password
from app.services.settings import HotelSettings, load_hotel_settings, save_hotel_settings, settings_form, today_of


async def _room(db, number):
    return await db.scalar(select(Room).where(Room.number == number))


async def _guest(db, id_number, **kwargs):
    guest = Guest(first_name=kwargs.pop("first_name", "Test"), last_name=kwargs.pop("last_name", "Client"),
                  id_type="cni", id_number=id_number, id_number_norm=id_number.upper().replace("-", ""), **kwargs)
    db.add(guest)
    await db.flush()
    return guest


# ---------------------------------------------------------------- outils
def test_common_helpers():
    assert to_int("1 500") == 1500 and to_int("", default=3) == 3
    with pytest.raises(BusinessError):
        to_int("abc")
    with pytest.raises(BusinessError):
        to_int("5", minimum=10)
    with pytest.raises(BusinessError):
        to_int("50", maximum=10)
    with pytest.raises(BusinessError):
        to_int("")
    assert to_date("2030-01-02") == date(2030, 1, 2) and to_date("", required=False) is None
    with pytest.raises(BusinessError):
        to_date("02/01/2030")
    with pytest.raises(BusinessError):
        to_date("")
    assert like_pattern("50%_x") == "%50\\%\\_x%"
    assert split_vat(11900, 19) == (10000, 1900) and split_vat(500, 0) == (500, 0)


@pytest.mark.parametrize("number,words", [
    (0, "zéro"), (1, "un"), (17, "dix-sept"), (21, "vingt et un"), (71, "soixante et onze"), (75, "soixante-quinze"),
    (80, "quatre-vingts"), (81, "quatre-vingt-un"), (99, "quatre-vingt-dix-neuf"), (100, "cent"), (200, "deux cents"),
    (201, "deux cent un"), (1000, "mille"), (31800, "trente et un mille huit cents"),
    (200000, "deux cent mille"), (80000, "quatre-vingt mille"), (2_000_000, "deux millions"),
    (1_250_000_000, "un milliard deux cent cinquante millions"), (-5, "moins cinq"),
])
def test_number_to_words(number, words):
    assert number_to_words_fr(number) == words


def test_amount_in_words():
    assert amount_in_words(1500) == "Mille cinq cents dinars algériens"


def test_password_security():
    stored = hash_password("Secret-2026")
    assert verify_password("Secret-2026", stored) and not verify_password("autre", stored)
    assert not verify_password("x", "pas-un-hash") and needs_rehash("pas-un-hash")
    assert not needs_rehash(stored)
    assert password_problems("court") and not password_problems("Long-enough-2026")
    throttle = LoginThrottle(2, 60)
    throttle.failure("a")
    assert not throttle.is_locked("a")
    throttle.failure("a")
    assert throttle.is_locked("a")
    throttle.success("a")
    assert not throttle.is_locked("a")


# ---------------------------------------------------------------- paramètres
async def test_settings_validation_and_form(db):
    hs = await load_hotel_settings(db)
    assert hs.currency_code == "DZD" and 4 in hs.weekend_set
    groups = settings_form(hs)
    assert any(f["name"] == "print_mode" and f["kind"] == "select" for g in groups for f in g["fields"])
    with pytest.raises(BusinessError):
        await save_hotel_settings(db, {"s_weekend_days": "lundi"})
    with pytest.raises(ValueError):
        HotelSettings(timezone="Lune/Base")


# ---------------------------------------------------------------- tarifs et folio
def test_rate_calendar_priority_weekend_and_discount():
    hs = HotelSettings(weekend_days="4,5", weekend_surcharge_percent=10)
    room_type = RoomType(id=1, code="X", name="X", base_price=10000, max_occupancy=2)
    periods = [
        RatePeriod(id=1, name="Saison", room_type_id=1, start_date=date(2031, 7, 1), end_date=date(2031, 8, 1), price=12000, priority=0),
        RatePeriod(id=2, name="Fête", room_type_id=1, start_date=date(2031, 7, 5), end_date=date(2031, 7, 6), price=15000, priority=5),
    ]
    calendar = RateCalendar(periods, hs)
    monday = date(2031, 6, 30)
    assert monday.weekday() == 0 and calendar.price(room_type, monday) == 10000
    assert calendar.price(room_type, date(2031, 7, 1)) == 12000               # mardi, saison
    assert calendar.price(room_type, date(2031, 7, 4)) == 13200               # vendredi : +10 %
    assert calendar.price(room_type, date(2031, 7, 5)) == 16500               # samedi, priorité 5 : 15000 +10 %
    company = Company(name="Remise", discount_percent=20)
    assert calendar.price(room_type, monday, company) == 8000


async def test_night_audit_freezes_prices_against_later_rate_changes(db):
    hs = await load_hotel_settings(db)
    today = today_of(hs)
    room = await _room(db, "207")
    guest = await _guest(db, "FREEZE-1")
    res = await booking.create_reservation(db, guest=guest, room=room, check_in=today - timedelta(days=2),
                                           check_out=today + timedelta(days=2), hs=hs, user=None, adults=1)
    res = await load_reservation(db, res.id)
    await booking.check_in(db, res, hs, None)
    # Selon l'ordre des tests, hier peut déjà être clôturé : on clôture alors aujourd'hui.
    audit_day = today if await night_audit.is_day_closed(db, today - timedelta(days=1)) else today - timedelta(days=1)
    run = await night_audit.run_night_audit(db, audit_day, hs)
    own_nights = [i for i in res.folio_items if i.kind == "room"]
    assert len(own_nights) == (audit_day - res.check_in).days + 1  # rattrapage des nuits précédentes inclus
    assert run.rooms_posted >= len(own_nights)
    with pytest.raises(BusinessError, match="déjà clôturée"):
        await night_audit.run_night_audit(db, audit_day, hs)
    with pytest.raises(BusinessError, match="future"):
        await night_audit.run_night_audit(db, today + timedelta(days=1), hs)

    posted_before = sorted(i.amount for i in res.folio_items if i.kind == "room")
    room.room_type.base_price = 99999                    # changement de tarif après clôture
    await db.flush()
    calendar = await load_calendar(db, hs)
    folio = build_folio(res, hs, calendar)
    posted_after = sorted(line.amount for line in folio.lines if line.kind == "room" and line.posted)
    assert posted_after == posted_before                 # les nuits figées ne bougent pas
    assert any(line.amount == 99999 for line in folio.lines if not line.posted)  # seules les nuits futures suivent

    # Départ : nuits restantes figées au nouveau tarif, facture définitive verrouillée
    await booking.add_payment(db, res, amount=500000, kind="paiement", method="virement", hs=hs, user=None)
    invoice = await booking.check_out(db, res, hs, None)
    assert invoice.total_ttc == sum(i.amount for i in res.folio_items)
    assert all(i.invoice_id == invoice.id for i in res.folio_items)
    view = invoice_view(res, hs, None)
    assert view.is_final and view.number == invoice.number
    room.room_type.base_price = 11000
    await db.flush()


async def test_auto_no_show_and_pending_dates(db):
    hs = await load_hotel_settings(db)
    today = today_of(hs)
    room = await _room(db, "208")
    guest = await _guest(db, "NOSHOW-1")
    res = await booking.create_reservation(db, guest=guest, room=room, check_in=today - timedelta(days=3),
                                           check_out=today + timedelta(days=1), hs=hs, user=None)
    pending = await night_audit.pending_audit_dates(db, hs)
    assert all(d < today for d in pending)
    target = today - timedelta(days=20)
    assert not await night_audit.is_day_closed(db, target)
    runs = []
    for day in (target, today - timedelta(days=3)):
        if not await night_audit.is_day_closed(db, day):
            runs.append(await night_audit.run_night_audit(db, day, hs))
    assert any(r.no_shows for r in runs)
    assert (await load_reservation(db, res.id)).status == "no_show"


# ---------------------------------------------------------------- réservations
async def test_booking_rules_and_errors(db):
    hs = await load_hotel_settings(db)
    today = today_of(hs)
    room = await _room(db, "209")
    guest = await _guest(db, "RULES-1")
    start = today + timedelta(days=60)
    with pytest.raises(BusinessError, match="postérieure"):
        await booking.create_reservation(db, guest=guest, room=room, check_in=start, check_out=start, hs=hs, user=None)
    with pytest.raises(BusinessError, match="365"):
        await booking.create_reservation(db, guest=guest, room=room, check_in=start, check_out=start + timedelta(days=400), hs=hs, user=None)
    with pytest.raises(BusinessError, match="Capacité"):
        await booking.create_reservation(db, guest=guest, room=room, check_in=start, check_out=start + timedelta(days=1),
                                         hs=hs, user=None, adults=3, children=2)
    with pytest.raises(BusinessError, match="adulte"):
        await booking.create_reservation(db, guest=guest, room=room, check_in=start, check_out=start + timedelta(days=1),
                                         hs=hs, user=None, adults=0)
    res = await booking.create_reservation(db, guest=guest, room=room, check_in=start, check_out=start + timedelta(days=3),
                                           hs=hs, user=None)
    with pytest.raises(BusinessError, match="pas disponible"):
        await booking.create_reservation(db, guest=guest, room=room, check_in=start + timedelta(days=1),
                                         check_out=start + timedelta(days=2), hs=hs, user=None)
    assert room.id not in {r.id for r in await booking.free_rooms(db, start, start + timedelta(days=1))}
    res = await load_reservation(db, res.id)
    with pytest.raises(BusinessError, match="Arrivée prévue"):
        await booking.check_in(db, res, hs, None)
    with pytest.raises(BusinessError, match="séjour en cours"):
        await booking.check_out(db, res, hs, None)
    with pytest.raises(BusinessError, match="déjà"):
        await booking.change_room(db, res, room, start, hs, None)
    with pytest.raises(BusinessError, match="hors de la période"):
        await booking.change_room(db, res, await _room(db, "210"), start + timedelta(days=10), hs, None)
    with pytest.raises(BusinessError):
        await booking.add_payment(db, res, amount=0, kind="acompte", method="cib", hs=hs, user=None)
    with pytest.raises(BusinessError):
        await booking.add_payment(db, res, amount=10, kind="inconnu", method="cib", hs=hs, user=None)
    with pytest.raises(BusinessError):
        await booking.add_payment(db, res, amount=10, kind="acompte", method="bitcoin", hs=hs, user=None)
    with pytest.raises(BusinessError):
        await booking.add_extra(db, res, description="", amount=10, vat_rate=19, hs=hs, user=None)
    with pytest.raises(BusinessError):
        await booking.add_extra(db, res, description="X", amount=0, vat_rate=19, hs=hs, user=None)
    await booking.cancel(db, res, hs, None, "test")
    with pytest.raises(BusinessError):
        await booking.cancel(db, res, hs, None)
    with pytest.raises(BusinessError):
        await booking.add_payment(db, res, amount=100, kind="paiement", method="cib", hs=hs, user=None)
    await booking.add_payment(db, res, amount=100, kind="remboursement", method="especes", hs=hs, user=None)
    with pytest.raises(BusinessError):
        await booking.update_reservation(db, res, room=room, check_in=start, check_out=start + timedelta(days=1),
                                         adults=1, children=0, company=None, hs=hs, user=None)


async def test_void_rules_reversal_and_identity(db):
    hs = await load_hotel_settings(db)
    today = today_of(hs)
    room = await _room(db, "210")
    guest = await _guest(db, "")
    guest.id_number = ""
    res = await booking.create_reservation(db, guest=guest, room=room, check_in=today, check_out=today + timedelta(days=2),
                                           hs=hs, user=None)
    res = await load_reservation(db, res.id)
    with pytest.raises(BusinessError, match="Pièce d'identité"):
        await booking.check_in(db, res, hs, None)
    guest.id_number = "VOID-1"
    await booking.check_in(db, res, hs, None)
    from app.models import User
    admin = await db.scalar(select(User).where(User.username == "admin"))
    reception = await db.scalar(select(User).where(User.username == "reception"))
    run = await night_audit.run_night_audit(db, today, hs, admin)  # clôture du jour : la nuit d'aujourd'hui est figée
    assert run.rooms_posted >= 1
    assert [i.business_date for i in res.folio_items if i.kind == "room"] == [today]
    extra = await booking.add_extra(db, res, description="Minibar", amount=900, vat_rate=19, hs=hs, user=reception)
    await db.flush()
    await booking.void_item(db, res, extra.id, hs, reception, "erreur")  # journée clôturée : contre-passation
    assert any(i.reversal_of_id == extra.id and i.amount == -900 for i in res.folio_items)
    with pytest.raises(BusinessError, match="déjà contre-passée"):
        await booking.void_item(db, res, extra.id, hs, reception)
    room_item = next(i for i in res.folio_items if i.kind == "room")
    with pytest.raises(BusinessError, match="administrateur"):
        await booking.void_item(db, res, room_item.id, hs, reception)
    await booking.void_item(db, res, room_item.id, hs, admin, "geste commercial")
    with pytest.raises(BusinessError, match="introuvable"):
        await booking.void_item(db, res, 999999, hs, admin)
    with pytest.raises(BusinessError, match="aujourd'hui"):
        await booking.change_room(db, res, await _room(db, "209"), today + timedelta(days=1), hs, None)
    with pytest.raises(BusinessError, match="clôturée"):
        await booking.change_room(db, res, await _room(db, "209"), today, hs, None)
    with pytest.raises(BusinessError):
        await booking.add_companion(db, res, {"first_name": "", "last_name": ""}, hs, None)


# ---------------------------------------------------------------- CRM
async def test_crm_search_merge_and_alerts(db):
    hs = await load_hotel_settings(db)
    a = await _guest(db, "SRCH-1", first_name="Nour", last_name="Saidi", phone="0770 11 22 33")
    b = await _guest(db, "SRCH-2", first_name="Nour", last_name="Saidi", phone="0770112233")
    assert (await crm.search(db, "srch 1")).redirect == f"/guests/{a.id}"
    result = await crm.search(db, "Saidi")
    assert {g.id for g in result.guests} >= {a.id, b.id}
    assert (await crm.search(db, "")).redirect is None
    groups = await crm.potential_duplicates(db)
    assert any({a.id, b.id} <= {g.id for g in group} for group in groups)
    from app.models import User
    admin = await db.scalar(select(User).where(User.username == "admin"))
    b.status, b.status_reason, b.preferences = "vip", "Habitué", "Étage élevé"
    await crm.merge_guests(db, a, b, hs, admin)
    assert a.status == "vip" and a.preferences == "Étage élevé"
    alerts = await crm.guest_alerts(db, a, hs)
    assert any(alert.level == "info" for alert in alerts)
    with pytest.raises(BusinessError):
        await crm.set_guest_status(db, a, "inconnu", "", admin, hs)
    with pytest.raises(BusinessError):
        await crm.save_guest(db, None, {"first_name": "A", "last_name": "B", "id_type": "faux"}, hs, admin)
    with pytest.raises(BusinessError):
        await crm.save_guest(db, None, {"first_name": "A", "last_name": "B", "gender": "X"}, hs, admin)
    assert crm.age_on(date(2000, 6, 15), date(2026, 6, 14)) == 25 and crm.age_on(None, date.today()) is None


# ---------------------------------------------------------------- POS et stock
async def test_pos_services(db):
    hs = await load_hotel_settings(db)
    today = today_of(hs)
    with pytest.raises(BusinessError, match="désactivé"):
        await pos.create_order(db, "restaurant", "", hs, None, {})
    with pytest.raises(BusinessError, match="inconnu"):
        await pos.create_order(db, "spa", "", hs, None, {"spa": "Spa"})
    item_a = PosItem(outlet="salle_reunion", name="Salle demi-journée", price=10000, vat_rate=19)
    item_b = PosItem(outlet="salle_reunion", name="Pause café", price=500, vat_rate=9, track_stock=True, stock_qty=1)
    other = PosItem(outlet="parking", name="Nuit parking", price=300)
    db.add_all([item_a, item_b, other])
    await db.flush()
    order = await pos.create_order(db, "salle_reunion", "Séminaire", hs, None, {"salle_reunion": "Salle"})
    order = await load_pos_order(db, order.id)
    with pytest.raises(BusinessError, match="vide"):
        await pos.settle_cash(db, order, "especes", hs, None)
    await pos.add_line(db, order, item_a, 1)
    await pos.add_line(db, order, item_a, 1)
    assert len(order.lines) == 1 and order.total == 20000
    with pytest.raises(BusinessError, match="Stock insuffisant"):
        await pos.add_line(db, order, item_b, 2)
    with pytest.raises(BusinessError, match="n'appartient pas"):
        await pos.add_line(db, order, other, 1)
    with pytest.raises(BusinessError, match="Quantité"):
        await pos.add_line(db, order, item_a, 0)
    await pos.add_line(db, order, item_b, 1)
    guest = await _guest(db, "POS-1")
    res = await booking.create_reservation(db, guest=guest, room=await _room(db, "306"), check_in=today,
                                           check_out=today + timedelta(days=1), hs=hs, user=None)
    res = await load_reservation(db, res.id)
    with pytest.raises(BusinessError, match="en séjour"):
        await pos.settle_room_charge(db, order, res, hs, None)
    await booking.check_in(db, res, hs, None)
    await pos.settle_room_charge(db, order, res, hs, None)
    posted = [i for i in res.folio_items if i.kind == "pos"]
    assert sorted((i.vat_rate, i.amount) for i in posted) == [(9, 500), (19, 20000)]  # une écriture par taux
    assert item_b.stock_qty == 0
    with pytest.raises(BusinessError, match="clôturée"):
        await pos.add_line(db, order, item_a, 1)
    with pytest.raises(BusinessError):
        await pos.adjust_stock(db, item_b, -5, "ajustement", "", hs, None)
    with pytest.raises(BusinessError):
        await pos.adjust_stock(db, item_b, -1, "reappro", "", hs, None)
    with pytest.raises(BusinessError):
        await pos.adjust_stock(db, item_b, 0, "reappro", "", hs, None)
    with pytest.raises(BusinessError):
        await pos.adjust_stock(db, item_b, 3, "vol", "", hs, None)
    await pos.adjust_stock(db, item_b, 4, "reappro", "BL-9", hs, None)
    assert item_b.stock_qty == 4
    ticket = printing.pos_receipt(order, hs)
    assert b"Impute chambre" in ticket


# ---------------------------------------------------------------- rapports
async def test_reports_series_and_entries(db):
    hs = await load_hotel_settings(db)
    today = today_of(hs)
    start = today - timedelta(days=40)
    guest = await _guest(db, "REPORT-1")
    res = await booking.create_reservation(db, guest=guest, room=await _room(db, "307"), check_in=today,
                                           check_out=today + timedelta(days=1), hs=hs, user=None)
    res = await load_reservation(db, res.id)
    await booking.add_extra(db, res, description="Navette aéroport", amount=2500, vat_rate=19, hs=hs, user=None)
    await booking.add_payment(db, res, amount=2500, kind="acompte", method="cib", hs=hs, user=None)
    await db.flush()
    kpi = await reports.kpis(db, start, today + timedelta(days=1))
    assert kpi.available_nights > 0 and 0 <= kpi.occupancy <= 100
    assert kpi.adr >= 0 and kpi.revpar >= 0 and kpi.total_revenue >= kpi.room_revenue
    for granularity in ("day", "week", "month", "bogus"):
        series = await reports.revenue_series(db, start, today + timedelta(days=1), granularity)
        assert len(series["labels"]) == len(series["room"]) == len(series["services"])
    monthly = await reports.monthly_series(db, today.year)
    assert len(monthly) == 12
    entries = await reports.accounting_entries(db, start, today + timedelta(days=1))
    assert {"VENTES", "CAISSE"} & {e["journal"] for e in entries}
    summary = await reports.daily_summary(db, today - timedelta(days=1))
    assert "by_kind" in summary and summary["kpi"].available_nights > 0


# ---------------------------------------------------------------- impression réseau
def test_escpos_and_network_printing():
    hs = HotelSettings(receipt_width_chars=32)
    received = bytearray()
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def accept():
        connection, _ = server.accept()
        with connection:
            while chunk := connection.recv(4096):
                received.extend(chunk)

    thread = threading.Thread(target=accept)
    thread.start()
    printing.send_raw("127.0.0.1", port, printing.test_page(hs))
    thread.join(timeout=5)
    server.close()
    assert received.startswith(b"\x1b@") and b"TEST D'IMPRESSION" in received and received.endswith(b"\x1dVA\x03")
    builder = printing.EscPos(20).pair("Une très longue désignation qui déborde", "1 000 DA")
    assert b"Une tres longue" in builder.bytes()
    with pytest.raises(BusinessError, match="Aucune imprimante"):
        printing.send_raw("", 9100, b"x")
    with pytest.raises(BusinessError, match="injoignable"):
        printing.send_raw("127.0.0.1", 1, b"x", timeout=1)


async def test_invoice_receipt_for_proforma(db):
    hs = await load_hotel_settings(db)
    today = today_of(hs)
    guest = await _guest(db, "PROFORMA-1")
    res = await booking.create_reservation(db, guest=guest, room=await _room(db, "308"), check_in=today,
                                           check_out=today + timedelta(days=2), hs=hs, user=None)
    res = await load_reservation(db, res.id)
    view = invoice_view(res, hs, await load_calendar(db, hs))
    assert not view.is_final and view.number.startswith("PROFORMA")
    data = printing.invoice_receipt(view, hs)
    assert b"FACTURE PROFORMA" in data and b"SOLDE" in data


# ---------------------------------------------------------------- planificateur, sauvegardes, seed
async def test_scheduler_tick_runs_audit_and_backup(db, monkeypatch):
    hs = await load_hotel_settings(db)
    hs_dict = hs.model_dump()
    hs_dict.update(night_audit_hour=0, backup_hour=0)

    async def fake_settings(_db):
        return HotelSettings(**hs_dict)

    monkeypatch.setattr(scheduler, "load_hotel_settings", fake_settings)
    monkeypatch.setattr(scheduler.backup, "has_backup_on", lambda day, reason="automatique": False)
    summary = await scheduler.tick()
    assert summary["backup"]
    assert isinstance(summary["audits"], int)
    assert not (await night_audit.pending_audit_dates(db, hs))


def test_backup_prune_and_validation(tmp_dir):
    first = backup.create_backup("automatique")
    second = backup.create_backup("automatique")
    assert backup.has_backup_on(second.created_at.date())
    removed = backup.prune_backups(1)
    remaining = [b for b in backup.list_backups() if b.reason == "automatique"]
    assert removed >= 1 and len(remaining) == 1 and first.name not in {b.name for b in remaining}
    assert second.size_label.endswith(("o", "Ko", "Mo"))
    with pytest.raises(BusinessError):
        backup.get_backup("../x")
    with pytest.raises(BusinessError):
        backup.store_upload(b"", "vide.db")
    stored = backup.store_upload(b"abc", "../../evil name.db")
    assert stored.parent.name == "_uploads" and ".." not in stored.name


async def test_seed_demo_and_password_reset(db):
    assert await seed.reset_password(db, "inexistant") is None
    password = await seed.reset_password(db, "gouvernante")
    assert password and len(password) >= 10
    from app.models import User
    user = await db.scalar(select(User).where(User.username == "gouvernante"))
    assert user.must_change_password
    await db.rollback()
    seed.print_created_users([])
    seed.print_created_users([("demo", "motdepasse")])


async def test_seed_demo_data_on_empty_guest_table(monkeypatch, db):
    from sqlalchemy import func
    from app.models import Guest as GuestModel

    calls = {}
    real_scalar = db.scalar

    async def fake_scalar(stmt, *args, **kwargs):
        text = str(stmt)
        if "count(guests.id)" in text and "demo" not in calls:
            calls["demo"] = True
            return 0
        return await real_scalar(stmt, *args, **kwargs)

    monkeypatch.setattr(db, "scalar", fake_scalar)
    summary = await seed.seed_demo_data(db)
    assert summary["reservations"] == 5 and summary["items"] == 9
    assert await real_scalar(select(func.count(GuestModel.id))) >= 5
