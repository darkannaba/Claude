"""Internationalisation FR / AR / EN : t(), fusion des catalogues, détection de la langue, route /lang, pages traduites."""
import json
import re
from datetime import date

import pytest
from markupsafe import Markup
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middlewares.language import LanguageMiddleware
from app.services import i18n
from app.services.i18n import (
    JS_LABELS, LANGS, LOCALES_DIR, catalog, detect_lang, direction, get_lang, is_rtl, js_labels,
    parse_accept_language, reload_catalogs, reset_lang, set_lang, t,
)
from app.templating import _gettext, _tr, render, templates
from tests.conftest import make_client

I18N_BLOCK = re.compile(r'<script type="application/json" id="i18n-data">(.*?)</script>', re.S)


def _i18n_data(html: str) -> dict:
    match = I18N_BLOCK.search(html)
    assert match, "bloc JSON window.I18N absent"
    return json.loads(match.group(1))


def _scope(session: dict | None = None, cookie: str = "", accept: str = "", path: str = "/") -> dict:
    headers = []
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    if accept:
        headers.append((b"accept-language", accept.encode()))
    scope = {"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": headers}
    if session is not None:
        scope["session"] = session
    return scope


# ---------------------------------------------------------------- t(), catalogues, sens d'écriture
def test_t_identity_fallback_and_translation():
    assert t("Tableau de bord") == "Tableau de bord"            # langue courante = français
    assert t("Tableau de bord", "fr") == "Tableau de bord"      # identité
    assert t("Tableau de bord", "en") == "Dashboard"
    assert t("Tableau de bord", "ar") == "لوحة التحكم"
    assert t("Tableau de bord", "en-US") == "Dashboard"         # code régional ramené à la langue
    assert t("Texte jamais traduit", "en") == "Texte jamais traduit"   # repli : texte source
    assert t("Texte jamais traduit", "ar") == "Texte jamais traduit"
    assert t("Tableau de bord", "xx") == "Tableau de bord"      # langue inconnue : français
    assert t("", "en") == ""
    assert t("Taux d'occupation (%)", "en") == "Occupancy rate (%)"   # apostrophe dans la clé JSON


def test_current_language_context_variable():
    assert get_lang() == "fr"
    token = set_lang("ar")
    try:
        assert get_lang() == "ar"
        assert t("Factures") == "الفواتير"
        assert is_rtl() is True and direction() == "rtl"
    finally:
        reset_lang(token)
    assert get_lang() == "fr" and is_rtl() is False
    token = set_lang("klingon")
    try:
        assert get_lang() == "fr"
    finally:
        reset_lang(token)


def test_is_rtl_and_direction():
    assert is_rtl("ar") is True and is_rtl("ar-DZ") is True
    assert is_rtl("fr") is False and is_rtl("en") is False and is_rtl("xx") is False
    assert direction("ar") == "rtl" and direction("en") == "ltr" and direction("fr") == "ltr"
    assert LANGS == {"fr": "Français", "ar": "العربية", "en": "English"}


def test_real_catalogs_merge_every_group_of_the_language_folder():
    for lang in ("en", "ar"):
        merged = catalog(lang)
        assert "Tableau de bord" in merged      # common.json
        assert "Hébergement" in merged          # js.json
        assert set(JS_LABELS) <= set(merged), lang
    assert catalog("fr") == {}
    for group in ("common.json", "js.json"):
        en = json.loads((LOCALES_DIR / "en" / group).read_text(encoding="utf-8"))
        ar = json.loads((LOCALES_DIR / "ar" / group).read_text(encoding="utf-8"))
        assert set(en) == set(ar), f"clés différentes entre en/{group} et ar/{group}"
        assert all(value for value in en.values()) and all(value for value in ar.values())


@pytest.fixture()
def temp_locales(tmp_path, monkeypatch):
    """Dossier de traductions jetable ; les vrais catalogues sont rechargés à la fin."""
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)
    yield tmp_path
    monkeypatch.undo()
    reload_catalogs()


def test_all_json_files_of_a_language_folder_are_merged(temp_locales):
    en = temp_locales / "en"
    en.mkdir()
    (en / "a_groupe.json").write_text(json.dumps({"Un": "One", "Commun": "Shared"}), encoding="utf-8")
    (en / "b_groupe.json").write_text(json.dumps({"Deux": "Two", "L'été": "Summer", "Vide": ""}), encoding="utf-8")
    reload_catalogs()
    assert t("Un", "en") == "One" and t("Deux", "en") == "Two" and t("Commun", "en") == "Shared"
    assert t("L'été", "en") == "Summer"
    assert t("Vide", "en") == "Vide"           # traduction vide : repli sur le texte source
    assert catalog("ar") == {}                  # dossier absent : aucune traduction, aucune erreur
    assert t("Un", "ar") == "Un"


def test_parse_accept_language():
    assert parse_accept_language("ar-DZ,ar;q=0.9,fr;q=0.8") == "ar"
    assert parse_accept_language("de-DE,de;q=0.9,en;q=0.5") == "en"
    assert parse_accept_language("fr;q=0.3, en;q=0.8") == "en"
    assert parse_accept_language("en;q=0, fr") == "fr"
    assert parse_accept_language("de, *;q=0.1") is None
    assert parse_accept_language("") is None and parse_accept_language(None) is None


def test_detect_lang_priority_session_cookie_header():
    assert detect_lang(Request(_scope(session={"lang": "ar"}, cookie="lang=en", accept="en"))) == "ar"
    assert detect_lang(Request(_scope(session={}, cookie="lang=en", accept="ar"))) == "en"
    assert detect_lang(Request(_scope(session={}, accept="ar-DZ,fr;q=0.5"))) == "ar"
    assert detect_lang(Request(_scope(session={"lang": "xx"}, cookie="lang=zz", accept="de"))) == "fr"
    assert detect_lang(Request(_scope())) == "fr"   # sans SessionMiddleware (middleware de licence)


def test_js_labels():
    assert js_labels("fr") == {key: key for key in JS_LABELS}
    english = js_labels("en")
    assert english["Hébergement"] == "Accommodation" and english["RevPAR (DA)"] == "RevPAR (DZD)"
    assert js_labels("ar")["Taxe de séjour"] == "رسم الإقامة"


# ---------------------------------------------------------------- filtres et globales Jinja
def test_jinja_gettext_and_tr():
    source = '{{ _("Factures") }}|{{ "Confirmée"|tr }}|{{ 5|tr }}|{{ nothing|tr }}|{{ d|weekday }}'
    template = templates.env.from_string(source)
    assert template.render(lang="en", nothing=None, d=date(2026, 1, 5)) == "Invoices|Confirmed|5|None|Mon"
    assert template.render(lang="ar", nothing=None, d=date(2026, 1, 5)) == "الفواتير|مؤكد|5|None|الاثنين"
    assert template.render(nothing=None, d=date(2026, 1, 5)) == "Factures|Confirmée|5|None|Lun"
    # _() n'est pas ré-échappé (sortie française strictement identique aux libellés en dur) ; |tr conserve le type.
    assert isinstance(_gettext({}, "Taux d'occupation (%)"), Markup)
    assert str(_gettext({"lang": "fr"}, "Taux d'occupation (%)")) == "Taux d'occupation (%)"
    assert _tr({"lang": "en"}, "Libre") == "Available" and not isinstance(_tr({"lang": "en"}, "Libre"), Markup)
    assert isinstance(_tr({"lang": "en"}, Markup("Libre")), Markup)
    assert templates.env.globals["LANGS"] is LANGS


# ---------------------------------------------------------------- route POST /lang
def test_lang_route_sets_session_and_cookie_then_redirects(admin):
    response = admin.form("/lang", {"lang": "en", "next": "/reservations?status=confirmed"})
    assert response.status_code == 303
    assert response.headers["location"] == "/reservations?status=confirmed"
    assert "lang=en" in response.headers.get("set-cookie", "")
    page = admin.get("/")
    assert '<html lang="en" dir="ltr"' in page.text
    for label in ("Dashboard", "Reservations", "Guests", "Night audit", "Invoices", "Reports", "Administration"):
        assert label in page.text, label
    assert "Sign out" in page.text and "Change my password" in page.text and "Local network – offline" in page.text
    assert "Dashboard</a>" in page.text and "Tableau de bord</a>" not in page.text   # lien de navigation traduit


def test_lang_route_rejects_unknown_language(admin):
    response = admin.form("/lang", {"lang": "xx", "next": "/"})
    assert response.status_code == 400
    assert "Requête invalide" in response.text and "Langue inconnue." in response.text
    assert admin.form("/lang", {"next": "/"}).status_code == 400
    assert '<html lang="fr" dir="ltr"' in admin.get("/").text


def test_lang_route_only_redirects_to_internal_paths(admin):
    for target in ("https://evil.example/", "//evil.example", "javascript:alert(1)", ""):
        response = admin.form("/lang", {"lang": "en", "next": target})
        assert response.status_code == 303 and response.headers["location"] == "/", target


def test_lang_route_requires_csrf(app):
    client = make_client(app)
    response = client.post("/lang", data={"lang": "en", "next": "/"}, follow_redirects=False)
    assert response.status_code == 403


# ---------------------------------------------------------------- pages rendues
def test_french_is_still_the_default_and_unchanged(admin):
    page = admin.get("/").text
    assert '<html lang="fr" dir="ltr" class="h-full">' in page
    assert "Tableau de bord" in page and "Se déconnecter" in page and "Réseau local – hors ligne" in page
    assert "lg:pl-64" in page and "left-0 -translate-x-full" in page
    assert _i18n_data(page) == {key: key for key in JS_LABELS}


def test_arabic_page_is_mirrored_and_translated(reception):
    assert reception.form("/lang", {"lang": "ar", "next": "/reservations"}).status_code == 303
    page = reception.get("/reservations").text
    assert '<html lang="ar" dir="rtl" class="h-full ar">' in page
    assert "الحجوزات" in page and "لوحة التحكم" in page and "تسجيل الخروج" in page
    assert "lg:pr-64" in page and "right-0 translate-x-full" in page and "lg:pl-64" not in page
    assert _i18n_data(page)["Hébergement"] == "الإقامة"
    assert "الاستقبال" in page   # rôle de l'utilisateur traduit via |tr


def test_js_i18n_block_is_injected_in_english(admin):
    admin.form("/lang", {"lang": "en", "next": "/"})
    data = _i18n_data(admin.get("/reports").text)
    assert data["Hébergement"] == "Accommodation"
    assert data["Taux d'occupation (%)"] == "Occupancy rate (%)"
    assert data["Annuler cette réservation ?"] == "Cancel this reservation?"
    script = admin.get("/static/js/app.js").text
    assert "window.I18N" in script and "i18n-data" in script


def test_language_switcher_visible_for_every_role(admin, reception, housekeeper, anonymous):
    for client, path in ((admin, "/reservations?status=confirmed"), (reception, "/guests"),
                         (housekeeper, "/housekeeping"), (anonymous, "/login")):
        page = client.get(path).text
        assert 'action="/lang"' in page, path
        for code in LANGS:
            assert f'name="lang" value="{code}"' in page, (path, code)
        assert f'name="next" value="{path}"' in page, path
    assert "Housekeeping" in housekeeper.get("/housekeeping").text


def test_cookie_alone_selects_the_language_for_anonymous_pages(app):
    client = make_client(app)
    client.cookies.set("lang", "ar")
    page = client.get("/login").text
    assert '<html lang="ar" dir="rtl"' in page and 'aria-label="اللغة"' in page


def test_accept_language_header_is_used_as_last_resort(anonymous):
    english = anonymous.get("/login", headers={"Accept-Language": "en-GB,en;q=0.9,fr;q=0.5"}).text
    assert '<html lang="en" dir="ltr"' in english and 'aria-label="Language"' in english
    german = anonymous.get("/login", headers={"Accept-Language": "de-DE,de;q=0.9"}).text
    assert '<html lang="fr" dir="ltr"' in german


def test_error_pages_are_translated(app, admin):
    client = make_client(app)
    client.cookies.set("lang", "en")
    response = client.post("/login", data={"username": "x", "password": "y"}, follow_redirects=False)
    assert response.status_code == 403
    assert "Session expired" in response.text and "Sign in" in response.text and '<html lang="en"' in response.text
    admin.form("/lang", {"lang": "ar", "next": "/"})
    missing = admin.get("/page-inexistante")
    assert missing.status_code == 404
    assert "الصفحة غير موجودة" in missing.text and 'dir="rtl"' in missing.text and "الرئيسية" in missing.text


async def test_flash_messages_are_translated_only_when_displayed(db):
    flashes = [{"kind": "success", "msg": "Session expirée"}, {"kind": "error", "msg": "Message libre 42"}]
    request = Request(_scope(session={"flashes": list(flashes), "lang": "en", "csrf_token": "jeton"}, path="/login"))
    response = await render(request, db, "auth/login.html", next="/")
    body = response.body.decode()
    assert "Session expired" in body and "Session expirée" not in body
    assert "Message libre 42" in body            # message sans traduction : texte source
    assert "flashes" not in request.session      # consommés à l'affichage
    request = Request(_scope(session={"flashes": list(flashes), "lang": "ar"}, path="/login"))
    body = (await render(request, db, "auth/login.html", next="/")).body.decode()
    assert "انتهت صلاحية الجلسة" in body and 'dir="rtl"' in body


# ---------------------------------------------------------------- middleware optionnel
def test_language_middleware_sets_the_language_for_the_whole_request():
    async def endpoint(request):
        return PlainTextResponse(f"{get_lang()}:{request.state.lang}:{t('Factures')}")

    app = Starlette(
        routes=[Route("/", endpoint)],
        middleware=[Middleware(SessionMiddleware, secret_key="test"), Middleware(LanguageMiddleware)],
    )
    with TestClient(app) as client:
        assert client.get("/", headers={"Accept-Language": "ar"}).text == "ar:ar:الفواتير"
        assert client.get("/").text == "fr:fr:Factures"
        client.cookies.set("lang", "en")
        assert client.get("/", headers={"Accept-Language": "ar"}).text == "en:en:Invoices"
    assert get_lang() == "fr"


def test_rtl_classes_are_compiled_in_css(anonymous):
    css = anonymous.get("/static/css/app.css").text
    for name in (".right-0{", ".translate-x-full{", ".lg\\:pr-64{", ".pr-9{", ".right-3{", ".mr-auto{", ".text-right{"):
        assert name in css, name
