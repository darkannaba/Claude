"""Interface de personnalisation (Administration > Thème et marque) et intégration de la charte au layout.

Vérifie : page d'administration en fr / en / ar (RTL), sélecteurs de couleur et composant d'aperçu en direct,
feuille theme-custom.css versionnée chargée après app.css, logo et favicon personnalisés dans la barre
latérale, la page de connexion et les pages d'erreur, lien dans la navigation d'administration, cohérence
de l'algorithme de nuances JavaScript (aperçu) avec palette() du serveur.
"""
import asyncio
import json
import re
import shutil
import subprocess
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.database import SessionLocal
from app.services import theme_manager as tm
from app.templating import templates

APP_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "app.js"
STYLESHEETS_RE = re.compile(r'<link rel="stylesheet" href="([^"]+)"')
EDITOR_DATA_RE = re.compile(r'<script type="application/json" id="theme-editor-data">(.*?)</script>', re.DOTALL)


def png(size=(240, 80), color=(13, 148, 136, 255)) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


async def _reset_theme() -> None:
    async with SessionLocal() as db:
        theme = await tm.get_theme(db)
        for kind, field in tm.KIND_FIELDS.items():
            if getattr(theme, field):
                await tm.delete_image(db, kind)
        for field, value in tm.DEFAULT_THEME.items():
            setattr(theme, field, value)
        await db.commit()


async def _version() -> int:
    async with SessionLocal() as db:
        return (await tm.get_theme(db)).version


@pytest.fixture(autouse=True)
def clean_theme(app):
    asyncio.run(_reset_theme())
    yield
    asyncio.run(_reset_theme())


@pytest.fixture()
def in_language(admin):
    """Bascule la session admin dans une langue, puis revient au français."""
    def switch(lang):
        admin.form("/lang", {"lang": lang, "next": "/admin/theme"})
        return admin
    yield switch
    admin.form("/lang", {"lang": "fr", "next": "/"})


def upload(client, kind, content, filename="logo.png", content_type="image/png"):
    response = client.post(f"/admin/theme/upload/{kind}", data={"csrf_token": client.csrf},
                           files={"file": (filename, content, content_type)}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response


def editor_data(html: str) -> dict:
    match = EDITOR_DATA_RE.search(html)
    assert match, "bloc JSON #theme-editor-data absent"
    return json.loads(match.group(1))


# ---------------------------------------------------------------- page d'administration
@pytest.mark.parametrize("lang, direction, title", [
    ("fr", "ltr", "Thème et image de marque"),
    ("en", "ltr", "Theme &amp; branding"),
    ("ar", "rtl", "المظهر والهوية البصرية"),
])
def test_admin_page_renders_in_each_language(in_language, lang, direction, title):
    client = in_language(lang)
    response = client.get("/admin/theme")
    assert response.status_code == 200
    html = response.text
    assert f'lang="{lang}" dir="{direction}"' in html
    assert title in html
    # Sélecteurs de couleur + champ hexadécimal synchronisé pour les 7 couleurs de la charte
    for field in tm.COLOR_FIELDS:
        assert f'type="color" name="{field}"' in html
        assert f'data-hex-for="{field}"' in html
    # Composant d'aperçu en direct (aucun script en ligne : données JSON + composant défini dans app.js)
    assert 'x-data="themeEditor"' in html and "data-theme-preview" in html and 'class="theme-preview"' in html
    data = editor_data(html)
    assert data["font"] == "inter" and data["density"] == "comfortable"
    assert {f["name"] for f in data["fields"]} == set(tm.COLOR_FIELDS)
    assert data["tokens"]["shade_mix"]["600"] == ["white", 0.0]
    assert data["rtl"] is (direction == "rtl")
    # Toutes les sections et routes POST du socle
    for action in ("/admin/theme/colors", "/admin/theme/typography", "/admin/theme/documents",
                   "/admin/theme/upload/logo", "/admin/theme/upload/logo-dark", "/admin/theme/upload/favicon",
                   "/admin/theme/reset"):
        assert f'action="{action}"' in html
    assert 'accept="image/png,image/webp,image/jpeg,image/svg+xml"' in html
    assert 'accept="image/png,image/webp,image/jpeg"' in html  # favicon : pas de SVG
    for key in ("inter", "roboto", "tajawal", "system"):
        assert f'name="font_family" value="{key}"' in html
    assert 'name="density" value="compact"' in html and 'name="density" value="comfortable"' in html
    assert 'name="show_logo_on_documents" value="1"' in html
    assert 'name="hotel_name" form="theme-documents-form"' in html
    # Confirmation de réinitialisation traduite côté serveur
    assert re.search(r'action="/admin/theme/reset" data-confirm="[^"]+"', html)


def test_admin_page_translations_are_applied(in_language):
    english = in_language("en").get("/admin/theme").text
    assert "Live preview" in english and "Reset default colours" in english and "Save colours" in english
    assert "Aperçu en direct" not in english
    arabic = in_language("ar").get("/admin/theme").text
    assert "معاينة مباشرة" in arabic and "حفظ الألوان" in arabic
    # Aperçu initial en arabe : textes d'exemple arabes
    assert "لوحة القيادة" in arabic


def test_identity_section_saves_names_and_slogans(admin):
    payload = {"hotel_name": "Hôtel Seybouse", "hotel_name_ar": "فندق سيبوس", "slogan": "Au bord de la mer",
               "slogan_ar": "على شاطئ البحر", "document_header": "", "document_footer": "", "show_logo_on_documents": "1"}
    try:
        assert admin.form("/admin/theme/documents", payload).status_code == 303
        page = admin.get("/admin/theme").text
        assert 'value="Hôtel Seybouse"' in page and "Au bord de la mer" in page
        assert "Hôtel Seybouse" in admin.get("/").text
    finally:
        admin.form("/admin/theme/documents", {"hotel_name": "Hôtel Annaba", "hotel_name_ar": "فندق عنابة",
                                              "show_logo_on_documents": "1"})


def test_slogan_shown_in_sidebar_and_login_by_language(admin, anonymous, in_language):
    admin.form("/admin/theme/documents", {"slogan": "Votre escale à Annaba", "slogan_ar": "محطتك في عنابة",
                                          "show_logo_on_documents": "1"})
    assert "Votre escale à Annaba" in admin.get("/").text
    assert "Votre escale à Annaba" in anonymous.get("/login").text
    arabic = in_language("ar").get("/").text
    assert "محطتك في عنابة" in arabic and "Votre escale à Annaba" not in arabic


def test_admin_nav_links_to_theme_page(admin, in_language):
    html = admin.get("/admin").text
    assert 'href="/admin/theme"' in html and "Thème et marque" in html
    current = admin.get("/admin/theme").text
    assert re.search(r'<a href="/admin/theme" class="btn-primary btn-sm">', current)
    assert "Theme &amp; branding" in in_language("en").get("/admin").text


def test_theme_page_forbidden_for_reception(reception):
    assert reception.get("/admin/theme").status_code == 403


# ---------------------------------------------------------------- feuille dynamique dans le layout
def test_layout_loads_theme_css_after_app_css_with_version(admin, anonymous):
    version = asyncio.run(_version())
    for client, url in ((admin, "/"), (admin, "/admin/theme"), (anonymous, "/login")):
        sheets = STYLESHEETS_RE.findall(client.get(url).text)
        assert len(sheets) >= 2
        assert sheets[0].startswith("/static/css/app.css")
        assert sheets[1] == f"/static/css/theme-custom.css?v={version}", (url, sheets)
    admin.form("/admin/theme/colors", {"primary_color": "#7c3aed"})
    sheets = STYLESHEETS_RE.findall(admin.get("/").text)
    assert sheets[1] == f"/static/css/theme-custom.css?v={asyncio.run(_version())}"
    assert asyncio.run(_version()) > version
    css = anonymous.get(sheets[1])
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert "--color-primary: #7c3aed;" in css.text


def test_error_page_uses_theme_stylesheet(admin, reception):
    admin.get("/")  # dernier rendu connu : charte courante
    response = reception.get("/admin/theme")
    assert response.status_code == 403
    sheets = STYLESHEETS_RE.findall(response.text)
    assert sheets[0].startswith("/static/css/app.css") and sheets[1].startswith("/static/css/theme-custom.css")


def test_default_favicon_and_logo_without_upload(admin, anonymous):
    for html in (admin.get("/").text, anonymous.get("/login").text):
        assert '<link rel="icon" href="/static/img/favicon.svg" type="image/svg+xml">' in html
        assert "/branding/" not in html
        assert 'src="/static/img/favicon.svg"' in html


# ---------------------------------------------------------------- logo et favicon personnalisés
def test_custom_logo_in_sidebar_and_login_after_upload(admin, anonymous):
    upload(admin, "logo", png())
    version = asyncio.run(_version())
    logo_url = f"/branding/logo?v={version}"
    dashboard = admin.get("/").text
    assert f'<img src="{logo_url}" alt="Logo de l&#39;établissement"' in dashboard or \
        f'<img src="{logo_url}" alt="Logo de l\'établissement"' in dashboard
    assert re.search(r'<img src="/branding/logo\?v=\d+"[^>]*class="brand-logo h-9 w-auto[^"]*object-contain', dashboard)
    login = anonymous.get("/login").text
    assert f'src="{logo_url}"' in login and "object-contain" in login
    assert anonymous.get(logo_url).headers["content-type"] == "image/png"
    # Variante sombre : prioritaire dans la barre latérale et sur la page de connexion (fonds foncés)
    upload(admin, "logo-dark", png(color=(255, 255, 255, 255)))
    version = asyncio.run(_version())
    assert f'src="/branding/logo-dark?v={version}"' in admin.get("/").text
    assert f'src="/branding/logo-dark?v={version}"' in anonymous.get("/login").text


def test_logo_alt_text_translated_and_rtl(admin, in_language):
    upload(admin, "logo", png())
    arabic = in_language("ar").get("/").text
    assert 'alt="شعار المؤسسة"' in arabic and 'dir="rtl"' in arabic
    assert 'class="brand-sidebar' in arabic and "text-start" in arabic
    english = in_language("en").get("/").text
    assert 'alt="Property logo"' in english


def test_custom_favicon_in_layout(admin, anonymous):
    upload(admin, "favicon", png((128, 128)))
    version = asyncio.run(_version())
    for html in (admin.get("/").text, anonymous.get("/login").text):
        assert f'<link rel="icon" type="image/png" sizes="64x64" href="/branding/favicon?v={version}">' in html
        assert f'<link rel="icon" type="image/png" sizes="32x32" href="/branding/favicon?size=32&amp;v={version}">' in html
        assert "/static/img/favicon.svg\" type=\"image/svg+xml\"" not in html
    small = anonymous.get(f"/branding/favicon?size=32&v={version}")
    assert small.status_code == 200 and Image.open(BytesIO(small.content)).size == (32, 32)


def test_admin_page_shows_current_files_and_delete_forms(admin):
    upload(admin, "logo", png())
    upload(admin, "favicon", png((64, 64)))
    data = editor_data(admin.get("/admin/theme").text)
    version = asyncio.run(_version())
    assert data["current"]["logo"] == f"/branding/logo?v={version}"
    assert data["current"]["favicon"] == f"/branding/favicon?v={version}"
    assert data["current"]["logo-dark"] == ""  # pas de repli : aucun fichier sombre enregistré
    html = admin.get("/admin/theme").text
    assert 'action="/admin/theme/delete/logo"' in html and 'action="/admin/theme/delete/favicon"' in html
    assert 'action="/admin/theme/delete/logo-dark"' not in html


# ---------------------------------------------------------------- aperçu : algorithme JavaScript
def test_js_preview_component_defined_in_app_js():
    source = APP_JS.read_text(encoding="utf-8")
    assert 'Alpine.data("themeEditor"' in source
    assert "URL.createObjectURL" in source and "URL.revokeObjectURL" in source
    assert "roundHalfEven" in source


def test_theme_tokens_match_server_algorithm():
    tokens = templates.env.globals["theme_tokens"]
    assert tokens["shade_mix"] == {str(k): [t, w] for k, (t, w) in tm.SHADE_MIX.items()}
    assert tokens["known_palettes"]["#0d9488"]["600"] == "#0d9488"
    assert tokens["fonts"]["tajawal"]["rtl"].startswith("Tajawal")
    assert tokens["densities"] == tm.DENSITIES
    assert tokens["max_upload_bytes"] == tm.MAX_UPLOAD_BYTES


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js absent")
def test_js_palette_identical_to_python():
    """Exécute le module de couleurs d'app.js sous Node et compare avec palette() / contrast_text()."""
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("  var themeColor = (function () {")
    end = source.index("  window.themeColor = themeColor;")
    samples = ["#7c3aed", "#123456", "#f5d90a", "#ffffff", "#000000", "#0d9488", "#0284c7", "#808080", "#e11d48"]
    tokens = templates.env.globals["theme_tokens"]
    script = (source[start:end] + "\nvar tokens = " + json.dumps(tokens) + ";\nvar out = {};\n"
              + "JSON.parse(process.argv[1]).forEach(function (c) { out[c] = { palette: themeColor.palette(c, tokens),"
              + " text: themeColor.contrastText(c, tokens), dark: themeColor.mix('#0f172a', c, 0.05) }; });\n"
              + "process.stdout.write(JSON.stringify(out));\n")
    result = subprocess.run(["node", "-e", script, json.dumps(samples)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    computed = json.loads(result.stdout)
    for color in samples:
        expected = {str(k): v for k, v in tm.palette(color).items()}
        assert computed[color]["palette"] == expected, color
        assert computed[color]["text"] == tm.contrast_text(color), color
        assert computed[color]["dark"] == tm.mix("#0f172a", color, 0.05), color


# ---------------------------------------------------------------- feuille compilée
def test_compiled_css_contains_preview_and_logical_classes():
    css = (APP_JS.parent.parent / "css" / "app.css").read_text(encoding="utf-8")
    for selector in (".theme-preview", ".pv-dark", ".pv-room-available", ".pv-badge-maintenance", ".text-start"):
        assert selector in css, selector
    assert "rgb(var(--brand-600)" in css
