"""Toutes les pages s'affichent pour chaque rôle autorisé, et sont refusées aux autres."""
import pytest

ADMIN_PAGES = [
    "/", "/planning", "/planning?days=31&floor=2", "/reservations", "/reservations?status=confirmed&q=a",
    "/reservations/new", "/reservations/new?check_in=2030-01-10&check_out=2030-01-12&room_type_id=1",
    "/guests", "/guests/new", "/guests/duplicates", "/guests/unpaid", "/companies", "/housekeeping",
    "/housekeeping?floor=1", "/pos", "/pos/catalog", "/pos/stock", "/night-audit", "/invoices", "/reports",
    "/reports?granularity=month&year=2026", "/admin", "/admin/rooms", "/admin/rates", "/admin/modules",
    "/admin/users", "/admin/backups", "/admin/audit-log", "/account/password", "/search?q=zzz-introuvable",
]


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_pages_render(admin, path):
    response = admin.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}\n{response.text[:2000]}"
    assert "<html" in response.text


RECEPTION_FORBIDDEN = ["/admin", "/admin/users", "/admin/backups", "/reports/export.csv"]


@pytest.mark.parametrize("path", RECEPTION_FORBIDDEN)
def test_reception_cannot_open_admin(reception, path):
    assert reception.get(path, follow_redirects=False).status_code == 403


HOUSEKEEPING_FORBIDDEN = ["/planning", "/reservations", "/guests", "/pos", "/reports", "/night-audit", "/admin"]


@pytest.mark.parametrize("path", HOUSEKEEPING_FORBIDDEN)
def test_housekeeper_is_restricted(housekeeper, path):
    assert housekeeper.get(path, follow_redirects=False).status_code == 403


def test_housekeeper_home_redirects_to_board(housekeeper):
    response = housekeeper.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/housekeeping"
    page = housekeeper.get("/housekeeping")
    assert page.status_code == 200
    assert "Réservations" not in page.text  # menu réduit au housekeeping


def test_anonymous_is_redirected_to_login(anonymous):
    response = anonymous.get("/reservations", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


def test_static_assets_are_local(anonymous):
    page = anonymous.get("/login").text
    assert "cdn." not in page and "https://" not in page
    for asset in ("/static/css/app.css", "/static/vendor/alpine.min.js", "/static/vendor/chart.umd.min.js",
                  "/static/js/app.js", "/static/fonts/DejaVuSans.ttf"):
        assert anonymous.get(asset).status_code == 200, asset


def test_dynamic_css_classes_are_compiled(anonymous):
    """Classes composées dans les templates (room-{{ status }}…) : doivent figurer dans la safelist Tailwind."""
    css = anonymous.get("/static/css/app.css").text
    for name in ("room-available", "room-occupied", "room-dirty", "room-maintenance", "bar-confirmed",
                 "bar-checked_in", "bar-checked_out", "flash-success", "flash-error", "flash-info",
                 "flash-warning", "flash-danger"):
        assert f".{name}{{" in css, name


def test_security_headers(anonymous):
    response = anonymous.get("/login")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"


def test_health_and_404(anonymous, admin):
    assert anonymous.get("/health").json()["status"] == "ok"
    assert admin.get("/reservations/999999").status_code == 404
    assert admin.get("/page-inexistante").status_code == 404
    assert admin.get("/api/inexistant").headers["content-type"].startswith("application/json")
