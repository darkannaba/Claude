"""Thématisation white-label : valeurs par défaut, CSS dynamique, palette, contraste, fichiers de marque, admin."""
import asyncio
import re
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import select

from app.database import SessionLocal
from app.models.audit import AuditLog
from app.models.theme import ThemeSettings
from app.services import theme_manager as tm
from app.services.common import BusinessError
from app.services.settings import load_hotel_settings, save_settings_subset

CSS_URL = "/static/css/theme-custom.css"


# ---------------------------------------------------------------- outils
def image_bytes(size=(200, 100), fmt="PNG", mode="RGBA", color=(13, 148, 136, 255)) -> bytes:
    buffer = BytesIO()
    if mode == "RGB" and len(color) == 4:
        color = color[:3]
    Image.new(mode, size, color).save(buffer, format=fmt)
    return buffer.getvalue()


SAFE_SVG = (b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 10 10">'
            b'<defs><linearGradient id="g"><stop offset="0" stop-color="#0d9488"/></linearGradient></defs>'
            b'<style>.a { fill: #0d9488; }</style>'
            b'<rect class="a" width="10" height="10" fill="url(#g)"/><use xlink:href="#g"/></svg>')

MALICIOUS_SVGS = {
    "script": b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    "onload": b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><rect width="1" height="1"/></svg>',
    "javascript": b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
                  b'<a xlink:href="java&#x09;script:alert(1)"><rect width="1" height="1"/></a></svg>',
    "foreignObject": b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><div>x</div></foreignObject></svg>',
    "entity": b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]><svg xmlns="http://www.w3.org/2000/svg">&a;</svg>',
    "external": b'<svg xmlns="http://www.w3.org/2000/svg"><image href="http://evil.example/x.png"/></svg>',
    "css-import": b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(http://evil.example/a.css);</style></svg>',
    "style-attr": b'<svg xmlns="http://www.w3.org/2000/svg"><rect style="background:url(http://evil.example/)"/></svg>',
    "animate-href": b'<svg xmlns="http://www.w3.org/2000/svg"><a><set attributeName="href" to="#x"/></a></svg>',
    "iframe": b'<svg xmlns="http://www.w3.org/2000/svg"><iframe src="/"/></svg>',
}


async def _reset_theme() -> None:
    async with SessionLocal() as db:
        theme = await tm.get_theme(db)
        for kind in tm.KIND_FIELDS:
            if getattr(theme, tm.KIND_FIELDS[kind]):
                await tm.delete_image(db, kind)
        for field, value in tm.DEFAULT_THEME.items():
            setattr(theme, field, value)
        await db.commit()


@pytest.fixture(autouse=True)
def clean_theme(app):
    asyncio.run(_reset_theme())
    yield
    asyncio.run(_reset_theme())


async def _theme() -> ThemeSettings:
    async with SessionLocal() as db:
        return await tm.get_theme(db)


def current_theme() -> ThemeSettings:
    return asyncio.run(_theme())


def upload(client, kind, content, filename="logo.png", content_type="image/png"):
    return client.post(f"/admin/theme/upload/{kind}", data={"csrf_token": client.csrf},
                       files={"file": (filename, content, content_type)}, follow_redirects=False)


def flashes(client) -> str:
    return client.get("/admin/theme").text


def css_var(css: str, name: str) -> str:
    match = re.search(rf"--{name}:\s*([^;]+);", css)
    assert match, f"variable --{name} absente"
    return match.group(1).strip()


# ---------------------------------------------------------------- valeurs par défaut
async def test_default_row_created_with_original_palette():
    async with SessionLocal() as db:
        existing = await db.get(ThemeSettings, 1)
        if existing is not None:
            await db.delete(existing)
            await db.commit()
    async with SessionLocal() as db:
        theme = await tm.get_theme(db)
        await db.commit()
        assert theme.id == 1 and theme.version == 1
        assert theme.primary_color == "#0d9488" and theme.secondary_color == "#0284c7"
        assert theme.card_color == "#ffffff"
        assert (theme.status_available_color, theme.status_occupied_color, theme.status_dirty_color,
                theme.status_maintenance_color) == ("#10b981", "#0ea5e9", "#f59e0b", "#f43f5e")
        assert theme.font_family == "inter" and theme.density == "comfortable"
        assert theme.logo_file == theme.favicon_file == "" and theme.show_logo_on_documents is True
    async with SessionLocal() as db:
        assert len((await db.scalars(select(ThemeSettings))).all()) == 1
        await tm.get_theme(db)  # deuxième appel : aucune nouvelle ligne
        assert len((await db.scalars(select(ThemeSettings))).all()) == 1
    await tm.ensure_theme()


def test_default_css_keeps_teal_palette(anonymous):
    response = anonymous.get(CSS_URL)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert response.headers["cache-control"] == "no-cache"
    css = response.text
    assert css_var(css, "brand-600") == "13 148 136"
    assert css_var(css, "brand-50") == "240 253 250"      # teal-50 exact
    assert css_var(css, "accent-600") == "2 132 199"      # sky-600
    assert css_var(css, "color-primary") == "#0d9488"
    assert css_var(css, "color-on-primary") == "#ffffff"
    assert css_var(css, "status-available") == "#10b981"
    assert css_var(css, "status-maintenance") == "#f43f5e"
    assert css_var(css, "font-family").startswith("Inter")
    assert ".dark {" in css and 'html[dir="rtl"]' in css
    assert "Densité compacte" not in css


# ---------------------------------------------------------------- palette et contraste
def test_palette_format_and_bounds():
    shades = tm.palette("#336699")
    assert list(shades) == list(tm.SHADES)
    assert all(re.fullmatch(r"#[0-9a-f]{6}", value) for value in shades.values())
    assert shades[600] == "#336699"
    luminances = [tm.relative_luminance(shades[s]) for s in tm.SHADES]
    assert luminances == sorted(luminances, reverse=True)  # du plus clair (50) au plus foncé (950)
    assert tm.palette("#ffffff")[50] == "#ffffff" and tm.palette("#000000")[950] == "#000000"
    white_extremes = tm.palette("#FFFFFF")
    assert all(0 <= int(value[i:i + 2], 16) <= 255 for value in white_extremes.values() for i in (1, 3, 5))
    assert tm.palette("#0D9488") == tm.KNOWN_PALETTES["#0d9488"]
    assert tm.rgb_triplet("#0a0b0c") == "10 11 12"
    assert tm.mix("#000000", "#ffffff", 0.5) == "#808080"


def test_contrast_text_on_light_and_dark_colours():
    assert tm.contrast_text("#fde047") == tm.DARK_TEXT      # jaune clair → texte foncé
    assert tm.contrast_text("#ffffff") == tm.DARK_TEXT
    assert tm.contrast_text("#1e3a8a") == tm.LIGHT_TEXT     # bleu nuit → texte blanc
    assert tm.contrast_text("#0d9488") == tm.LIGHT_TEXT     # charte d'origine : boutons blancs conservés
    assert tm.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert tm.contrast_text("#22d3ee") == tm.DARK_TEXT      # cyan clair : blanc < 3:1 et foncé meilleur


@pytest.mark.parametrize("value", ["#abc", "#zzzzzz", "0d9488", "teal", "", "#0d94880", "rgb(0,0,0)"])
def test_invalid_colours_rejected(value):
    with pytest.raises(BusinessError):
        tm.normalize_hex(value)


def test_normalize_hex_lowercases():
    assert tm.normalize_hex("  #0D9488 ") == "#0d9488"


# ---------------------------------------------------------------- couleurs via l'administration
def test_colours_update_regenerates_css_and_etag(admin, anonymous):
    first = anonymous.get(CSS_URL)
    etag = first.headers["etag"]
    assert anonymous.get(CSS_URL, headers={"If-None-Match": etag}).status_code == 304
    version = current_theme().version

    response = admin.form("/admin/theme/colors", {
        "primary_color": "#1E3A8A", "secondary_color": "#f97316", "card_color": "#fffbeb",
        "status_available_color": "#22c55e", "status_occupied_color": "#3b82f6",
        "status_dirty_color": "#eab308", "status_maintenance_color": "#dc2626",
    })
    assert response.status_code == 303 and response.headers["location"] == "/admin/theme"
    theme = current_theme()
    assert theme.primary_color == "#1e3a8a" and theme.version == version + 1

    second = anonymous.get(CSS_URL, headers={"If-None-Match": etag})
    assert second.status_code == 200 and second.headers["etag"] != etag
    css = second.text
    assert css_var(css, "brand-600") == "30 58 138"
    assert css_var(css, "color-primary") == "#1e3a8a"
    assert css_var(css, "color-on-primary") == "#ffffff"
    assert css_var(css, "accent-600") == "249 115 22"
    assert css_var(css, "color-card") == "#fffbeb"
    assert css_var(css, "status-available") == "#22c55e"
    assert css_var(css, "status-dirty") == "#eab308"
    assert css_var(css, "status-maintenance") == "#dc2626"
    assert anonymous.get(CSS_URL, headers={"If-None-Match": f'W/{second.headers["etag"]}'}).status_code == 304
    assert anonymous.get(CSS_URL, headers={"If-None-Match": "*"}).status_code == 304

    # Couleur primaire claire : le texte des boutons passe en foncé
    admin.form("/admin/theme/colors", {"primary_color": "#fde047"})
    css = anonymous.get(CSS_URL).text
    assert css_var(css, "color-on-primary") == tm.DARK_TEXT

    async def logged():
        async with SessionLocal() as db:
            return (await db.scalars(select(AuditLog).where(AuditLog.action == "theme_colors_saved"))).all()
    assert asyncio.run(logged())


def test_invalid_colour_rejected_cleanly(admin):
    before = current_theme()
    for bad in ("#abc", "#zzzzzz"):
        response = admin.form("/admin/theme/colors", {"primary_color": bad})
        assert response.status_code == 303
        assert "Couleur invalide" in flashes(admin)
    after = current_theme()
    assert after.primary_color == before.primary_color and after.version == before.version
    admin.form("/admin/theme/colors", {"card_color": "#111111"})
    assert "doit rester claire" in flashes(admin)
    admin.form("/admin/theme/colors", {"unrelated": "x"})
    assert "Aucune couleur" in flashes(admin)
    assert current_theme().card_color == "#ffffff"


def test_reset_colours(admin):
    admin.form("/admin/theme/colors", {"primary_color": "#7c3aed", "status_dirty_color": "#000000"})
    admin.form("/admin/theme/typography", {"font_family": "roboto", "density": "compact"})
    assert current_theme().primary_color == "#7c3aed"
    response = admin.form("/admin/theme/reset")
    assert response.status_code == 303
    theme = current_theme()
    assert all(getattr(theme, field) == tm.DEFAULT_THEME[field] for field in tm.COLOR_FIELDS)
    assert theme.font_family == "roboto" and theme.density == "compact"  # seules les couleurs sont rétablies
    assert "Couleurs par défaut rétablies" in flashes(admin)


# ---------------------------------------------------------------- typographie et densité
def test_typography_and_density(admin, anonymous):
    response = admin.form("/admin/theme/typography", {"font_family": "tajawal", "density": "compact"})
    assert response.status_code == 303
    css = anonymous.get(CSS_URL).text
    assert css_var(css, "font-family").startswith("Tajawal")
    assert css_var(css, "font-family-rtl").startswith("Tajawal")
    assert css_var(css, "density-btn-y") == "0.3125rem"
    assert css_var(css, "density-card-p") == "0.875rem"
    assert css_var(css, "density-btn-sm-y") == "0.25rem"
    assert css_var(css, "density-title-gap") == "0.625rem"
    # Densité portée uniquement par les variables : aucune règle de composant qui écraserait p-0, pl-9, mb-0
    for rule in (".btn {", ".input {", ".card {", ".card-title {", ".table td {", ".btn-sm {"):
        assert rule not in css

    admin.form("/admin/theme/typography", {"font_family": "system", "density": "comfortable"})
    css = anonymous.get(CSS_URL).text
    assert css_var(css, "font-family").startswith("system-ui")
    assert css_var(css, "font-family-rtl").startswith('"Noto Sans Arabic"')
    assert "Densité compacte" not in css and css_var(css, "density-btn-y") == "0.5rem"

    admin.form("/admin/theme/typography", {"font_family": "comic", "density": "comfortable"})
    assert "Police inconnue" in flashes(admin)
    admin.form("/admin/theme/typography", {"font_family": "inter", "density": "huge"})
    assert "affichage inconnue" in flashes(admin)
    assert current_theme().font_family == "system"


def test_font_faces_declared_for_local_woff2(tmp_path, monkeypatch):
    (tmp_path / "inter.woff2").write_bytes(b"wOF2")
    (tmp_path / "tajawal-700.woff2").write_bytes(b"wOF2")
    (tmp_path / "system.woff2").write_bytes(b"wOF2")      # police système : jamais déclarée
    (tmp_path / "unknown-400.woff2").write_bytes(b"wOF2")
    monkeypatch.setattr(tm, "UI_FONTS_DIR", tmp_path)
    css = tm.build_css(ThemeSettings(id=1, version=3, **tm.DEFAULT_THEME))
    assert 'font-family: "Inter"' in css and "font-weight: 100 900" in css
    assert 'font-family: "Tajawal"' in css and "font-weight: 700" in css
    assert css.count("@font-face") == 2
    monkeypatch.setattr(tm, "UI_FONTS_DIR", tmp_path / "absent")
    assert "@font-face" not in tm.build_css(ThemeSettings(id=1, version=3, **tm.DEFAULT_THEME))


def test_build_css_revalidates_tampered_values():
    theme = ThemeSettings(id=1, version=2, **{**tm.DEFAULT_THEME, "primary_color": "red;}body{x:1",
                                              "font_family": "evil", "density": "nope"})
    css = tm.build_css(theme)
    assert "red;" not in css and css_var(css, "color-primary") == "#0d9488"
    assert css_var(css, "font-family").startswith("Inter")


# ---------------------------------------------------------------- documents
def test_documents_texts_and_identity_saved(admin):
    response = admin.form("/admin/theme/documents", {
        "slogan": "  L'hospitalité au bord de la mer  ", "slogan_ar": "الضيافة على شاطئ البحر",
        "document_header": "Hôtel classé 3 étoiles", "document_footer": "Merci de votre confiance",
        "hotel_nif": "001223344556677", "hotel_rc": "23B0123456",
    })
    assert response.status_code == 303
    theme = current_theme()
    assert theme.slogan == "L'hospitalité au bord de la mer"
    assert theme.slogan_ar == "الضيافة على شاطئ البحر"
    assert theme.document_header == "Hôtel classé 3 étoiles"
    assert theme.document_footer == "Merci de votre confiance"
    assert theme.show_logo_on_documents is False  # case décochée (absente du formulaire)

    async def identity():
        async with SessionLocal() as db:
            return await load_hotel_settings(db)
    hs = asyncio.run(identity())
    assert hs.hotel_nif == "001223344556677" and hs.hotel_rc == "23B0123456"

    admin.form("/admin/theme/documents", {"show_logo_on_documents": "1", "slogan": "x" * 201})
    assert "Texte trop long" in flashes(admin)
    assert current_theme().slogan == "L'hospitalité au bord de la mer"
    admin.form("/admin/theme/documents", {"show_logo_on_documents": "1"})
    assert current_theme().show_logo_on_documents is True

    async def restore():
        async with SessionLocal() as db:
            await save_settings_subset(db, {"hotel_nif": "", "hotel_rc": ""})
            await db.commit()
    asyncio.run(restore())


# ---------------------------------------------------------------- fichiers de marque
def test_logo_png_upload_resized_and_served(admin, anonymous):
    version = current_theme().version
    response = upload(admin, "logo", image_bytes((2400, 600)))
    assert response.status_code == 303
    theme = current_theme()
    assert theme.logo_file.startswith("logo-") and theme.logo_file.endswith(".png")
    assert theme.version == version + 1
    assert (tm.branding_dir() / theme.logo_file).is_file()

    served = anonymous.get("/branding/logo")
    assert served.status_code == 200 and served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"
    with Image.open(BytesIO(served.content)) as image:
        assert image.size == (1024, 256)  # largeur ≤ 1024, hauteur ≤ 512, ratio 4:1 conservé
        assert image.mode == "RGBA"
    assert anonymous.get("/branding/logo", headers={"If-None-Match": served.headers["etag"]}).status_code == 304

    urls = tm.branding_urls(theme)
    assert urls["logo"] == f"/branding/logo?v={theme.version}" and urls["logo_dark"] == urls["logo"]
    assert urls["css"].startswith(CSS_URL)
    assert tm.logo_path_for_documents(theme) == tm.branding_dir() / theme.logo_file
    assert tm.theme_context(theme)["branding"] == urls

    # Remplacement : l'ancien fichier est supprimé
    old = theme.logo_file
    upload(admin, "logo", image_bytes((300, 900), fmt="JPEG", mode="RGB"), "photo.jpg", "image/jpeg")
    theme = current_theme()
    assert theme.logo_file != old and not (tm.branding_dir() / old).exists()
    with Image.open(tm.branding_dir() / theme.logo_file) as image:
        assert image.size == (171, 512) and image.mode == "RGB"
    assert "Fichier de marque enregistré" in flashes(admin)


def test_logo_webp_upload(admin, anonymous):
    response = upload(admin, "logo-dark", image_bytes((400, 200), fmt="WEBP"), "logo.webp", "image/webp")
    assert response.status_code == 303
    theme = current_theme()
    assert theme.logo_dark_file.endswith(".png")
    served = anonymous.get("/branding/logo-dark")
    assert served.status_code == 200 and served.headers["content-type"] == "image/png"
    assert tm.branding_urls(theme)["logo_dark"].startswith("/branding/logo-dark")
    assert tm.branding_urls(theme)["logo"] == ""


def test_svg_logo_accepted_and_served_with_csp(admin, anonymous):
    response = upload(admin, "logo", SAFE_SVG, "logo.svg", "image/svg+xml")
    assert response.status_code == 303
    theme = current_theme()
    assert theme.logo_file.endswith(".svg")
    served = anonymous.get("/branding/logo")
    assert served.status_code == 200 and served.headers["content-type"].startswith("image/svg+xml")
    assert served.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'"
    assert b"<svg" in served.content
    # Aucun rendu PNG d'un SVG : pas de logo sur les PDF
    assert tm.logo_path_for_documents(theme) is None
    png = tm.branding_dir() / theme.logo_file.replace(".svg", ".png")
    png.write_bytes(image_bytes())
    assert tm.logo_path_for_documents(theme) == png
    png.unlink()


@pytest.mark.parametrize("name", sorted(MALICIOUS_SVGS))
def test_malicious_svg_rejected(admin, name):
    response = upload(admin, "logo", MALICIOUS_SVGS[name], "evil.svg", "image/svg+xml")
    assert response.status_code == 303
    assert current_theme().logo_file == ""
    assert "SVG refusé" in flashes(admin)


def test_svg_sanitizer_edge_cases():
    with pytest.raises(BusinessError, match="encodage"):
        tm.sanitize_svg(b"<svg \xff\xfe>")
    with pytest.raises(BusinessError, match="invalide"):
        tm.sanitize_svg(b"<svg><unclosed></svg>")
    with pytest.raises(BusinessError, match="invalide"):
        tm.sanitize_svg(b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"/>')
    data_uri = (b'<svg xmlns="http://www.w3.org/2000/svg"><image href="data:image/png;base64,iVBORw0KGgo="/>'
                b'<rect onclick="x()"/></svg>')
    with pytest.raises(BusinessError, match="actif"):
        tm.sanitize_svg(data_uri)
    ok = tm.sanitize_svg(b'<svg xmlns="http://www.w3.org/2000/svg"><image href="data:image/png;base64,iVBORw0KGgo="/></svg>')
    assert ok.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n<svg')


def test_corrupted_oversized_and_unsupported_files_rejected(admin):
    corrupted = image_bytes()[:60]  # en-tête PNG tronqué
    upload(admin, "logo", corrupted)
    assert "Image invalide ou corrompue" in flashes(admin)

    upload(admin, "logo", b"\x89PNG\r\n\x1a\n" + b"\0" * (tm.MAX_UPLOAD_BYTES + 10))
    assert "Fichier trop volumineux" in flashes(admin)

    upload(admin, "logo", b"GIF89a" + b"\0" * 20, "anim.gif", "image/gif")
    assert "image non pris en charge" in flashes(admin)

    upload(admin, "logo", b"", "empty.png")
    assert "Aucun fichier sélectionné" in flashes(admin)

    response = admin.post("/admin/theme/upload/logo", data={"csrf_token": admin.csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert "Aucun fichier sélectionné" in flashes(admin)

    upload(admin, "banner", image_bytes())
    assert "Type de fichier de marque inconnu" in flashes(admin)
    assert current_theme().logo_file == ""


def test_decompression_bomb_rejected(admin):
    bomb = image_bytes((6000, 6000), mode="L", color=0)  # 36 Mpx, quelques Ko compressés
    assert len(bomb) < tm.MAX_UPLOAD_BYTES
    upload(admin, "logo", bomb)
    assert "Image trop grande" in flashes(admin)
    with pytest.raises(BusinessError, match="trop grande"):
        tm.open_raster(image_bytes((9000, 10), mode="L", color=0))
    assert current_theme().logo_file == ""


def test_open_raster_rejects_disguised_format():
    with pytest.raises(BusinessError, match="non pris en charge"):
        tm.open_raster(image_bytes(fmt="BMP", mode="RGB"))
    with pytest.raises(BusinessError, match="non pris en charge"):
        tm.prepare_branding_files("logo", image_bytes(fmt="BMP", mode="RGB"))
    with pytest.raises(BusinessError, match="inconnu"):
        tm.prepare_branding_files("banner", image_bytes())


def test_favicon_generated_square_png(admin, anonymous):
    response = upload(admin, "favicon", image_bytes((300, 120), fmt="JPEG", mode="RGB"), "icon.jpg", "image/jpeg")
    assert response.status_code == 303
    theme = current_theme()
    assert theme.favicon_file.startswith("favicon-") and theme.favicon_file.endswith(".png")
    big = anonymous.get("/branding/favicon")
    small = anonymous.get("/branding/favicon?size=32")
    assert big.status_code == small.status_code == 200
    with Image.open(BytesIO(big.content)) as image:
        assert image.format == "PNG" and image.size == (64, 64) and image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0  # bandes transparentes (image non carrée centrée)
    with Image.open(BytesIO(small.content)) as image:
        assert image.size == (32, 32)
    urls = tm.branding_urls(theme)
    assert urls["favicon"].startswith("/branding/favicon?v=") and "size=32" in urls["favicon_32"]

    upload(admin, "favicon", SAFE_SVG, "icon.svg", "image/svg+xml")
    assert "Le favicon doit être une image PNG" in flashes(admin)
    assert current_theme().favicon_file == theme.favicon_file


def test_delete_branding_files(admin, anonymous):
    upload(admin, "favicon", image_bytes((64, 64)))
    theme = current_theme()
    name = theme.favicon_file
    small = name.replace(".png", "-32.png")
    assert (tm.branding_dir() / small).is_file()
    response = admin.form("/admin/theme/delete/favicon")
    assert response.status_code == 303
    assert current_theme().favicon_file == ""
    assert not (tm.branding_dir() / name).exists() and not (tm.branding_dir() / small).exists()
    assert anonymous.get("/branding/favicon").status_code == 404
    assert "Fichier de marque supprimé" in flashes(admin)

    admin.form("/admin/theme/delete/favicon")
    assert "Aucun fichier à supprimer" in flashes(admin)
    admin.form("/admin/theme/delete/banner")
    assert "Type de fichier de marque inconnu" in flashes(admin)


def test_branding_route_not_found_cases(anonymous):
    assert anonymous.get("/branding/logo").status_code == 404
    assert anonymous.get("/branding/unknown").status_code == 404
    assert tm.branding_file(ThemeSettings(id=1, logo_file="../../etc/passwd"), "logo") is None
    assert tm.branding_file(ThemeSettings(id=1, logo_file="logo-0123456789ab.png"), "logo") is None  # absent
    assert tm.media_type_for(tm.branding_dir() / "x.bin") == "application/octet-stream"


def test_logo_hidden_on_documents_when_disabled(admin):
    upload(admin, "logo", image_bytes())
    admin.form("/admin/theme/documents", {})
    theme = current_theme()
    assert theme.show_logo_on_documents is False and tm.logo_path_for_documents(theme) is None


# ---------------------------------------------------------------- accès et sécurité
def test_admin_page_renders_for_admin(admin):
    response = admin.get("/admin/theme")
    assert response.status_code == 200
    assert 'action="/admin/theme/colors"' in response.text
    assert 'action="/admin/theme/upload/logo"' in response.text
    assert 'type="color"' in response.text and 'value="#0d9488"' in response.text


def test_admin_page_in_arabic_is_rtl(admin):
    admin.form("/lang", {"lang": "ar", "next": "/admin/theme"})
    try:
        response = admin.get("/admin/theme")
        assert response.status_code == 200 and 'dir="rtl"' in response.text
        assert "المظهر والهوية البصرية" in response.text
    finally:
        admin.form("/lang", {"lang": "fr", "next": "/"})


def test_theme_admin_requires_admin_role(reception, housekeeper, anonymous):
    assert reception.get("/admin/theme").status_code == 403
    assert reception.form("/admin/theme/colors", {"primary_color": "#000000"}).status_code == 403
    assert reception.form("/admin/theme/reset").status_code == 403
    assert housekeeper.form("/admin/theme/delete/logo").status_code == 403
    response = upload(reception, "logo", image_bytes())
    assert response.status_code == 403
    login = anonymous.get("/admin/theme", follow_redirects=False)
    assert login.status_code == 303 and login.headers["location"].startswith("/login")
    assert current_theme().primary_color == "#0d9488"


def test_theme_admin_requires_csrf(admin):
    response = admin.post("/admin/theme/colors", data={"primary_color": "#000000"}, follow_redirects=False)
    assert response.status_code == 403 and "Session expirée" in response.text
    response = admin.post("/admin/theme/upload/logo", data={"csrf_token": "faux"},
                          files={"file": ("l.png", image_bytes(), "image/png")}, follow_redirects=False)
    assert response.status_code == 403
    assert current_theme().primary_color == "#0d9488" and current_theme().logo_file == ""


def test_public_routes_exempt_from_license(monkeypatch, anonymous):
    from app.config import config
    from app.middlewares import license_checker

    monkeypatch.setattr(config, "license_enforced", True)
    assert license_checker._exempt("/branding/logo") and license_checker._exempt(CSS_URL)
    assert anonymous.get(CSS_URL).status_code == 200
    assert anonymous.get("/branding/logo").status_code == 404  # 404 propre, pas de redirection /activate


async def test_service_functions_direct(db):
    theme = await tm.update_colors(db, {"secondary_color": "#7C3AED"})
    assert theme.secondary_color == "#7c3aed"
    with pytest.raises(BusinessError):
        await tm.update_typography(db, "inter", "")
    with pytest.raises(BusinessError):
        await tm.save_image(db, "logo", None)
    with pytest.raises(BusinessError):
        await tm.save_image(db, "banner", None)
    with pytest.raises(BusinessError):
        await tm.delete_image(db, "banner")
    fields = tm.color_fields(theme)
    assert [f["name"] for f in fields] == list(tm.COLOR_FIELDS)
    assert fields[1]["value"] == "#7c3aed" and fields[1]["default"] == "#0284c7"
    data = tm.preview(theme)
    assert data["accent"][600] == "#7c3aed" and data["on_primary"] == "#ffffff"
    assert data["font_stack_rtl"].startswith('"Noto Sans Arabic"')
    await db.rollback()
