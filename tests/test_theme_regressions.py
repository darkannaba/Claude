"""Non-régression de la thématisation : densité compacte, lisibilité d'une charte claire, pied de document long,
fichiers de marque et transactions, SVG piégés, couleurs des statuts et de la secondaire, restauration, fichiers
absents du disque."""
import asyncio
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models.theme import ThemeSettings
from app.services import backup_manager
from app.services import theme_manager as tm
from app.services.common import BusinessError
from app.services.settings import load_hotel_settings
from tests import test_theme_documents
from tests.test_theme import CSS_URL, css_var, current_theme, flashes, image_bytes, upload

# Fixtures partagées : charte rétablie avant et après chaque test, séjour en cours pour les PDF
clean_theme = test_theme_documents.clean_theme


@pytest.fixture(scope="module")
def booking(app):
    """Réservation lointaine (aucun conflit avec les séjours des autres modules de test)."""
    from datetime import timedelta

    from tests.conftest import make_client

    reception = make_client(app, "reception")
    start = run(test_theme_documents._today()) + timedelta(days=420)
    created = reception.form("/reservations", {
        "check_in": start.isoformat(), "check_out": (start + timedelta(days=2)).isoformat(),
        "room_id": str(run(test_theme_documents._room_id("309"))),
        "g_last_name": "Haddad", "g_first_name": "Lina", "g_phone": "0661000888", "g_id_type": "passport",
        "g_id_number": "THEME-REG-001", "g_nationality": "Algérienne", "adults": "1", "children": "0",
        "channel": "Direct",
    })
    assert created.status_code == 303, created.text
    return {"url": re.search(r"(/reservations/\d+)$", created.headers["location"]).group(1)}

ROOT = Path(__file__).resolve().parent.parent
APP_CSS = (ROOT / "app/static/css/app.css").read_text(encoding="utf-8")
INPUT_CSS = (ROOT / "app/static/src/input.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
TEMPLATES = ROOT / "app/templates"


def run(coro):
    return asyncio.run(coro)


def theme_of(**values) -> SimpleNamespace:
    return SimpleNamespace(**{**tm.DEFAULT_THEME, "version": 1, **values})


async def _set_theme(**values) -> None:
    async with SessionLocal() as db:
        theme = await tm.get_theme(db)
        for field, value in values.items():
            setattr(theme, field, value)
        await db.commit()


def branding_names() -> set[str]:
    folder = tm.branding_dir()
    return {path.name for path in folder.iterdir()} if folder.is_dir() else set()


# ---------------------------------------------------------------- 1. densité compacte
def test_compact_density_emits_only_variables():
    css = tm.build_css(theme_of(density="compact"))
    # Aucune règle de sélecteur de composant : elle écraserait p-0 (planning), pl-9 (recherche), mb-0…
    rules = re.findall(r"^\s*([^{}\n]+)\{", css, re.MULTILINE)
    assert {rule.strip() for rule in rules} <= {":root", ".dark", 'html[dir="rtl"]',
                                                'html[dir="rtl"] body, html[dir="rtl"] .ar, html.ar body', "body"}
    assert css_var(css, "density-card-p") == "0.875rem" and css_var(css, "density-input-x") == "0.5rem"
    # Les composants lisent ces variables dans la feuille compilée, et les utilitaires restent applicables
    for fragment in ("padding:var(--density-card-p)", "padding:var(--density-input-y) var(--density-input-x)",
                     "padding:var(--density-btn-sm-y) var(--density-btn-sm-x)", "margin-bottom:var(--density-title-gap)",
                     ".p-0{padding:0}", ".pl-9{padding-left:2.25rem}", ".mb-0{margin-bottom:0}"):
        assert fragment in APP_CSS, fragment


# ---------------------------------------------------------------- 2. charte claire lisible
@pytest.mark.parametrize("primary", ["#facc15", "#fde68a", "#ffffff", "#0d9488", "#7c3aed"])
def test_light_primary_keeps_text_readable(primary):
    css = tm.build_css(theme_of(primary_color=primary))
    brand = tm.palette(primary)
    on_primary = css_var(css, "color-on-primary")
    assert tm.contrast_ratio(on_primary, brand[600]) >= 3.0
    links = re.findall(r"--color-link: (#[0-9a-f]{6});", css)
    assert len(links) == 2  # mode clair puis mode sombre
    for background in ("#ffffff", tm.LIGHT_SURFACE):
        assert tm.contrast_ratio(links[0], background) >= 4.5
    assert tm.contrast_ratio(links[1], tm.mix(tm.DARK_CARD, primary, 0.05)) >= 4.5
    assert tm.contrast_ratio(css_var(css, "color-brand-strong"), brand[100]) >= 4.5


def test_readable_on_keeps_sufficient_colours_and_darkens_others():
    assert tm.readable_on("#0f766e", "#ffffff") == "#0f766e"
    darker = tm.readable_on("#cda711", "#ffffff")
    assert tm.contrast_ratio(darker, "#ffffff") >= 4.5 and tm.relative_luminance(darker) < tm.relative_luminance("#cda711")
    lighter = tm.readable_on("#0f766e", "#0f172a")
    assert tm.contrast_ratio(lighter, "#0f172a") >= 4.5


def test_no_hard_coded_white_text_on_brand_colour():
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"bg-brand-[5-9]00[^\"']*text-white|text-white[^\"']*bg-brand-[5-9]00", text):
            offenders.append(path.name)
    assert offenders == []
    assert "brand-solid" in (TEMPLATES / "pms/planning.html").read_text(encoding="utf-8")
    assert "brand-solid" in (TEMPLATES / "macros/ui.html").read_text(encoding="utf-8")
    assert "brand-solid" in (TEMPLATES / "layout/base.html").read_text(encoding="utf-8")
    assert ".brand-solid{background-color:rgb(var(--brand-600));color:var(--color-on-primary)}" in APP_CSS
    assert re.search(r"\.bar-checked_in\{[^}]*color:var\(--color-on-primary\)", APP_CSS)
    assert re.search(r"\.bar-checked_in:hover\{[^}]*color:var\(--color-on-primary\)", APP_CSS)
    gantt = re.search(r"\.gantt-bar\{[^}]*\}", APP_CSS).group(0)
    assert "color:rgb(255 255 255" not in gantt
    assert "a{color:var(--color-link)}" in APP_CSS.replace(" ", "")


def test_light_primary_saved_with_warning(admin, anonymous):
    response = admin.form("/admin/theme/colors", {"primary_color": "#facc15"})
    assert response.status_code == 303
    messages = flashes(admin)
    assert "Couleurs de la charte enregistrées" in messages
    assert re.search(r'class="flash-warning no-print">.*?Couleur primaire très claire', messages)
    css = anonymous.get(CSS_URL).text
    assert css_var(css, "color-on-primary") == tm.DARK_TEXT
    admin.form("/admin/theme/colors", {"primary_color": "#0d9488"})
    assert 'class="flash-warning no-print"' not in flashes(admin)


# ---------------------------------------------------------------- 3. pied de document très long
def test_footer_line_limit_enforced(admin):
    response = admin.form("/admin/theme/documents", {"document_footer": "\n".join(f"ligne {n}" for n in range(9))})
    assert response.status_code == 303
    assert "Trop de lignes" in flashes(admin)
    assert current_theme().document_footer == ""
    admin.form("/admin/theme/documents", {"document_footer": "\n".join(f"ligne {n}" for n in range(8))})
    assert current_theme().document_footer.count("\n") == 7


def test_very_long_footer_never_breaks_a4_documents(booking, admin):
    # Texte déjà en base (antérieur à la limite, base modifiée) : 80 puis 499 lignes
    for lines in (80, 499):
        run(_set_theme(document_footer="\n".join(f"ligne {n}" for n in range(lines))))
        for url in (f"{booking['url']}/police.pdf", f"{booking['url']}/invoice.pdf?format=a4"):
            response = admin.get(url)
            assert response.status_code == 200, (lines, url)
            assert response.content.startswith(b"%PDF")


# ---------------------------------------------------------------- 4. fichiers et transactions
async def test_previous_logo_kept_when_transaction_rolled_back():
    async with SessionLocal() as db:
        theme = await tm.save_image(db, "logo", SimpleNamespace(read=_reader(image_bytes())))
        first = theme.logo_file
        await db.commit()
    assert (tm.branding_dir() / first).is_file()

    async with SessionLocal() as db:  # remplacement annulé : l'ancien reste, le nouveau disparaît
        theme = await tm.save_image(db, "logo", SimpleNamespace(read=_reader(image_bytes((300, 80)))))
        second = theme.logo_file
        assert (tm.branding_dir() / first).is_file() and (tm.branding_dir() / second).is_file()
        await db.rollback()
    assert (tm.branding_dir() / first).is_file() and not (tm.branding_dir() / second).exists()

    async with SessionLocal() as db:  # suppression annulée : fichier conservé
        await tm.delete_image(db, "logo")
        await db.rollback()
    assert (tm.branding_dir() / first).is_file()

    async with SessionLocal() as db:  # remplacement validé : l'ancien n'est supprimé qu'après le commit
        theme = await tm.save_image(db, "logo", SimpleNamespace(read=_reader(image_bytes((300, 80)))))
        third = theme.logo_file
        assert (tm.branding_dir() / first).is_file()
        await db.commit()
    assert not (tm.branding_dir() / first).exists() and (tm.branding_dir() / third).is_file()


def _reader(data: bytes):
    async def read(_size=-1):
        return data
    return read


def test_failed_commit_keeps_previous_logo(app, admin, anonymous, monkeypatch):
    from tests.conftest import PASSWORDS, Client

    assert upload(admin, "logo", image_bytes()).status_code == 303
    first = current_theme().logo_file
    original_commit = AsyncSession.commit
    quiet = Client(app, raise_server_exceptions=False)  # réponse 500 au lieu de l'exception remontée
    quiet.refresh_csrf()
    assert quiet.form("/login", {"username": "admin", "password": PASSWORDS["admin"]}).status_code == 303

    async def failing_commit(self):
        raise OperationalError("COMMIT", {}, Exception("database is locked"))

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)
    before = branding_names()
    assert upload(quiet, "logo", image_bytes((300, 80))).status_code == 500
    assert quiet.form("/admin/theme/delete/logo").status_code == 500
    monkeypatch.setattr(AsyncSession, "commit", original_commit)

    assert current_theme().logo_file == first
    assert (tm.branding_dir() / first).is_file()
    assert branding_names() == before  # aucun fichier orphelin
    assert anonymous.get("/branding/logo").status_code == 200


# ---------------------------------------------------------------- 5 et 6. SVG piégés
TRICKY_SVGS = {
    "style-child-tail": b'<svg xmlns="http://www.w3.org/2000/svg"><style><g/>@import url(https://evil.example/x.css);'
                        b' rect{fill:url(https://evil.example/t)}</style></svg>',
    "css-escape-import": b'<svg xmlns="http://www.w3.org/2000/svg"><style>@\\69mport "https://e/x.css";</style></svg>',
    "css-escape-url": b'<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:u\\72l(https://e/x)"/></svg>',
    "image-set": b'<svg xmlns="http://www.w3.org/2000/svg"><style>rect{fill:image-set("https://e/x" 1x)}</style></svg>',
    "presentation-escape": b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="u\\72l(https://e/x)"/></svg>',
    "xml-stylesheet": b'<?xml-stylesheet type="text/css" href="https://evil.example/x.css"?>'
                      b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>',
    "xsl-after-declaration": b'<?xml version="1.0"?><?xml-stylesheet type="text/xsl" href="https://e/x.xsl"?>'
                             b'<svg xmlns="http://www.w3.org/2000/svg"/>',
    "pi-inside-root": b'<svg xmlns="http://www.w3.org/2000/svg"><?xml-stylesheet href="https://e/x.css"?><rect/></svg>',
}


@pytest.mark.parametrize("name", sorted(TRICKY_SVGS))
def test_tricky_svg_rejected(name):
    with pytest.raises(BusinessError, match="SVG refusé"):
        tm.sanitize_svg(TRICKY_SVGS[name])


@pytest.mark.parametrize("name", ["style-child-tail", "xml-stylesheet"])
def test_tricky_svg_rejected_on_upload(admin, anonymous, name):
    response = upload(admin, "logo", TRICKY_SVGS[name], "evil.svg", "image/svg+xml")
    assert response.status_code == 303
    assert "SVG refusé" in flashes(admin)
    assert current_theme().logo_file == ""
    assert anonymous.get("/branding/logo").status_code == 404


def test_svg_stored_from_validated_tree(admin, anonymous):
    source = (b'<?xml version="1.0" encoding="UTF-8"?>\n<!-- commentaire -->'
              b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 10 10">'
              b'<!-- interne --><style>rect { fill: url(#g); }</style><use xlink:href="#r"/>'
              b'<rect id="r" width="10" height="10"/></svg>')
    assert upload(admin, "logo", source, "logo.svg", "image/svg+xml").status_code == 303
    served = anonymous.get("/branding/logo").content
    assert served != source
    assert b"<!--" not in served and b"<?xml-stylesheet" not in served
    assert served.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n<svg')
    assert b'xmlns="http://www.w3.org/2000/svg"' in served and b'xlink:href="#r"' in served
    assert b"ns0:" not in served
    tm.sanitize_svg(served)  # le fichier enregistré repasse la vérification


# ---------------------------------------------------------------- 7. couleurs des statuts
def test_room_badges_use_status_colours(admin):
    macros = (TEMPLATES / "macros/ui.html").read_text(encoding="utf-8")
    for status in ("available", "occupied", "dirty", "maintenance"):
        assert f"'badge-status-{status}'" in macros
        assert (f".badge-status-{status}{{background-color:var(--status-{status});"
                f"color:var(--status-{status}-on)}}") in APP_CSS
    for status in ("available", "occupied", "dirty", "maintenance"):
        assert f"--status-{status}-on:" in INPUT_CSS  # valeur par défaut sans feuille dynamique
    assert "badge-status-" in admin.get("/").text
    assert "badge-status-" in admin.get("/admin/rooms").text


def test_status_text_colour_readable_for_custom_status():
    css = tm.build_css(theme_of(status_occupied_color="#7c3aed", status_dirty_color="#fde68a"))
    assert css_var(css, "status-occupied") == "#7c3aed"
    assert tm.contrast_ratio(css_var(css, "status-occupied-on"), "#7c3aed") >= 4.5
    assert tm.contrast_ratio(css_var(css, "status-dirty-on"), "#fde68a") >= 4.5


# ---------------------------------------------------------------- 8. couleur secondaire et graphiques
def test_secondary_colour_used_by_real_components_and_charts(admin):
    assert ".accent-solid{background-color:rgb(var(--accent-600));color:var(--color-on-secondary)}" in APP_CSS
    assert re.search(r"\.bar-confirmed\{[^}]*rgb\(var\(--accent-600\)\)", APP_CSS)
    assert re.search(r"\.badge-accent\{[^}]*rgb\(var\(--accent-100\)", APP_CSS)
    assert "'confirmed': 'badge-accent'" in (TEMPLATES / "macros/ui.html").read_text(encoding="utf-8")
    assert "accent-solid" in (TEMPLATES / "pms/planning.html").read_text(encoding="utf-8")
    # Graphiques : couleurs lues dans les variables CSS de la charte, plus aucune couleur teal figée
    charts = APP_JS[APP_JS.index("graphiques (Chart.js local)"):APP_JS.index("var charts = [];")]
    assert 'cssVar("--color-primary"' in charts and 'cssVar("--color-secondary"' in charts
    assert "getPropertyValue(name)" in charts
    assert "#0d9488\", stack" not in charts and "rgba(13,148,136" not in charts
    css = tm.build_css(theme_of(secondary_color="#eab308"))
    assert css_var(css, "color-secondary") == "#eab308"
    assert tm.contrast_ratio(css_var(css, "color-accent-strong"), tm.palette("#eab308")[100]) >= 4.5


# ---------------------------------------------------------------- 9. restauration
async def test_restore_brings_back_branding_files(db):
    async with SessionLocal() as session:
        theme = await tm.save_image(session, "logo", SimpleNamespace(read=_reader(image_bytes())))
        logo_a = theme.logo_file
        await session.commit()
    hs = await load_hotel_settings(db)
    (job,) = await backup_manager.run_backup(db, hs, destinations=["local"])
    await db.commit()
    with zipfile.ZipFile(job.local_path) as archive:
        assert backup_manager.MEDIA_PREFIX + "branding/" + logo_a in archive.namelist()

    async with SessionLocal() as session:  # remplacement : A est supprimé du disque
        theme = await tm.save_image(session, "logo", SimpleNamespace(read=_reader(image_bytes((300, 80)))))
        logo_b = theme.logo_file
        await session.commit()
    assert not (tm.branding_dir() / logo_a).exists()

    await backup_manager.restore_job(db, job.id, hs, None)
    await db.commit()
    async with SessionLocal() as session:
        restored = await session.get(ThemeSettings, tm.THEME_ID)
        assert restored.logo_file == logo_a
        assert tm.branding_file(restored, "logo") == tm.branding_dir() / logo_a
        assert tm.branding_urls(restored)["logo"].startswith("/branding/logo?v=")
    assert not (tm.branding_dir() / logo_b).exists()  # plus de fichier orphelin


def test_restore_branding_files_ignores_unsafe_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(tm, "branding_dir", lambda: tmp_path / "branding")
    archive_path = tmp_path / "a.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("data/media/branding/logo-0123456789ab.png", b"png")
        archive.writestr("data/media/branding/../../evil.png", b"x")
        archive.writestr("data/media/branding/sub/logo-0123456789ab.png", b"x")
        archive.writestr("data/media/branding/script.svg", b"x")
    with zipfile.ZipFile(archive_path) as archive:
        assert tm.restore_branding_files(archive, "data/media/") == ["logo-0123456789ab.png"]
    assert sorted(p.name for p in (tmp_path / "branding").iterdir()) == ["logo-0123456789ab.png"]
    assert not (tmp_path / "evil.png").exists()


async def test_reconcile_clears_missing_files(db):
    theme = await tm.get_theme(db)
    theme.logo_file = "logo-aaaaaaaaaaaa.png"
    theme.favicon_file = "favicon-bbbbbbbbbbbb.png"
    await db.flush()
    stray = tm.branding_dir() / "logo-cccccccccccc.png"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(image_bytes())
    result = await tm.reconcile_branding(db)
    assert sorted(result["cleared"]) == ["favicon", "logo"]
    assert "logo-cccccccccccc.png" in result["removed"] and not stray.exists()
    assert theme.logo_file == "" and theme.favicon_file == ""
    await db.rollback()


# ---------------------------------------------------------------- 10. fichier absent du disque
def test_missing_branding_file_falls_back_to_default_logo(admin, anonymous):
    assert upload(admin, "logo", image_bytes()).status_code == 303
    assert upload(admin, "favicon", image_bytes((64, 64))).status_code == 303
    theme = current_theme()
    assert "/branding/logo?v=" in admin.get("/").text
    for name in (theme.logo_file, theme.favicon_file):
        for path in tm._companions(name):
            path.unlink(missing_ok=True)
    urls = tm.branding_urls(current_theme())
    assert urls["logo"] == urls["logo_dark"] == urls["favicon"] == urls["favicon_32"] == ""
    for page in (admin.get("/").text, anonymous.get("/login").text):
        assert "/branding/logo" not in page and "/branding/favicon" not in page
        assert "/static/img/favicon.svg" in page
