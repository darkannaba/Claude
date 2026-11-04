"""Règles métier : tarification, folio, conflits, départ/solde, changement de chambre, PDF."""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.admin import Company
from app.models.guests import Guest
from app.models.property import Room
from app.models.reservations import Charge, Payment
from app.services import availability as av
from app.services.common import BookingError
from app.services.folio import compute_folio
from app.services.pdf import invoice_pdf, police_form_pdf
from app.services.pricing import nightly_price
from app.services.settings import load_settings, today_of


@pytest.fixture()
def db():
    """Session jetable : les services ne committent pas, tout est annulé à la fin du test."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _room(db, number):
    return db.scalar(select(Room).where(Room.number == number))


def _guest(db, id_number="AB123456", id_type="cni"):
    guest = Guest(first_name="Yacine", last_name="Test", phone="0550000000", id_type=id_type,
                  id_number=id_number, nationality="Algérienne", created_at=datetime(2030, 1, 1))
    db.add(guest)
    db.flush()
    return guest


def _settings(db):
    return load_settings(db)


def test_nightly_price_weekend_and_company_discount(db):
    settings = _settings(db)
    room_type = _room(db, "101").room_type  # STD, 8 000 DA de base
    friday = next(d for d in (date(2031, 3, 1) + timedelta(i) for i in range(7)) if d.weekday() == 4)
    monday = friday + timedelta(days=3)

    assert nightly_price(db, room_type, monday, None, settings) == 8000
    assert nightly_price(db, room_type, friday, None, settings) == 8800  # +10 % week-end

    company = Company(name="Sonatrach Test", discount_percent=10)
    assert nightly_price(db, room_type, friday, company, settings) == 7920  # puis -10 % entreprise


def test_folio_totals_vat_tourist_tax_and_balance(db):
    settings = _settings(db)
    room = _room(db, "102")
    guest = _guest(db)
    check_in = date(2031, 6, 2)  # lundi
    res = av.create_reservation(db, guest=guest, room=room, check_in=check_in,
                                check_out=check_in + timedelta(days=2), adults=2, children=0)
    db.add(Charge(reservation_id=res.id, label="Blanchisserie", category="autre", amount_ttc=1000))
    db.add(Payment(reservation_id=res.id, amount=5000, kind="acompte", method="carte"))
    db.flush()

    folio = compute_folio(db, res, settings)
    assert folio.nights == 2
    assert folio.room_total == 16000                     # 2 nuits x 8 000
    assert folio.taxable_ttc == 17000                    # nuitées + extras, TTC
    assert folio.taxable_ht == 14286                     # 17 000 / 1,19
    assert folio.vat_amount == 2714
    assert folio.tourist_tax == 100 * 2 * 2              # 100 DA x 2 adultes x 2 nuits
    assert folio.total_due == 17400
    assert folio.balance == 12400


def test_double_booking_is_refused(db):
    room = _room(db, "103")
    guest = _guest(db)
    start = date(2031, 7, 1)
    av.create_reservation(db, guest=guest, room=room, check_in=start, check_out=start + timedelta(days=3))
    with pytest.raises(BookingError, match="pas disponible"):
        av.create_reservation(db, guest=guest, room=room, check_in=start + timedelta(days=1),
                              check_out=start + timedelta(days=4))
    # Une arrivée le jour du départ précédent reste possible.
    av.create_reservation(db, guest=guest, room=room, check_in=start + timedelta(days=3),
                          check_out=start + timedelta(days=5))


def test_capacity_and_date_validation(db):
    room = _room(db, "104")  # STD : capacité 2
    guest = _guest(db)
    start = date(2031, 8, 1)
    with pytest.raises(BookingError, match="Capacité"):
        av.create_reservation(db, guest=guest, room=room, check_in=start, check_out=start + timedelta(days=1),
                              adults=2, children=1)
    with pytest.raises(BookingError, match="postérieure"):
        av.create_reservation(db, guest=guest, room=room, check_in=start, check_out=start)


def test_check_in_requires_identity_document(db):
    settings = _settings(db)
    today = today_of(settings)
    room = _room(db, "105")
    guest = _guest(db, id_number="")
    res = av.create_reservation(db, guest=guest, room=room, check_in=today, check_out=today + timedelta(days=2))
    with pytest.raises(BookingError, match="Pièce d'identité"):
        av.check_in(db, res, settings, datetime.now())
    guest.id_number = "123456789"
    av.check_in(db, res, settings, datetime.now())
    assert res.status == "checked_in"
    assert room.status == "occupied"


def test_checkout_blocked_by_unpaid_balance_then_forced(db):
    settings = _settings(db)
    today = today_of(settings)
    room = _room(db, "106")
    res = av.create_reservation(db, guest=_guest(db), room=room, check_in=today - timedelta(days=1),
                                check_out=today + timedelta(days=1))
    av.check_in(db, res, settings, datetime.now())
    # Le service modifie la session avant de contrôler le solde : on simule le rollback
    # que fait le routeur en cas d'erreur, via un savepoint.
    savepoint = db.begin_nested()
    with pytest.raises(BookingError, match="Solde impayé"):
        av.check_out(db, res, settings, datetime.now(), force=False)
    savepoint.rollback()
    db.refresh(res)
    assert res.status == "checked_in"
    assert room.status == "occupied"

    av.check_out(db, res, settings, datetime.now(), force=True)
    assert res.status == "checked_out"
    assert res.check_out == today
    assert room.status == "dirty"


def test_room_change_splits_stay_and_updates_statuses(db):
    settings = _settings(db)
    today = today_of(settings)
    old_room, new_room = _room(db, "107"), _room(db, "108")
    res = av.create_reservation(db, guest=_guest(db), room=old_room, check_in=today - timedelta(days=1),
                                check_out=today + timedelta(days=2))
    av.check_in(db, res, settings, datetime.now())

    av.change_room(db, res, new_room, today, settings)

    assert res.room_id == new_room.id
    assert old_room.status == "dirty"
    assert new_room.status == "occupied"
    rooms_by_segment = sorted((s.room.number, s.start_date, s.end_date) for s in res.segments)
    assert rooms_by_segment == [
        ("107", today - timedelta(days=1), today),
        ("108", today, today + timedelta(days=2)),
    ]
    folio = compute_folio(db, res, settings)
    assert folio.nights == 3
    assert len(folio.room_lines) == 2


def test_room_change_refused_when_target_busy(db):
    settings = _settings(db)
    today = today_of(settings)
    busy = _room(db, "110")
    av.create_reservation(db, guest=_guest(db, "BUSY1"), room=busy, check_in=today, check_out=today + timedelta(days=2))
    res = av.create_reservation(db, guest=_guest(db, "FREE1"), room=_room(db, "109"), check_in=today,
                                check_out=today + timedelta(days=2))
    with pytest.raises(BookingError, match="pas disponible"):
        av.change_room(db, res, busy, today, settings)


def test_cancel_and_no_show_rules(db):
    settings = _settings(db)
    today = today_of(settings)
    res = av.create_reservation(db, guest=_guest(db), room=_room(db, "201"), check_in=today,
                                check_out=today + timedelta(days=1))
    av.cancel(res, datetime.now())
    db.flush()  # les requêtes de disponibilité lisent la base : il faut flusher le changement de statut
    assert res.status == "cancelled"
    assert av.room_is_free(db, res.room_id, today, today + timedelta(days=1)) is True  # annulée = libérée
    with pytest.raises(BookingError):
        av.mark_no_show(res, settings)


def test_pdf_documents_are_generated(db):
    settings = _settings(db)
    today = today_of(settings)
    room = _room(db, "202")
    res = av.create_reservation(db, guest=_guest(db), room=room, check_in=today, check_out=today + timedelta(days=2))
    folio = compute_folio(db, res, settings)
    police = police_form_pdf(settings, res, res.guest, res.companions, room)
    a4 = invoice_pdf(settings, res, folio, "FAC-TEST-00001", "a4")
    ticket = invoice_pdf(settings, res, folio, "FAC-TEST-00001", "ticket")
    for document in (police, a4, ticket):
        assert document.startswith(b"%PDF")
        assert len(document) > 1000
