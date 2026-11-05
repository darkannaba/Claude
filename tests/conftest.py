"""Configuration des tests : base SQLite jetable, comptes à mots de passe connus, clients HTTP par rôle."""
import asyncio
import os
import re
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="hotel-tests-"))
os.environ.update({
    "DATABASE_URL": f"sqlite+aiosqlite:///{(_TMP / 'test.db').as_posix()}",
    "SECRET_KEY": "test-secret-key-0123456789",
    "SCHEDULER_ENABLED": "false",
    "BACKUP_DIR": str(_TMP / "backups"),
    "LOG_DIR": str(_TMP / "logs"),
    "DATA_DIR": str(_TMP / "data"),
    "ENVIRONMENT": "test",
    "LICENSE_ENFORCED": "false",  # activé explicitement dans tests/test_licensing.py
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import User  # noqa: E402
from app.services.security import hash_password  # noqa: E402
from app.services.seed import bootstrap  # noqa: E402

PASSWORDS = {"admin": "Admin-pass-2026", "reception": "Recep-pass-2026", "gouvernante": "Gouv-pass-2026"}
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')


async def _prepare_users() -> None:
    await bootstrap()
    async with SessionLocal() as db:
        for user in (await db.scalars(select(User))).all():
            if user.username in PASSWORDS:
                user.password_hash = hash_password(PASSWORDS[user.username])
                user.must_change_password = False
        await db.commit()


@pytest.fixture(scope="session")
def app():
    asyncio.run(_prepare_users())
    application = create_app()
    with TestClient(application):  # exécute le lifespan une fois (migrations + seed idempotents)
        yield application


class Client(TestClient):
    """Client de test qui ajoute automatiquement le jeton CSRF aux requêtes POST."""

    csrf = ""

    def refresh_csrf(self) -> str:
        page = self.get("/login") if not self.csrf else self.get("/account/password")
        match = CSRF_RE.search(page.text)
        if match:
            self.csrf = match.group(1)
        return self.csrf

    def form(self, url: str, data: dict | None = None, **kwargs):
        payload = {"csrf_token": self.csrf, **(data or {})}
        kwargs.setdefault("follow_redirects", False)
        return self.post(url, data=payload, **kwargs)


def make_client(app, username: str | None = None) -> Client:
    client = Client(app)
    client.refresh_csrf()
    if username:
        response = client.form("/login", {"username": username, "password": PASSWORDS[username]})
        assert response.status_code == 303, response.text
    return client


@pytest.fixture()
def admin(app):
    return make_client(app, "admin")


@pytest.fixture()
def reception(app):
    return make_client(app, "reception")


@pytest.fixture()
def housekeeper(app):
    return make_client(app, "gouvernante")


@pytest.fixture()
def anonymous(app):
    return make_client(app)


@pytest.fixture()
async def db(app):
    async with SessionLocal() as session:
        yield session
        await session.rollback()


@pytest.fixture()
def tmp_dir():
    return _TMP
