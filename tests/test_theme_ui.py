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


# ---------------------------------------------------------------- sens d'écriture du nom et du slogan (arabe)
def _element(html: str, css_class: str) -> str:
    """Balise ouvrante du premier élément portant la classe donnée."""
    match = re.search(r'<(?:div|h1|p)[^>]*class="[^"]*\b' + re.escape(css_class) + r'\b[^"]*"[^>]*>', html)
    assert match, css_class
    return match.group(0)


def _text_dir(html: str, css_class: str) -> str:
    """Attribut dir du <bdi> qui isole le texte du bloc ``css_class`` ; le bloc lui-même ne porte aucun dir
    (il garde le sens de la page et donc l'alignement « text-start »)."""
    block = _element(html, css_class)
    assert " dir=" not in block, block
    after = html.split(block, 1)[1]
    match = re.match(r'\s*<bdi\b[^>]*?(?<!:)dir="([^"]+)"', after)
    assert match, after[:120]
    return match.group(1)


def _set_identity(admin, **fields):
    payload = {"hotel_name": "Hôtel Annaba", "hotel_name_ar": "فندق عنابة", "show_logo_on_documents": "1", **fields}
    assert admin.form("/admin/theme/documents", payload).status_code == 303


def test_french_fallback_name_and_slogan_use_dir_auto_in_arabic(admin, anonymous, in_language):
    """Slogan et nom arabes vides : le texte français de repli est isolé par dir="auto"."""
    try:
        _set_identity(admin, hotel_name="Hôtel Annaba (centre)", hotel_name_ar="", slogan="Votre escale à Annaba !",
                      slogan_ar="")
        sidebar = in_language("ar").get("/").text
        assert _text_dir(sidebar, "brand-name") == "auto" and _text_dir(sidebar, "brand-tagline") == "auto"
        assert "Hôtel Annaba (centre)" in sidebar and "Votre escale à Annaba !" in sidebar

        anonymous.form("/lang", {"lang": "ar", "next": "/login"})
        login = anonymous.get("/login").text
        assert 'dir="rtl"' in login.split("<html", 1)[1].split(">", 1)[0]
        h1 = re.search(r"<h1[^>]*>([^<]*)</h1>", login)
        assert h1 and 'dir="auto"' in h1.group(0) and h1.group(1) == "Hôtel Annaba (centre)"
        assert 'dir="auto"' in _element(login, "brand-tagline")
    finally:
        _set_identity(admin)


def test_arabic_name_and_slogan_are_rtl_when_defined(admin, in_language):
    try:
        _set_identity(admin, slogan="Votre escale à Annaba !", slogan_ar="محطتك في عنابة")
        sidebar = in_language("ar").get("/").text
        assert _text_dir(sidebar, "brand-name") == "rtl" and _text_dir(sidebar, "brand-tagline") == "rtl"
        # Interface française : nom français en dir="auto".
        french = in_language("fr").get("/").text
        assert _text_dir(french, "brand-name") == "auto"
    finally:
        _set_identity(admin)


def test_arabic_login_title_is_arabic_name(admin, anonymous):
    _set_identity(admin)
    anonymous.form("/lang", {"lang": "ar", "next": "/login"})
    login = anonymous.get("/login").text
    h1 = re.search(r"<h1[^>]*>([^<]*)</h1>", login)
    assert h1 and h1.group(1) == "فندق عنابة" and 'dir="rtl"' in h1.group(0)
    # Nom français en ligne secondaire, isolé par dir="auto".
    assert re.search(r'<p class="text-slate-300" dir="auto">Hôtel Annaba</p>', login)
    # Interface française : titre français, nom arabe en ligne secondaire.
    anonymous.form("/lang", {"lang": "fr", "next": "/login"})
    french = anonymous.get("/login").text
    assert re.search(r'<h1[^>]*dir="auto"[^>]*>Hôtel Annaba</h1>', french)
    assert re.search(r'<p class="ar text-slate-300" dir="rtl" lang="ar">فندق عنابة</p>', french)


def test_preview_texts_use_bound_direction(admin):
    html = admin.get("/admin/theme").text
    for css_class in ("pv-sidebar-name", "pv-sidebar-tagline", "pv-doc-name", "pv-doc-tagline"):
        assert _text_dir(html, css_class) == "auto", css_class
        inner = html.split(_element(html, css_class), 1)[1].split(">", 1)[0]
        assert ':dir="' in inner, css_class
    source = APP_JS.read_text(encoding="utf-8")
    assert "get nameDir()" in source and "get taglineDir()" in source


# ---------------------------------------------------------------- aperçu fidèle au rendu réel des logos
def _preview(html: str) -> str:
    return html.split("data-theme-preview", 1)[1]


def test_preview_sidebar_uses_dark_logo_or_fallback_and_document_main_logo(admin):
    preview = _preview(admin.get("/admin/theme").text)
    assert re.search(r'<img :src="sidebarLogo" src="/static/img/favicon.svg" data-preview-logo="sidebar"', preview)

    upload(admin, "logo", png())
    version = _version_now()
    preview = _preview(admin.get("/admin/theme").text)
    # Sans logo sombre : repli sur le logo principal dans la barre latérale (comme layout/base.html).
    assert f'<img :src="sidebarLogo" src="/branding/logo?v={version}" data-preview-logo="sidebar"' in preview
    assert 'class="pv-sidebar" data-preview="sidebar"' in preview
    # Document : logo principal sur fond blanc.
    doc = preview.split('data-preview="document"', 1)[1]
    assert f'src="/branding/logo?v={version}" data-preview-logo="document"' in doc

    upload(admin, "logo-dark", png(color=(255, 255, 255, 255)))
    version = _version_now()
    preview = _preview(admin.get("/admin/theme").text)
    assert f'src="/branding/logo-dark?v={version}" data-preview-logo="sidebar"' in preview
    assert f'src="/branding/logo?v={version}" data-preview-logo="document"' in preview
    # Barre supérieure : aucun logo (le layout réel n'en affiche pas).
    topbar = preview.split('class="pv-topbar"', 1)[1].split("</div>", 1)[0]
    assert "<img" not in topbar
    assert "headerLogo" not in APP_JS.read_text(encoding="utf-8")


def _version_now() -> int:
    return asyncio.run(_version())


@pytest.mark.parametrize("lang, logo_help, dark_help, preview_help", [
    ("fr", "Fond clair : documents PDF (factures, fiches de police, tickets 80 mm) et pages d&#39;erreur en mode clair.",
     "Fond foncé : barre latérale, page de connexion et pages d&#39;erreur en mode sombre.",
     "La barre latérale, toujours foncée, affiche le logo sombre"),
    ("en", "Light background: PDF documents (invoices, police registration forms, 80 mm tickets) and error pages in light mode.",
     "Dark background: sidebar, sign-in page and error pages in dark mode.",
     "The sidebar, always dark, shows the dark logo"),
    ("ar", "خلفية فاتحة: وثائق PDF (الفواتير وبطاقات الشرطة والتذاكر 80 مم) وصفحات الخطأ في الوضع الفاتح.",
     "خلفية داكنة: الشريط الجانبي وصفحة تسجيل الدخول وصفحات الخطأ في الوضع الداكن.",
     "يعرض الشريط الجانبي، الداكن دائمًا، الشعار الداكن"),
], ids=["fr", "en", "ar"])
def test_logo_help_texts_match_real_rendering(in_language, lang, logo_help, dark_help, preview_help):
    html = in_language(lang).get("/admin/theme").text
    unescaped = html.replace("&#39;", "'")
    for text in (logo_help, dark_help, preview_help):
        assert text.replace("&#39;", "'") in unescaped, text
    assert "barre supérieure" not in html and "top bar" not in html and "الشريط العلوي" not in html


# ---------------------------------------------------------------- aperçu « Document » : logo imprimable uniquement
SVG_LOGO = (b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="40" viewBox="0 0 120 40">'
            b'<rect width="120" height="40" fill="#7c3aed"/></svg>')

# Harnais Node : exécute app.js avec un DOM minimal, puis instancie le composant Alpine « themeEditor » à partir du
# bloc JSON réel de la page et renvoie docLogo / docLogoUnprintable pour chaque scénario (sélection locale ou non).
EDITOR_HARNESS = r"""
var fs = require("fs");
var editorJson = process.argv[2], scenarios = JSON.parse(process.argv[3]);
var listeners = {}, registry = {};
function on(type, fn) { (listeners[type] = listeners[type] || []).push(fn); }
global.CustomEvent = function () {};
global.window = global;
global.document = {
  documentElement: { classList: { contains: function () { return false; }, toggle: function () {} } },
  querySelector: function () { return null; }, querySelectorAll: function () { return []; },
  getElementById: function (id) { return id === "theme-editor-data" ? { textContent: editorJson } : null; },
  addEventListener: on, dispatchEvent: function () {}
};
window.addEventListener = function () {};
window.getComputedStyle = function () { return { getPropertyValue: function () { return ""; } }; };
window.Alpine = { data: function (name, factory) { registry[name] = factory; }, store: function () {} };
eval(fs.readFileSync(process.argv[1], "utf8"));
(listeners["alpine:init"] || []).forEach(function (fn) { fn(); });
var out = scenarios.map(function (s) {
  var editor = registry.themeEditor();
  if (s.pending) { editor.pending.logo = s.pending; editor.pendingTypes.logo = s.type; }
  return { logo: editor.docLogo, unprintable: editor.docLogoUnprintable };
});
process.stdout.write(JSON.stringify(out));
"""


def _doc_preview_states(html: str, scenarios: list[dict]) -> list[dict]:
    match = EDITOR_DATA_RE.search(html)
    assert match
    result = subprocess.run(["node", "-e", EDITOR_HARNESS, str(APP_JS), match.group(1), json.dumps(scenarios)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _document_preview(html: str) -> str:
    return _preview(html).split('data-preview="document"', 1)[1].split("</aside>", 1)[0]


def test_saved_svg_logo_not_shown_in_document_preview(admin):
    """Logo principal SVG enregistré (sans rendu PNG) : aucun PDF ne l'imprime, l'aperçu Document non plus."""
    upload(admin, "logo", SVG_LOGO, filename="logo.svg", content_type="image/svg+xml")
    version = _version_now()
    html = admin.get("/admin/theme").text
    data = editor_data(html)
    assert data["current"]["logo"] == f"/branding/logo?v={version}" and data["doc_logo"] is True
    assert data["logo_printable"] is False
    doc = _document_preview(html)
    image = re.search(r'<img[^>]*data-preview-logo="document"[^>]*>', doc).group(0)
    assert " src=" not in image.replace(':src="docLogo"', "")  # pas de logo rendu côté serveur
    assert 'x-show="docs.logo &amp;&amp; docLogoUnprintable"' in doc or 'x-show="docs.logo && docLogoUnprintable"' in doc
    assert "data-logo-unprintable" in html
    # La barre latérale continue d'afficher le SVG (le navigateur sait l'afficher).
    assert f'src="/branding/logo?v={version}" data-preview-logo="sidebar"' in _preview(html)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js absent")
def test_document_preview_logo_follows_printability(admin):
    upload(admin, "logo", SVG_LOGO, filename="logo.svg", content_type="image/svg+xml")
    svg_page = admin.get("/admin/theme").text
    saved_svg, pending_png, pending_svg = _doc_preview_states(svg_page, [
        {}, {"pending": "blob:png", "type": "image/png"}, {"pending": "blob:svg", "type": "image/svg+xml"}])
    assert saved_svg == {"logo": "", "unprintable": True}
    assert pending_png == {"logo": "blob:png", "unprintable": False}
    assert pending_svg == {"logo": "", "unprintable": True}

    upload(admin, "logo", png())
    png_page = admin.get("/admin/theme").text
    assert editor_data(png_page)["logo_printable"] is True
    assert "data-logo-unprintable" not in png_page
    version = _version_now()
    (saved_png, pending_svg) = _doc_preview_states(png_page, [{}, {"pending": "blob:svg", "type": "image/svg+xml"}])
    assert saved_png == {"logo": f"/branding/logo?v={version}", "unprintable": False}
    assert pending_svg == {"logo": "", "unprintable": True}
    assert f'src="/branding/logo?v={version}" data-preview-logo="document"' in _document_preview(png_page)


def test_error_page_shows_dark_logo_alone_in_dark_mode(admin, reception):
    """Logo sombre seul : la page d'erreur l'affiche en mode sombre (comme l'indique l'aide)."""
    upload(admin, "logo-dark", png(color=(255, 255, 255, 255)))
    version = _version_now()
    admin.get("/")  # dernier rendu connu : charte courante
    html = reception.get("/admin/theme").text
    images = re.findall(r'<img[^>]*class="brand-logo[^"]*"[^>]*>', html)
    assert len(images) == 1
    assert f'src="/branding/logo-dark?v={version}"' in images[0] and "dark:block" in images[0]


# ---------------------------------------------------------------- alignement réel de la barre latérale (Chrome)
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
MEASURE_SCRIPT = """<script>
var out = {}, logo = document.querySelector("aside a img").getBoundingClientRect();
["brand-name", "brand-tagline"].forEach(function (cls) {
  var block = document.querySelector("aside ." + cls), text = block.querySelector("bdi");
  var b = block.getBoundingClientRect(), t = text.getBoundingClientRect();
  out[cls] = { blockDir: getComputedStyle(block).direction, textDir: getComputedStyle(text).direction,
               blockLeft: b.left, blockRight: b.right, textLeft: t.left, textRight: t.right };
});
out.logoLeft = logo.left;
document.body.setAttribute("data-measure", JSON.stringify(out));
</script>"""


@pytest.mark.skipif(CHROME is None, reason="Chrome absent")
def test_rtl_sidebar_fallback_text_aligned_next_to_logo(admin, in_language, tmp_path):
    """Arabe sans nom ni slogan arabes : le texte français isolé (ltr) reste aligné côté logo (à droite)."""
    try:
        _set_identity(admin, hotel_name="Hôtel (centre)", hotel_name_ar="", slogan="Escale !", slogan_ar="")
        html = in_language("ar").get("/").text
    finally:
        _set_identity(admin)
    static = APP_JS.parent.parent
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    html = html.replace('href="/static/', f'href="file://{static}/').replace('src="/static/', f'src="file://{static}/')
    page = tmp_path / "sidebar.html"
    page.write_text(html.replace("</body>", MEASURE_SCRIPT + "</body>"), encoding="utf-8")
    result = subprocess.run([CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--allow-file-access-from-files",
                             "--window-size=1280,800", "--dump-dom", page.as_uri()],
                            capture_output=True, text=True, timeout=60)
    match = re.search(r'data-measure="([^"]+)"', result.stdout)
    assert match, result.stderr[-500:]
    measure = json.loads(match.group(1).replace("&quot;", '"'))
    for cls in ("brand-name", "brand-tagline"):
        m = measure[cls]
        assert m["blockDir"] == "rtl" and m["textDir"] == "ltr", (cls, m)
        # Texte collé au bord droit du bloc (côté logo), et non au bord gauche.
        assert abs(m["blockRight"] - m["textRight"]) < 1.5, (cls, m)
        assert m["textLeft"] - m["blockLeft"] > 10, (cls, m)
    assert measure["logoLeft"] >= measure["brand-name"]["blockRight"] - 1  # logo à droite du texte
